import hashlib
import tempfile
from pathlib import Path
from unittest import TestCase
from onerc_vmms_migrate.file_recovery import matches_source_row, external


class TestFileRecovery(TestCase):
	def test_one_matching_row_is_enough_but_transport_bytes_must_match(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "blob"
			path.write_bytes(b"original file")
			item = {
				"size": 13,
				"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
				"references": [
					{"file_size": 1, "content_hash": "0" * 32},
					{"file_size": 13, "content_hash": hashlib.md5(path.read_bytes()).hexdigest()},
				],
			}
			self.assertTrue(matches_source_row(item, path))
			path.write_bytes(b"changed bytes")
			self.assertFalse(matches_source_row(item, path))

	def test_external_is_not_missing_local_bytes(self):
		self.assertTrue(external("https://gravatar.com/avatar/a"))
		self.assertTrue(external("//example.org/image.png"))
		self.assertFalse(external("/private/files/cv.pdf"))
