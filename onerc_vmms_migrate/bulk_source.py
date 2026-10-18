"""Resumable v2 server-to-server capture, compatible with the existing loader."""
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

import frappe
import requests

from onerc_vmms_migrate import workspace
from onerc_vmms_migrate.browser_source import validate_url
from onerc_vmms_migrate.source import SourceError

PREFIX = "/api/method/onerc_vmms_export.api."
MAP_DATASETS = {"users", "employees", "applicants", "members", "memberships", "membership_types", "invoices", "files", "history", "schemas"}
DATASETS = (*sorted(MAP_DATASETS), "source", "missing_columns", "custom_fields")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        os.chmod(temp, 0o600)
        handle.write(canonical(value))
    os.replace(temp, path)


def sha256_file(path):
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            sha.update(chunk)
    return sha.hexdigest()


def source_origin(value):
    """Production always uses public HTTPS; loopback is a host-only test option."""
    parsed = urlsplit(value.strip())
    if frappe.conf.get("onerc_vmms_migrate_allow_local_source") and parsed.scheme == "http":
        if (parsed.hostname not in ("127.0.0.1", "localhost") or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment or not parsed.port):
            raise SourceError("The local test source must be an explicit loopback origin and port.")
        return f"http://{parsed.hostname}:{parsed.port}"
    return validate_url(value)


