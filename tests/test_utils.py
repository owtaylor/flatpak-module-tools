import os

import pytest

from flatpak_module_tools.utils import atomic_writer


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
