import os
from subprocess import check_call

import pytest
import yaml

from flatpak_module_tools.flatpak_builder import FlatpakBuilder, FlatpakSourceInfo, ModuleInfo
from flatpak_module_tools.module_locator import Modulemd


FLATPAK_RUNTIME_MMD = """
document: modulemd
version: 2
data:
  name: flatpak-runtime
  stream: f33
  version: 3320201217083338
  context: 601d93de
  summary: Flatpak Runtime
  description: >-
    This module defines two runtimes for Flatpaks, the 'runtime' profile that most
    Flatpaks in Fedora use, and a smaller 'runtime-base' profile that is intended
    to be more minimal and (slightly) more API stable. There are also corresponding
    sdk and sdk-base profiles that are used to build SDKs that applications can be
    built against with flatpak-builder.
  license:
    module:
    - MIT
  xmd:
    flatpak:
      branch: f33
      runtimes:
        runtime:
          id: org.fedoraproject.Platform
          sdk: org.fedoraproject.Sdk
        runtime-base:
          id: org.fedoraproject.BasePlatform
          sdk: org.fedoraproject.BaseSdk
        sdk:
          id: org.fedoraproject.Sdk
          runtime: org.fedoraproject.Platform
        sdk-base:
          id: org.fedoraproject.BaseSdk
          runtime: org.fedoraproject.BasePlatform
  dependencies:
  - buildrequires:
      platform: [f33]
    requires:
      platform: [f33]
  profiles:
    buildroot:
      rpms:
      - flatpak-rpm-macros
      - flatpak-runtime-config
    runtime:
      rpms:
      - flatpak-runtime-config
      - glibc
  api:
    rpms:
    - flatpak-rpm-macros
    - flatpak-runtime-config
  components:
    rpms:
      flatpak-rpm-macros:
        rationale: Set up build root for flatpak RPMS
        repository: git+https://src.fedoraproject.org/rpms/flatpak-rpm-macros
        ref: f33
      flatpak-runtime-config:
        rationale: Runtime configuration files
        repository: git+https://src.fedoraproject.org/rpms/flatpak-runtime-config
        ref: f33
"""


TESTAPP_MMD = """
document: modulemd
version: 2
data:
  name: testapp
  stream: stable
  version: 3320201216094032
  context: 50ef3cd5
  summary: Map application for GNOME
  description: >-
    GNOME Maps is a simple map application for the GNOME desktop.
  license:
    module:
    - MIT
  dependencies:
  - buildrequires:
      flatpak-common: [f33]
      flatpak-runtime: [f33]
      platform: [f33]
    requires:
      flatpak-common: [f33]
      flatpak-runtime: [f33]
      platform: [f33]
  profiles:
    default:
      rpms:
      - testapp
  components:
    rpms:
      testapp:
        rationale: Application package
        repository: git+https://src.fedoraproject.org/rpms/testapp
        cache: https://src.fedoraproject.org/repo/pkgs/testapp
        ref: f33
"""


TESTAPP_CONTAINER_YAML = """
compose:
    modules:
    - testapp:master
flatpak:
    id: org.fedoraproject.TestApp
    branch: stable
    command: testapp
    finish-args: |-
        --socket=fallback-x11
        --socket=wayland
"""


@pytest.fixture
def source():
    runtime_mmd = Modulemd.ModuleStream.read_string(FLATPAK_RUNTIME_MMD, True)
    runtime_module = ModuleInfo(runtime_mmd.get_module_name(),
                                runtime_mmd.get_stream_name(),
                                runtime_mmd.get_version(),
                                runtime_mmd,
                                [])

    testapp_mmd = Modulemd.ModuleStream.read_string(TESTAPP_MMD, True)
    testapp_module = ModuleInfo(testapp_mmd.get_module_name(),
                                testapp_mmd.get_stream_name(),
                                testapp_mmd.get_version(),
                                testapp_mmd,
                                [])

    container_yaml = yaml.safe_load(TESTAPP_CONTAINER_YAML)

    source = FlatpakSourceInfo(container_yaml['flatpak'],
                               [runtime_module, testapp_module],
                               testapp_module)

    yield source


def test_export_long_filenames(source, tmpdir):
    """
    Test for a bug where if the exported filesytem is in PAX format, then
    names/linknames that go from > 100 characters < 100 characters were
    not properly processed.
    """
    # len("verylongrootname/app/bin") = 24
    # len("files/bin") = 9
    #
    # We want names that go from > 100 characters to < 100 characters
    # so, e.g. 85 character name

    bindir = tmpdir / "verylongrootname/app/bin"
    os.makedirs(bindir)

    with open(bindir / "testapp", "w") as f:
        os.fchmod(f.fileno(), 0o0755)

    # When "verylongrootname/app/bin" => "files/bin", we reduce the
    # length of the filename from characters - we want to test a
    # name that goes from > 100 to < 100, so e.g., a 90 character
    # name in bindir

    verylongname = ("v" * 77) + "erylong"
    with open(bindir / verylongname, "w") as f:
        os.fchmod(f.fileno(), 0o0755)

    # Also try a name that stays > 100 characters
    veryverylongname = ("v" * 100) + "erylong"
    with open(bindir / veryverylongname, "w") as f:
        os.fchmod(f.fileno(), 0o0755)

    # We create hard links - sorting *after* vvv...vverylongname
    os.link(bindir / "testapp", bindir / "zzzlink")
    os.link(bindir / verylongname, bindir / "zzzlink2")
    os.link(bindir / veryverylongname, bindir / "zzzlink3")

    check_call(["tar", "cfv", "export.tar", "-H", "pax", "--sort=name",
                "verylongrootname"], cwd=tmpdir)

    workdir = str(tmpdir / "work")
    os.mkdir(workdir)

    builder = FlatpakBuilder(source, workdir, "verylongrootname")

    with open(tmpdir / "export.tar", "rb") as f:
        outfile, manifest_file = (builder._export_from_stream(f, close_stream=False))

    os.mkdir(tmpdir / "processed")
    check_call(["tar", "xfv", outfile], cwd=tmpdir / "processed")

    binfiles = sorted(os.listdir(tmpdir / "processed/files/bin"))
    assert binfiles == ["testapp", verylongname, veryverylongname,
                        "zzzlink", "zzzlink2", "zzzlink3"]
