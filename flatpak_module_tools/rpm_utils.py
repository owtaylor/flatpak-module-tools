from pathlib import Path
from typing import Optional

import rpm


def _get_ts(root: Path):
    rpm.addMacro('_dbpath', str(root / "usr/lib/sysimage/rpm"))  # type: ignore
    ts = rpm.TransactionSet()  # type: ignore
    ts.openDB()
    rpm.delMacro('_dbpath')  # type: ignore

    return ts


def create_rpm_manifest(root: Path, restrict_to: Optional[Path] = None):
    if restrict_to:
        prefix = "/" + str(restrict_to.relative_to(root)) + "/"
    else:
        prefix = None

    ts = _get_ts(root)
    matched = []

    mi = ts.dbMatch()
    for h in mi:
        if prefix is None or any(d.startswith(prefix) for d in h['dirnames']):
            item = {
                'name': h['name'],
                'version': h['version'],
                'release': h['release'],
                'arch': h['arch'],
                'payloadhash': h['sigmd5'].hex(),
                'size': h['size'],
                'buildtime': h['buildtime']
            }

            if h['epoch'] is not None:
                item['epoch'] = h['epoch']

            matched.append(item)

    matched.sort(key=lambda i: (i['name'], i['arch']))
    return matched
