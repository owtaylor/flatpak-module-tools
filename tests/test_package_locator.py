from io import BufferedReader, BytesIO, RawIOBase
import gzip
import subprocess
from typing import Optional

import responses
import pytest

from flatpak_module_tools.package_locator import PackageLocator, ExtendedVersionInfo
from flatpak_module_tools.utils import Arch

from .build_rpm import build_rpm

BASIC_REPO = """\
[basic]
name=Fedora $releasever - $basearch
baseurl=https://repos.example.com/basic/$basearch/
enabled=1
type=rpm

[basic-debuginfo]
name=Fedora $releasever - $basearch
baseurl=https://repos.example.com/basic-debuginfo/$basearch/
enabled=0
type=rpm
"""


BASIC_NO_BASEURL_REPO = """\
[basic]
name=Fedora $releasever - $basearch
metalink=https://mirrors.example.com/basic?arch=$basearch
enabled=1
type=rpm
"""


BASIC_REPODATA_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo">
  <data type="primary">
    <location href="repodata/HASH-primary.xml.gz"/>
  </data>
</repomd>
"""


BASIC_REPODATA_BAD_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo">
</repomd>
"""


BASIC_PRIMARY_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<metadata xmlns="http://linux.duke.edu/metadata/common">
<package type="rpm">
  <name>glib2</name>
  <arch>ppc64le</arch>
  <version epoch="0" ver="2.3.4" rel="1.fc38"/>
</package>
<package type="rpm">
  <name>glib2</name>
  <arch>ppc64le</arch>
  <version epoch="0" ver="2.3.5" rel="1.fc38"/>
</package>
<package type="rpm">
  <name>glib2</name>
  <arch>ppc64le</arch>
  <version epoch="0" ver="2.3.6" rel="1.fc38"/>
</package>
<package type="rpm">
  <name>glib3</name>
  <arch>ppc64le</arch>
  <version epoch="0" ver="3.0.0" rel="1.fc38"/>
</package>
</metadata>
"""


# responses looks for specifically for BufferedReader subclass for the body to
# know if to treat it as a file. BytesIO acts as a buffered reader, but is not
# a subclass, so this class pretends it's unbuffered so we can wrap it in a
# BufferedReader. Bah...

class RawBytesReader(RawIOBase):
    def __init__(self, data):
        self._reader = BytesIO(data)

    def read(self, size=-1) -> bytes:
        return self._reader.read(size)

    def readinto(self, __buffer) -> Optional[int]:
        return self._reader.readinto(__buffer)

    def readable(self) -> bool:
        return True


class StreamingGzippedResponse(responses.CallbackResponse):
    def __init__(self, method, url, body_str: str, **kwargs):
        compressed_data = gzip.compress(body_str.encode("UTF-8"))

        def callback(request):
            headers = {
                "Content-Type": "text/xml"
            }
            return (200, headers, BufferedReader(RawBytesReader(compressed_data)))

        super().__init__(method=method, url=url, callback=callback, stream=True, **kwargs)


@responses.activate
def test_package_locator():
    responses.add(
        responses.GET, "https://repos.example.com/basic.repo",
        body=BASIC_REPO
    )
    responses.add(
        responses.GET, "https://repos.example.com/basic-no-baseurl.repo",
        body=BASIC_NO_BASEURL_REPO
    )
    responses.add(
        responses.GET, "https://repos.example.com/basic/ppc64le/repodata/repomd.xml",
        body=BASIC_REPODATA_XML
    )
    responses.add(
        responses.GET, "https://repos.example.com/basic-bad/ppc64le/repodata/repomd.xml",
        body=BASIC_REPODATA_BAD_XML
    )
    responses.add(StreamingGzippedResponse(
        responses.GET, "https://repos.example.com/basic/ppc64le/repodata/HASH-primary.xml.gz",
        body_str=BASIC_PRIMARY_XML
    ))

    # basic operation - find highest version
    locator = PackageLocator()
    locator.add_remote_repofile("https://repos.example.com/basic.repo")
    ver = locator.find_latest_version("glib2", arch=Arch.PPC64LE)
    assert ver and ver.version == "2.3.6"

    # basic operation - no version found
    ver = locator.find_latest_version("glib4", arch=Arch.PPC64LE)
    assert ver is None

    # Use baseurl input rather than a repo URL
    locator = PackageLocator()
    locator.add_repo("https://repos.example.com/basic/$basearch")
    ver = locator.find_latest_version("glib2", arch=Arch.PPC64LE)
    assert ver and ver.version == "2.3.6"

    # Test repo with a proxy setting
    locator = PackageLocator()
    locator.add_repo("https://repos.example.com/basic/$basearch",
                     proxy="https://proxy.example.com/")
    ver = locator.find_latest_version("glib2", arch=Arch.PPC64LE)
    assert ver and ver.version == "2.3.6"
    # No easy way to check that the proxies argument actually got used; it's not
    # reflected in responses.calls[-1].request.

    # Bad repomd.xml
    with pytest.raises(RuntimeError, match=r"Cannot find <data type='primary'/> in repomd.xml"):
        locator = PackageLocator()
        locator.add_repo("https://repos.example.com/basic-bad/$basearch/")
        locator.find_latest_version("glib2", arch=Arch.PPC64LE)

    # Handling when there's no baseurl in the repository definition
    with pytest.raises(
        RuntimeError,
        match=r"https://repos.example.com/basic-no-baseurl.repo: "
            r"Repository \[basic\] has no baseurl set"
    ):
        locator = PackageLocator()
        locator.add_remote_repofile("https://repos.example.com/basic-no-baseurl.repo")
        locator.find_latest_version("glib2", arch=Arch.PPC64LE)


@responses.activate
def test_package_locator_local(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    build_rpm(repo, name="glib2", version="2.3.4", release="1.fc38")
    subprocess.check_call(["createrepo_c", repo])

    locator = PackageLocator()
    locator.add_repo(repo)
    ver = locator.find_latest_version("glib2", arch=Arch.PPC64LE)

    assert ver and ver.version == "2.3.4"


def test_extended_version_info():
    vi1 = ExtendedVersionInfo(priority=10, epoch=None, version="1.2.3", release="1")
    vi2 = ExtendedVersionInfo(priority=20, epoch=None, version="1.2.3", release="1")

    assert vi1 > vi2
    assert vi1 != vi2
    assert not vi1 == vi2

    vi3 = ExtendedVersionInfo(priority=10, epoch=None, version="1.2.3", release="1")
    vi4 = ExtendedVersionInfo(priority=10, epoch=None, version="1.2.3", release="2")

    assert vi3 < vi4
    assert vi3 != vi4
    assert not vi3 == vi4

    assert repr(vi1) == "1.2.3-1, priority=10"
