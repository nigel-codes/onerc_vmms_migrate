"""Approve receiver code updates without relaxing the pinned migration inputs.

0.7.1 stored only a combined inputs+code digest. Its immutable, non-executable
source baseline lets us recompute that exact digest with the CURRENT inputs.
It contains repository code only; no captured records or site configuration.
New sessions store a separate input digest and do not need this compatibility path.
"""

import gzip
import hashlib
import hmac
import io
import zlib
from pathlib import Path

LEGACY_COMMIT = "6a3d44ea3f3217800be0b8aee9d1f6dcedbdec5f"
LEGACY_SHA256 = "66762e790d8a180bc7af8230b0cdb2510c71fb53f54135999b8ab30246fe1340"
LEGACY_SIZE = 523419
BASELINE = Path(__file__).with_name("resume_baselines") / "0.7.1-code.gz"


def legacy_inputs_match(previous_fingerprint):
	from onerc_vmms_migrate import browser

	try:
		with gzip.GzipFile(fileobj=io.BytesIO(BASELINE.read_bytes())) as handle:
			raw = handle.read(LEGACY_SIZE + 1)
	except (OSError, EOFError, zlib.error) as exc:
		raise ValueError(
			"The 0.7.1 resume baseline is unavailable or damaged. Deploy the complete receiver update."
		) from exc
	if len(raw) != LEGACY_SIZE or hashlib.sha256(raw).hexdigest() != LEGACY_SHA256:
		raise ValueError("The 0.7.1 resume code baseline failed integrity verification.")
	digest = browser._input_digest()
	digest.update(raw)
	return hmac.compare_digest(digest.hexdigest(), str(previous_fingerprint))


def approve_people_code_update(value):
	from onerc_vmms_migrate import browser, workspace

	if value.get("scope") != "people" or value.get("mode") != "import":
		raise ValueError("Code-only resume is restricted to the production people workflow.")
	if workspace.config().get("browser_capture") != value.get("capture_id"):
		raise ValueError("The selected capture changed. Restore the original capture before resuming.")
	current = browser.fingerprint(False)
	pinned = value.get("inputs_fingerprint")
	if pinned:
		if not hmac.compare_digest(str(pinned), current):
			raise ValueError(
				"Capture, settings, mappings or placeholder assignments changed. Restore the original inputs before resuming."
			)
		proof = "pinned input fingerprint"
	elif legacy_inputs_match(value["fingerprint"]):
		proof = "receiver 0.7.1 source baseline " + LEGACY_COMMIT
	else:
		raise ValueError(
			"The original 0.7.1 inputs could not be verified. Keep the checkpoint and review the capture/settings/mappings; no records were reset."
		)
	return {"inputs_fingerprint": current, "proof": proof}
