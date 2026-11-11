"""Recover falsely held blobs only against captured SHA256 and a source File row."""

import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

from onerc_vmms_migrate.source import SourceError


def external(url):
	parsed = urlsplit(str(url))
	return parsed.scheme.lower() in ("http", "https") or bool(parsed.netloc)


def matches_source_row(item, path):
	sha = hashlib.sha256()
	md5 = hashlib.md5(usedforsecurity=False)
	size = 0
	with open(path, "rb") as handle:
		while block := handle.read(1024 * 1024):
			sha.update(block)
			md5.update(block)
			size += len(block)
	if sha.hexdigest() != item.get("sha256") or size != item.get("size"):
		return False
	return any(
		str(row.get("content_hash") or "").lower() == md5.hexdigest()
		and (not row.get("file_size") or int(row["file_size"]) == size)
		for row in item.get("references") or []
	)


def recover(source, url, settings=None):
	from onerc_vmms_migrate import workspace

	item = source.data["all_file_manifest"][url]
	if item.get("status") != "corrupt":
		return False
	path = source.file_path(url)
	if not path:
		digest = item.get("sha256") or ""
		import re

		if not re.fullmatch("[0-9a-f]{64}", digest):
			return False
		candidate = Path(workspace.path("bulk-blobs", digest))
		if candidate.is_file():
			path = str(candidate)
	if not path and settings is not None:
		from onerc_vmms_migrate.browser_source import APIClient

		client = APIClient(settings)
		try:
			folder = str(Path(workspace.path("recovered-blobs", "placeholder")).parent)
			relative = client.download(url, folder)
			path = os.path.join(workspace.root(), relative)
		finally:
			client.close()
	if not path or not matches_source_row(item, path):
		return False
	item = {
		**item,
		"status": "ready",
		"original_status": "corrupt",
		"recovery": "Captured SHA256 and at least one source File MD5/size matched",
	}
	source.data["all_file_manifest"][url] = item
	source.data.setdefault("captured_file_manifest", {})[url] = item
	source.data.setdefault("captured_files", {})[url] = os.path.relpath(path, workspace.root())
	return True


def persist(source):
	"""Only blob receipts change; the captured source records remain immutable."""
	import json
	from onerc_vmms_migrate import workspace

	capture = source.data["source"].get("capture_id") or source.data["source"].get("snapshot_id")
	target = Path(workspace.path("captures", capture, "snapshot.json"))
	raw = json.loads(target.read_text())
	for key in ("all_file_manifest", "captured_file_manifest", "captured_files"):
		raw[key] = source.data.get(key, {})
	temporary = str(target) + ".recovering"
	workspace.write_json(temporary, raw)
	os.replace(temporary, target)
