import hashlib
import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock

import frappe
import requests
from onerc_vmms_migrate.bulk_source import BulkClient, source_origin
from onerc_vmms_migrate import bulk_source
from onerc_vmms_migrate.source import SourceError


class TestBulkTransfer(TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(frappe, "conf", frappe._dict()).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.client = object.__new__(BulkClient)

    def response(self, content, offset, full):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"X-VMMS-Offset": str(offset), "X-VMMS-Size": str(len(full)),
            "X-VMMS-SHA256": hashlib.sha256(full).hexdigest(),
            "X-VMMS-Chunk-SHA256": hashlib.sha256(content).hexdigest()}
        response.iter_content.return_value = [content]
        return response

    def test_resume_only_requests_remaining_bytes_then_reuses_verified_blob(self):
        full = b"abcdefghij"
        item = {"id": "a" * 32, "size": len(full), "sha256": hashlib.sha256(full).hexdigest()}
        (self.cache / (item["sha256"] + ".part")).write_bytes(full[:4])
        self.client.request = MagicMock(side_effect=[self.response(full[4:8], 4, full), self.response(full[8:], 8, full)])
        path, copied = self.client.download("b" * 32, item, self.cache, 4)
        self.assertTrue(copied)
        self.assertEqual(path.read_bytes(), full)
        self.assertEqual([c.args[2]["offset"] for c in self.client.request.call_args_list], [4, 8])
        self.client.request.reset_mock()
        self.assertFalse(self.client.download("b" * 32, item, self.cache, 4)[1])
        self.client.request.assert_not_called()

    def test_interrupted_request_does_not_append_partial_chunk(self):
        full = b"abcdefgh"
        item = {"id": "a" * 32, "size": len(full), "sha256": hashlib.sha256(full).hexdigest()}
        response = self.response(full[:4], 0, full)
        def interrupted(*args):
            yield b"ab"
            raise requests.ConnectionError("interrupted")
        response.iter_content.side_effect = interrupted
        self.client.request = MagicMock(return_value=response)
        with self.assertRaises(SourceError):
            self.client.download("b" * 32, item, self.cache, 4)
        self.assertFalse((self.cache / (item["sha256"] + ".part")).exists())

    def test_corrupted_resumed_bytes_fail_full_checksum(self):
        full = b"abcdefgh"
        item = {"id": "a" * 32, "size": len(full), "sha256": hashlib.sha256(full).hexdigest()}
        part = self.cache / (item["sha256"] + ".part")
        part.write_bytes(b"XXXX")
        self.client.request = MagicMock(return_value=self.response(full[4:], 4, full))
        with self.assertRaisesRegex(SourceError, "SHA-256"):
            self.client.download("b" * 32, item, self.cache, 4)
        self.assertFalse(part.exists())

    def test_local_http_requires_host_configuration_and_loopback(self):
        with self.assertRaises(SourceError):
            source_origin("http://127.0.0.1:8199")
        frappe.conf.onerc_vmms_migrate_allow_local_source = 1
        self.assertEqual(source_origin("http://127.0.0.1:8199"), "http://127.0.0.1:8199")
        for url in ("http://10.0.0.1:8199", "http://127.0.0.1:8199/path", "http://user:secret@localhost:8199", "http://localhost"):
            with self.assertRaises(SourceError):
                source_origin(url)


class TestCaptureCheckpoints(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(self.tmp.name)
        patch.object(bulk_source.workspace, "root", return_value=str(self.root)).start()
        self.client = MagicMock()
        self.client.url = "https://source.example.org"
        self.calls = []
        self.interrupt = True
        self.rows = {dataset: [] for dataset in bulk_source.DATASETS}
        for dataset in ("source", "missing_columns", "custom_fields"):
            self.rows[dataset] = [{"key": "value", "data": {"source_id": "source"} if dataset == "source" else []}]
        self.rows["users"] = [{"key": "a", "data": {"name": "a"}}, {"key": "b", "data": {"name": "b"}}]
        self.missing = False
        def response(endpoint, params=None, method="GET"):
            params = params or {}
            self.calls.append((endpoint, dict(params)))
            if endpoint == "capabilities":
                return {"protocol": 2, "source_id": "source", "datasets": list(bulk_source.DATASETS), "max_chunk_bytes": 4}
            if endpoint in ("create_snapshot", "snapshot_status"):
                return {"status": "ready", "source_id": "source", "counts": {k: len(v) for k, v in self.rows.items()}, "files": int(self.missing)}
            if endpoint == "file_manifest":
                return {"rows": [{"id": "c" * 32, "source_url": "/private/files/missing.pdf", "status": "missing"}] if self.missing else [], "next_cursor": None}
            dataset = params["dataset"]
            if dataset == "users":
                if params["cursor"] == "next":
                    if self.interrupt:
                        self.interrupt = False
                        raise SourceError("test interrupted")
                    rows, cursor = self.rows[dataset][1:], None
                else:
                    rows, cursor = self.rows[dataset][:1], "next"
            else:
                rows, cursor = self.rows[dataset], None
            return {"rows": [{**row, "sha256": hashlib.sha256(bulk_source.canonical(row["data"]).encode()).hexdigest()} for row in rows], "next_cursor": cursor}
        self.client.json.side_effect = response
        patch.object(bulk_source, "BulkClient", return_value=self.client).start()

    def test_record_capture_resumes_from_last_committed_page(self):
        capture_id = "a" * 32
        with self.assertRaisesRegex(SourceError, "test interrupted"):
            bulk_source.capture(None, capture_id)
        self.calls.clear()
        result = bulk_source.capture(None, capture_id)
        self.assertTrue(result["complete"])
        user_calls = [params for endpoint, params in self.calls if endpoint == "records" and params["dataset"] == "users"]
        self.assertEqual(len(user_calls), 1)
        self.assertEqual(user_calls[0]["cursor"], "next")
        data = json.loads((self.root / "captures" / capture_id / "snapshot.json").read_text())
        self.assertEqual(set(data["users"]), {"a", "b"})

    def test_missing_private_files_keep_capture_incomplete(self):
        self.interrupt = False
        self.missing = True
        result = bulk_source.capture(None, "b" * 32)
        self.assertFalse(result["complete"])
        self.assertEqual(result["missing_files"], 1)
        data = json.loads((self.root / "captures" / ("b" * 32) / "snapshot.json").read_text())
        self.assertFalse(data["capture_complete"])
        self.client.download.assert_not_called()
