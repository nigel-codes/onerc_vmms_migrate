"""An old partial capture cannot become complete merely by updating the receiver."""
REQUIRED = {'User', 'Version', 'Activity Log', 'Access Log', 'Workflow Action', 'Deleted Document',
            'Mpesa Express Request', 'Mpesa C2B Payment Register', 'B2C Disbursement Request',
            'B2C Payment Disbursement', 'B2C Payment Disbursement Reference', 'GL Entry', 'DocShare', 'User Permission'}


def require_capture(snapshot):
    manifest = snapshot.get('source', {}).get('continuity', {})
    if manifest.get('version') != 1 or not isinstance(manifest.get('tables'), dict):
        raise ValueError('Take a fresh continuity capture with both migration apps updated; an old partial capture cannot be imported.')
    missing = sorted(dt for dt in REQUIRED if 'tab' + dt in manifest['tables'] and dt not in snapshot.get('schemas', {}))
    if missing:
        raise ValueError('The continuity capture omitted required source history: ' + ', '.join(missing))
    return manifest
