import os
from unittest.mock import patch

import pytest

from flatpak_module_tools.utils import Arch, atomic_writer, _get_rpm_arch


def test_arch():
    assert Arch(flatpak="testarch_flatpak") is Arch.TESTARCH
    assert Arch(oci="testarch_oci") is Arch.TESTARCH
    assert Arch(rpm="testarch_rpm") is Arch.TESTARCH

    assert Arch.TESTARCH.flatpak == "testarch_flatpak"
    assert Arch.TESTARCH.oci == "testarch_oci"
    assert Arch.TESTARCH.rpm == "testarch_rpm"

    _get_rpm_arch.cache_clear()
    with patch("subprocess.check_output", return_value="testarch_rpm"):
        assert Arch() == Arch(rpm="testarch_rpm")

    _get_rpm_arch.cache_clear()
    with pytest.raises(KeyError, match=r"Can't find Arch\(flatpak=X, oci=None, rpm=None\)"):
        Arch(flatpak="X")

    assert str(Arch.TESTARCH) == "Arch.TESTARCH"


def test_atomic_writer_basic(tmp_path):
    output_path = str(tmp_path / 'out.json')

    def expect(val):
        with open(output_path, "rb") as f:
            assert f.read() == val

    with atomic_writer(output_path) as writer:
        writer.write("HELLO")
    os.utime(output_path, (42, 42))
    expect(b"HELLO")

    with atomic_writer(output_path) as writer:
        writer.write("HELLO")
    expect(b"HELLO")
    assert os.stat(output_path).st_mtime == 42

    with atomic_writer(output_path) as writer:
        writer.write("GOODBYE")
    expect(b"GOODBYE")


def test_atomic_writer_write_failure(tmp_path):
    output_path = str(tmp_path / 'out.json')

    with pytest.raises(IOError):
        with atomic_writer(output_path) as writer:
            writer.write("HELLO")
            raise IOError()

    assert os.listdir(tmp_path) == []
