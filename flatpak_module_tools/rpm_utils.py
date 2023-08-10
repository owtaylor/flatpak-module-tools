from dataclasses import dataclass
from functools import cached_property, total_ordering
from pathlib import Path
import re
from typing import Optional, Union

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
            if h['sigmd5'] is None:  # imported key rather than a package
                continue
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


STRIP_DISTTAG_RE = re.compile(r"(.*?).fc\d+(?:app)?$")


@total_ordering
@dataclass
class VersionInfo:
    epoch: Optional[str]
    version: str
    release: str

    @cached_property
    def stripped_release(self):
        m = STRIP_DISTTAG_RE.match(self.release)
        return m.group(1) if m else self.release

    def __init__(self, epoch: Union[str, int, None], version: str, release: str):
        if isinstance(epoch, int):
            self.epoch = str(epoch)
        else:
            self.epoch = epoch
        self.version = version
        self.release = release

    @staticmethod
    def from_dict(d):
        return VersionInfo(epoch=d["epoch"],
                           version=d["version"],
                           release=d["release"])

    def _to_tuple(self):
        return (self.epoch, self.version, self.stripped_release)

    def __eq__(self, other):
        return self._to_tuple() == other._to_tuple()

    def __ne__(self, other):
        return self._to_tuple() != other._to_tuple()

    def __lt__(self, other):
        return rpm.labelCompare(self._to_tuple(), other._to_tuple()) < 0  # type: ignore

    def __repr__(self):
        if self.epoch is not None:
            return f"{self.epoch}:{self.version}-{self.release}"
        else:
            return f"{self.version}-{self.release}"
