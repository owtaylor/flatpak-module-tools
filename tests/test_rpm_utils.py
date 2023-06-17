from pathlib import Path
import subprocess
from unittest.mock import ANY

import pytest

from flatpak_module_tools.rpm_utils import create_rpm_manifest


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


def build_rpm(name, spec, path):
    specpath = path / f"{name}.spec"
    with open(path / f"{name}.spec", "w") as f:
        f.write(spec)

    subprocess.check_call([
        "rpmbuild", "--define", f"_rpmdir {path}", "-bb", specpath
    ])

    return path / "noarch" / f"{name}-1-1.noarch.rpm"


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
