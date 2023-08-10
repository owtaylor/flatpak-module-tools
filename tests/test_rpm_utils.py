from pathlib import Path
import subprocess
from unittest.mock import ANY

import pytest

from flatpak_module_tools.rpm_utils import VersionInfo, create_rpm_manifest


TEMPLATE_SPEC = """
Name:           {name}
Version:        1
Release:        1
Summary:        Tools for maintaining Flatpak applications and runtimes as Fedora modules
{epoch}

License:        MIT

BuildArch:      noarch

%description
Very small RPM

%prep

%build

%install
mkdir -p %{{buildroot}}{prefix}/share/doc/{name}
echo "HELLO" > %{{buildroot}}{prefix}/share/doc/{name}/HELLO

%files
{prefix}/share/doc/{name}

%changelog
* Fri Jun 16 2023 Owen Taylor <otaylor@redhat.com - 1-1
- Created
"""

TESTRPM_SPEC = TEMPLATE_SPEC.format(name="testrpm", epoch="", prefix="/app")
TESTRPM_EPOCH_SPEC = TEMPLATE_SPEC.format(name="testrpm-epoch", epoch="Epoch: 1", prefix="/app")
TESTRPM_USR_SPEC = TEMPLATE_SPEC.format(name="testrpm-usr", epoch="", prefix="/usr")


FEDORA_GPG_KEY_RAWHIDE_X86_64 = """\
-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBGLykg8BEADURjKtgQpQNoluifXia+U3FuqGCTQ1w7iTqx1UvNhLX6tb9Qjy
l/vjl1iXxucrd2JBnrT/21BdtaABhu2hPy7bpcGEkG8MDinAMZBzcyzHcS/JiGHZ
d/YmMWQUgbDlApbxFSGWiXMgT0Js5QdcywHI5oiCmV0lkZ+khZ4PkVWmk6uZgYWf
JOG5wp5TDPnoYXlA4CLb6hu2691aDm9b99XYqEjhbeIzS9bFQrdrQzRMKyzLr8NW
s8Pq2tgyzu8txlWdBXJyAMKldTPstqtygLL9UUdo7CIQQzWqeDbAnv+WdOmiI/hR
etbbwNV+thkLJz0WD90C2L3JEeUJX5Qa4oPvfNLDeCKmJFEFUTCEdm0AYoQDjLJQ
3d3q9M09thXO/jYM0cSnJDclssLNsNWfjJAerLadLwNnYRuralw7f74QSLYdJAJU
SFShBlctWKnlhQ7ehockqtgXtWckkqPZZjGiMXwHde9b9Yyi+VqtUQWxSWny+9g9
6tcoa3AdnmpqSTHQxYajD0EGXJ0z0NXfqxkI0lo8UxzypEBy4sARZ4XhTU73Zwk0
LGhEUHlfyxXgRs6RRvM2UIoo+gou2M9rn/RWkhuHJNSfgrM0BmIBCjhjwGiS33Qh
ysLDWJMdch8lsu1fTmLEFQrOB93oieOJQ0Ysi5gQY8TOT+oZvVi9pSMJuwARAQAB
tDFGZWRvcmEgKDM5KSA8ZmVkb3JhLTM5LXByaW1hcnlAZmVkb3JhcHJvamVjdC5v
cmc+iQJOBBMBCAA4FiEE6PI5lvIyGGQMtEy+dc9axBi450wFAmLykg8CGw8FCwkI
BwIGFQoJCAsCBBYCAwECHgECF4AACgkQdc9axBi450yd4w//ZtghbZX5KFstOdBS
rcbBfCK9zmRvzeejzGl6lPKfqwx7OOHYxFlRa9MYLl8QG7Aq6yRRWzzEHiSb0wJw
WXz5tbkAmV/fpS4wnb3FDArD44u317UAnaU+UlhgK1g62lwI2dGpvTSvohMBMeBY
B5aBd+sLi3UtiSRM2XhxvxaWwr/oFLjKDukgrPQzeV3F/XdxGhSz/GZUVFVprcrB
h/dIo4k0Za7YVRhlVM0coOIcKbcjxAK9CCZ8+jtdIh3/BN5zJ0RFMgqSsrWYWeft
BI3KWLbyMfRwEtp7xSi17WXbRfsSoqwIVgP+RCSaAdVuiYs/GCRsT3ydYcDvutuJ
YZoE53yczemM/1HZZFI04zI7KBsKm9NFH0o4K2nBWuowBm59iFvWHFpX6em54cq4
45NwY01FkSQUqntfqCWFSowwFHAZM4gblOikq2B5zHoIntCiJlPGuaJiVSw9ZpEc
+IEQfmXJjKGSkMbU9tmNfLR9skVQJizMTtoUQ12DWC+14anxnnR2hxnhUDAabV6y
J5dGeb/ArmxQj3IMrajdNwjuk9GMeMSSS2EMY8ryOuYwRbFhBOLhGAnmM5OOSUxv
A4ipWraXDW0bK/wXI7yHMkc6WYrdV3SIXEqJBTp7npimv3JC+exWEbTLcgvV70FP
X55M9nDtzUSayJuEcfFP2c9KQCE=
=J4qZ
-----END PGP PUBLIC KEY BLOCK-----
"""


