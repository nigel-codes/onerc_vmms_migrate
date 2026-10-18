"""Stream already verified historical attachments without loading them in RAM."""
import hashlib
import os
import uuid
from pathlib import Path

import frappe


PERSONAL_OWNERS=frozenset({'User','Red Profile','VMMS Volunteer','VMMS Member','VMMS Membership',
    'VMMS Volunteer Application','VMMS Deployment Assignment','VMMS Terms of Reference','Event Booking','Event Ticket'})


def required_privacy(doctype,source_private,explicit=None):
    return bool(source_private or doctype=='Legacy VMMS Document' or
        (doctype in PERSONAL_OWNERS if explicit is None else explicit))


def assert_supported():
    from frappe.core.doctype.file.file import File
    if 'copy_from_existing_file' not in File.before_insert.__code__.co_names:
        raise RuntimeError('This Frappe build lacks the existing-blob File insert support required for verified streaming migration. Upgrade to a maintenance build with that support before importing.')


def copy_verified(source_path, original_name, doctype, name, private, expected_sha=None):
    # Native File validation and permissions still run. Its existing-blob seam
    # prevents historical bytes being transformed, charged against upload size
    # limits or read into memory as a single multi-gigabyte Python string.
    assert_supported()
    directory=Path(frappe.get_site_path('private' if private else 'public','files'))
    directory.mkdir(parents=True,exist_ok=True)
    temporary=directory/('vmms-part-'+uuid.uuid4().hex)
    sha=hashlib.sha256()
    md5=hashlib.md5(usedforsecurity=False)
    size=0
    created=False
    destination=None
    try:
        with open(source_path,'rb') as source,temporary.open('xb') as target:
            # Match native File storage permissions so nginx can serve authorised
            # private downloads. Privacy is enforced by File permissions and
            # the protected /private/files route, not by a different Unix mode.
            os.chmod(temporary,0o644)
            while content:=source.read(1024*1024):
                target.write(content);sha.update(content);md5.update(content);size+=len(content)
            target.flush();os.fsync(target.fileno())
        digest=sha.hexdigest()
        if expected_sha and digest!=expected_sha:
            raise ValueError('Historical attachment changed while being copied.')
        suffix=Path(original_name).suffix
        # Keep user-readable names in File.file_name; disk names are unambiguous
        # even for source names that differ only in case or Unicode spelling.
        if not suffix.isascii() or not suffix[1:].isalnum() or len(suffix)>12:suffix=''
        filename='vmms-'+digest+suffix.lower()
        destination=directory/filename
        if destination.exists():
            if destination.is_symlink():raise ValueError('Migration attachment destination is a symbolic link.')
            from onerc_vmms_migrate.bulk_source import sha256_file
            if sha256_file(destination)!=digest:
                raise ValueError('A destination attachment has changed. Existing bytes were not overwritten.')
            temporary.unlink()
        else:
            os.replace(temporary,destination)
            created=True
        url=('/private/files/' if private else '/files/')+filename
        file=frappe.get_doc({'doctype':'File','file_name':os.path.basename(original_name),
            'file_url':url,'is_private':int(private),'attached_to_doctype':doctype,'attached_to_name':name,
            'file_size':size,'content_hash':md5.hexdigest()})
        file.flags.copy_from_existing_file=True
        file.insert(ignore_permissions=True)
        if created:
            def rollback():
                # This transaction created the blob; a full rollback removes all
                # File references created in the same transaction as well.
                if destination.exists():destination.unlink()
            frappe.db.after_rollback.add(rollback)
        return file.file_url
    except Exception:
        if temporary.exists():temporary.unlink()
        if created and destination is not None and destination.exists():destination.unlink()
        raise
