"""Recipient keys and snapshot-bound sealed password hashes. No plaintext cache."""
import fcntl
import json
import os
from pathlib import Path

import frappe
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization

from onerc_vmms_migrate import login_crypto, workspace
from onerc_vmms_migrate.source import SourceError


def keys():
    folder = Path(workspace.path("login-transfer", "placeholder")).parent
    os.chmod(folder, 0o700)
    key_file = folder / "recipient-key.enc"
    try:
        wrapping = Fernet(frappe.conf.encryption_key.encode())
        descriptor = os.open(folder / "key.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if key_file.exists():
                private = wrapping.decrypt(key_file.read_bytes())
                key = serialization.load_pem_private_key(private, password=None)
                public = key.public_key().public_bytes(serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo)
            else:
                private, public = login_crypto.new_keypair()
                temp = key_file.with_suffix(".tmp")
                with temp.open("xb") as handle:
                    os.chmod(temp, 0o600)
                    handle.write(wrapping.encrypt(private))
                os.replace(temp, key_file)
        return private, public
    except Exception:
        raise SourceError("The protected login transfer key could not be opened. Check the target site's encryption configuration.") from None


def public_key_info():
    _, public = keys()
    return {"public_key": public.decode(), "recipient_fingerprint": login_crypto.fingerprint(public)}


def decrypt(envelope, source_id, snapshot_id):
    try:
        private, _ = keys()
        return login_crypto.open_envelope(envelope, private, source_id, snapshot_id)
    except login_crypto.LoginTransferError:
        raise SourceError("The login bundle failed recipient or snapshot verification.") from None


def load_capture(directory, source):
    path = Path(directory) / "login-envelope.json"
    if not path.is_file():
        raise SourceError("The selected capture is missing its protected login bundle.")
    return decrypt(json.loads(path.read_text()), source["source_id"], source["snapshot_id"])