def build_rpm(name, spec, path):
    specpath = path / f"{name}.spec"
    with open(path / f"{name}.spec", "w") as f:
        f.write(spec)

    # We need to define _rpmfilename, since otherwise we'll pick up a value from
    # the system macros, which could be different (when building in Koji, for example)
    subprocess.check_call([
        "rpmbuild",
        "--define", f"_rpmdir {path}",
        "--define", "_rpmfilename %{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}.rpm",
        "-bb", specpath
    ])

    return path / f"{name}-1-1.noarch.rpm"


@pytest.fixture(scope='module')
def rpmroot(tmp_path_factory):
    parent = tmp_path_factory.mktemp('rpmroot')

    with open(parent / "testrpm.spec", "w") as f:
        f.write(TESTRPM_SPEC)

    root = parent / "root"

    testrpm = build_rpm("testrpm", TESTRPM_SPEC, parent)
    testrpm_epoch = build_rpm("testrpm-epoch", TESTRPM_EPOCH_SPEC, parent)
    testrpm_usr = build_rpm("testrpm-usr", TESTRPM_USR_SPEC, parent)

    subprocess.check_call([
        "rpm", "--root", root, "-Uvh", testrpm, testrpm_epoch, testrpm_usr
    ])

    # We don't necessarily expect GPG keys to be imported into the roots we
    # create, but if they are, we don't want them to break the manifest creation
    gpg_key = Path("/tmp") / "RPM-GPG-KEY-fedora-rawhide-x86_64"
    with open(gpg_key, "w") as f:
        f.write(FEDORA_GPG_KEY_RAWHIDE_X86_64)

    subprocess.check_call([
        "rpm", "--root", root, "--import", gpg_key
    ])

    return root


def test_create_rpm_manifest(rpmroot: Path):
    assert [x.name for x in (rpmroot / "app/share/doc").iterdir()] == ['testrpm', 'testrpm-epoch']
    assert [x.name for x in (rpmroot / "usr/share/doc").iterdir()] == ['testrpm-usr']

    rpmlist = create_rpm_manifest(rpmroot)

    assert rpmlist == [{
        'name': 'testrpm',
        'version': "1",
        'release': "1",
        'arch': "noarch",
        'payloadhash': ANY,
        'size': ANY,
        'buildtime': ANY
    }, {
        'name': 'testrpm-epoch',
        'epoch': 1,
        'version': "1",
        'release': "1",
        'arch': "noarch",
        'payloadhash': ANY,
        'size': ANY,
        'buildtime': ANY
    }, {
        'name': 'testrpm-usr',
        'version': "1",
        'release': "1",
        'arch': "noarch",
        'payloadhash': ANY,
        'size': ANY,
        'buildtime': ANY
    }]

    assert isinstance(rpmlist[0]['payloadhash'], str)
    assert len(rpmlist[0]['payloadhash']) == 32
    assert isinstance(rpmlist[0]['size'], int)
    assert isinstance(rpmlist[0]['buildtime'], int)

    rpmlist = create_rpm_manifest(rpmroot, restrict_to=rpmroot / "app")
    assert [i['name'] for i in rpmlist] == ['testrpm', 'testrpm-epoch']


def test_version_info():
    v1 = VersionInfo(None, "1.2", "3.fc28")
    v2 = VersionInfo(None, "1.2", "3.fc29")
    v3 = VersionInfo(None, "1.3", "3")

    assert v1 == v2
    assert v2 != v3
    assert v2 < v3

    assert repr(VersionInfo(None, "1.2", "3.fc28")) == "1.2-3.fc28"
    assert repr(VersionInfo(1, "1.2", "3.fc28")) == "1:1.2-3.fc28"

    assert VersionInfo("1", "1.2", "3") == VersionInfo(1, "1.2", "3")

    assert VersionInfo.from_dict({
        "epoch": 1,
        "version": "1.2",
        "release": "3"
    }) == VersionInfo(1, "1.2", "3")