class BulkClient:
    def __init__(self, settings):
        self.url = source_origin(settings.source_url)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Authorization": f"token {settings.api_key}:{settings.get_password('api_secret')}"})

    def close(self):
        self.session.close()

    def request(self, method, endpoint, params=None):
        source_origin(self.url)
        try:
            response = self.session.request(method, self.url + PREFIX + endpoint,
                params=params if method == "GET" else None,
                data=params if method == "POST" else None,
                timeout=(10, 90), allow_redirects=False, stream=True)
        except requests.RequestException:
            raise SourceError("The source transfer was interrupted. Resume the capture to retry.") from None
        if response.status_code != 200:
            status = response.status_code
            response.close()
            raise SourceError(f"The export API returned HTTP {status}. Check source export permissions and worker status.")
        return response

    def json(self, endpoint, params=None, method="GET"):
        with self.request(method, endpoint, params) as response:
            try:
                value = response.json()["message"]
            except (ValueError, KeyError):
                raise SourceError("The export API returned an invalid response.") from None
            if not isinstance(value, dict):
                raise SourceError("The export API returned an invalid response.")
            return value

    def download(self, snapshot_id, item, cache, chunk_bytes):
        sha, size = item.get("sha256"), item.get("size")
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha) or not isinstance(size, int) or size < 0:
            raise SourceError("The export manifest contains invalid file metadata.")
        target = Path(cache) / sha
        if target.exists() and target.stat().st_size == size and sha256_file(target) == sha:
            return target, False
        part = target.with_suffix(".part")
        if part.exists() and part.stat().st_size > size:
            part.unlink()
        offset = part.stat().st_size if part.exists() else 0
        # Verify each received chunk before appending it. A failed request cannot
        # advance the persistent offset; the final digest also verifies prior bytes.
        while offset < size:
            length = min(chunk_bytes, size - offset)
            try:
                with self.request("GET", "file_chunk", {"snapshot_id": snapshot_id, "file_id": item["id"],
                    "offset": offset, "length": length}) as response:
                    if (response.headers.get("X-VMMS-SHA256") != sha
                        or response.headers.get("X-VMMS-Offset") != str(offset)
                        or response.headers.get("X-VMMS-Size") != str(size)):
                        raise SourceError("The source returned a mismatched attachment range.")
                    chunks, received = [], 0
                    for content in response.iter_content(65536):
                        received += len(content)
                        if received > length:
                            raise SourceError("The source attachment exceeded its requested range.")
                        chunks.append(content)
                    content = b"".join(chunks)
                    if len(content) != length or hashlib.sha256(content).hexdigest() != response.headers.get("X-VMMS-Chunk-SHA256"):
                        raise SourceError("The attachment chunk did not match its checksum.")
            except requests.RequestException:
                raise SourceError("An attachment transfer was interrupted. Resume to retry.") from None
            with part.open("ab") as handle:
                os.chmod(part, 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            offset += length
        if not part.exists():
            part.touch(mode=0o600)
        if sha256_file(part) != sha:
            part.unlink()
            raise SourceError("The completed attachment failed SHA-256 verification. Resume to download it again.")
        os.replace(part, target)
        return target, True


def capture(settings, capture_id, progress=None, wait_seconds=14400):
    if not re.fullmatch(r"[a-f0-9]{32}", capture_id):
        raise SourceError("Invalid capture identifier.")
    directory = Path(workspace.path("captures", capture_id, "placeholder")).parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = Path(workspace.path("bulk-blobs", "placeholder")).parent
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = BulkClient(settings)
    db = None
    try:
        caps = client.json("capabilities")
        if caps.get("protocol") != 2 or not isinstance(caps.get("source_id"), str) or not set(DATASETS).issubset(caps.get("datasets", [])):
            raise SourceError("The source export app uses an unsupported protocol or dataset list.")
        chunk_bytes = caps.get("max_chunk_bytes")
        if not isinstance(chunk_bytes, int) or not 1 <= chunk_bytes <= 4 * 1024 * 1024:
            raise SourceError("The source advertises an unsupported attachment chunk size.")
        preserve_logins = bool(getattr(settings, "preserve_passwords", False))
        recipient = ""
        if preserve_logins:
            from onerc_vmms_migrate.logins import public_key_info
            recipient = public_key_info()["recipient_fingerprint"]
            advertised = caps.get("sealed_logins") or {}
            if not advertised.get("enabled") or advertised.get("recipient_fingerprint") != recipient:
                raise SourceError("Register this target's login transfer public key on the source and enable protected login export before capturing.")
        state_path = directory / "transfer.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {
            "capture_id": capture_id, "snapshot_id": capture_id,
            "source_url": client.url, "source_id": caps["source_id"], "status": "starting",
            "preserve_logins": preserve_logins, "recipient_fingerprint": recipient}
        if state["source_id"] != caps["source_id"] or state["source_url"] != client.url:
            raise SourceError("This capture belongs to a different source. Start a new capture.")
        if bool(state.get("preserve_logins")) != preserve_logins or state.get("recipient_fingerprint", "") != recipient:
            raise SourceError("The capture login-transfer options changed. Start a new capture.")
        atomic_json(state_path, state)
        request = {"request_id": capture_id}
        if preserve_logins:
            request.update(include_logins="1", recipient_fingerprint=recipient)
        remote = client.json("create_snapshot", request, method="POST")
        deadline = time.monotonic() + wait_seconds
        while remote.get("status") in ("queued", "building"):
            if progress:
                progress({"phase": "source_export", **remote})
            if time.monotonic() >= deadline:
                raise SourceError("The source export is still pending. Check its long worker and resume later.")
            time.sleep(3)
            remote = client.json("snapshot_status", {"snapshot_id": capture_id})
        if remote.get("status") != "ready" or remote.get("source_id") != caps["source_id"]:
            raise SourceError("The source snapshot is not ready. Check the source worker log.")
        db = sqlite3.connect(directory / "transfer.sqlite")
        os.chmod(directory / "transfer.sqlite", 0o600)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS records (dataset TEXT, record_key TEXT, payload TEXT, sha256 TEXT, PRIMARY KEY(dataset,record_key));
            CREATE TABLE IF NOT EXISTS checkpoints (dataset TEXT PRIMARY KEY, cursor TEXT, complete INTEGER);
        """)
        for dataset in (*DATASETS, "file_manifest"):
            checkpoint = db.execute("SELECT cursor,complete FROM checkpoints WHERE dataset=?", (dataset,)).fetchone()
            if checkpoint and checkpoint[1]:
                continue
            cursor = checkpoint[0] if checkpoint else ""
            while True:
                params = {"snapshot_id": capture_id, "cursor": cursor or "", "limit": 200}
                endpoint = "file_manifest" if dataset == "file_manifest" else "records"
                if dataset != "file_manifest":
                    params["dataset"] = dataset
                page = client.json(endpoint, params)
                rows = page.get("rows")
                next_cursor = page.get("next_cursor")
                if not isinstance(rows, list) or (next_cursor is not None and (not isinstance(next_cursor, str) or not rows or next_cursor == cursor)):
                    raise SourceError("Invalid or repeated export page.")
                for row in rows:
                    if dataset == "file_manifest":
                        key, value = row["id"], row
                        digest = hashlib.sha256(canonical(value).encode()).hexdigest()
                    else:
                        key, value, digest = row["key"], row["data"], row["sha256"]
                        if hashlib.sha256(canonical(value).encode()).hexdigest() != digest:
                            raise SourceError("A source record failed its checksum.")
                    if db.execute("SELECT 1 FROM records WHERE dataset=? AND record_key=?", (dataset, key)).fetchone():
                        raise SourceError("The source repeated a record across pages.")
                    db.execute("INSERT INTO records VALUES (?,?,?,?)", (dataset, key, canonical(value), digest))
                db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)", (dataset, next_cursor, int(next_cursor is None)))
                db.commit()
                if progress:
                    progress({"phase": "records", "dataset": dataset,
                              "received": db.execute("SELECT count(*) FROM records WHERE dataset=?", (dataset,)).fetchone()[0]})
                if next_cursor is None:
                    break
                cursor = next_cursor
        snapshot = {"password_hashes": {}, "captured_files": {}, "captured_file_manifest": {}}
        for dataset in DATASETS:
            if dataset == "history":
                count = db.execute("SELECT count(*) FROM records WHERE dataset='history'").fetchone()[0]
                if count != remote.get("counts", {}).get(dataset):
                    raise SourceError("The captured history count does not match the source snapshot.")
                from onerc_vmms_migrate.history_store import index
                index(db)
                snapshot["history"] = {}
                snapshot["history_storage"] = {"count":count,"sqlite_sha256":sha256_file(directory / "transfer.sqlite")}
                continue
            rows = db.execute("SELECT record_key,payload FROM records WHERE dataset=? ORDER BY record_key", (dataset,)).fetchall()
            if len(rows) != remote.get("counts", {}).get(dataset):
                raise SourceError(f"The captured {dataset} count does not match the source snapshot.")
            snapshot[dataset] = {key: json.loads(payload) for key, payload in rows} if dataset in MAP_DATASETS else json.loads(rows[0][1]) if rows else {}
        manifest = [json.loads(row[0]) for row in db.execute("SELECT payload FROM records WHERE dataset='file_manifest' ORDER BY record_key")]
        if len(manifest) != remote.get("files"):
            raise SourceError("The captured attachment count does not match the source snapshot.")
        missing = [item for item in manifest if item.get("status") != "ready"]
        atomic_json(directory / "missing-files.json", missing)
        report = Path(workspace.report_dir(capture_id + "-bulk-capture"))
        atomic_json(report / "missing-files.json", missing)
        transferred = reused = 0
        # Transfer available files even when private bytes are still outstanding.
        # This preserves progress for inspection; such a capture cannot be imported.
        for index, item in enumerate(manifest, 1):
            if item.get("status") != "ready":
                continue
            path, copied = client.download(capture_id, item, cache, chunk_bytes)
            snapshot["captured_files"][item["source_url"]] = str(path.relative_to(Path(workspace.root())))
            snapshot["captured_file_manifest"][item["source_url"]] = item
            transferred += int(copied)
            reused += int(not copied)
            if progress and index % 10 == 0:
                progress({"phase": "files", "processed": index, "total": len(manifest), "transferred": transferred, "reused": reused})
        complete = not missing
        snapshot["capture_complete"] = complete
        snapshot["all_file_manifest"] = {item["source_url"]: item for item in manifest}
        snapshot["source"].update(source_url=client.url, capture_id=capture_id)
        password_count = 0
        if preserve_logins:
            from onerc_vmms_migrate.logins import decrypt
            if not remote.get("logins", {}).get("included"):
                raise SourceError("The source snapshot did not preserve its login credentials.")
            envelope = client.json("login_bundle", {"snapshot_id": capture_id, "recipient_fingerprint": recipient}, method="POST")
            passwords = decrypt(envelope, caps["source_id"], capture_id)
            if set(passwords) - set(snapshot["users"]):
                raise SourceError("The login bundle contains accounts outside the captured user dataset.")
            password_count = len(passwords)
            if password_count != remote["logins"].get("users_with_password"):
                raise SourceError("The captured password count does not match the source snapshot.")
            del passwords
            atomic_json(directory / "login-envelope.json", envelope)
            snapshot["source"]["logins"] = {"included": True, "users_with_password": password_count,
                "recipient_fingerprint": recipient}
        atomic_json(directory / "snapshot.json", snapshot)
        summary = {"capture_id": capture_id, "source": client.url, "source_id": caps["source_id"],
                   "counts": {key: snapshot["history_storage"]["count"] if key == "history" else len(snapshot[key]) for key in sorted(MAP_DATASETS)},
                   "files": len(manifest), "missing_files": len(missing), "complete": complete,
                   "files_transferred": transferred, "files_reused": reused,
                   "password_hashes_preserved": password_count,
                   "passwords": "Existing password hashes preserved through protected transport." if preserve_logins else "Not exported. New users require password resets."}
        state.update(status="complete" if complete else "incomplete", result=summary)
        atomic_json(state_path, state)
        atomic_json(report / "capture.json", summary)
        return summary
    except Exception:
        if db is not None:
            db.rollback()
        raise
    finally:
        if db is not None:
            db.close()
        client.close()
