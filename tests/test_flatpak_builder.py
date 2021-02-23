import os
from subprocess import check_call
from textwrap import dedent

import gi
gi.require_version('Modulemd', '2.0')
from gi.repository import Modulemd

import pytest
import yaml

from flatpak_module_tools.flatpak_builder import (
    FlatpakBuilder, FlatpakSourceInfo, FLATPAK_METADATA_BOTH, get_rpm_arch, ModuleInfo
)


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
      - libfoo
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
    default-@ARCH@:
      rpms:
      - testapp-fancymath
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
    end-of-life: TestApp was replaced with NewApp
    end-of-life-rebase: org.fedoraproject.NewApp
    finish-args: |-
        --socket=fallback-x11
        --socket=wayland
"""


RUNTIME_CONTAINER_YAML = """
compose:
    modules:
    - flatpak-runtime:f33
flatpak:
    id: org.fedoraproject.Platform
    branch: stable
    end-of-life: Fedora 33 is no longer supported
    end-of-life-rebase: org.fedoraproject.NewPlatform
"""


@pytest.fixture
def runtime_module():
    runtime_mmd = Modulemd.ModuleStream.read_string(FLATPAK_RUNTIME_MMD, True)
    yield ModuleInfo(runtime_mmd.get_module_name(),
                     runtime_mmd.get_stream_name(),
                     runtime_mmd.get_version(),
                     runtime_mmd,
                     ['flatpak-rpm-macros-0:32-2.x86_64.rpm',
                      'flatpak-runtime-config-0:29-5.x86_64.rpm'])


@pytest.fixture
def testapp_module():
    testapp_mmd = TESTAPP_MMD.replace("@ARCH@",
                                      get_rpm_arch())

    testapp_mmd = Modulemd.ModuleStream.read_string(testapp_mmd, True)
    yield ModuleInfo(testapp_mmd.get_module_name(),
                     testapp_mmd.get_stream_name(),
                     testapp_mmd.get_version(),
                     testapp_mmd,
                     ['testapp-0:1-1.x86_64.rpm',
                      'testapp-fancymath-0:1-1.x86_64.rpm',
                      'libfoo-0:1.2.4-1.module_f33+11439+4b44cd2d.x86_64.rpm'])


@pytest.fixture
def testapp_source(testapp_module, runtime_module):
    container_yaml = yaml.safe_load(TESTAPP_CONTAINER_YAML)

    modules = {
        'flatpak-runtime': runtime_module,
        'testapp': testapp_module
    }

    yield FlatpakSourceInfo(container_yaml['flatpak'],
                            modules,
                            testapp_module)


@pytest.fixture
def runtime_source(runtime_module):
    container_yaml = yaml.safe_load(RUNTIME_CONTAINER_YAML)

    modules = {
        'flatpak-runtime': runtime_module,
    }

    yield FlatpakSourceInfo(container_yaml['flatpak'],
                            modules,
                            runtime_module)


def test_source_info_bad_profile(testapp_source):
    with pytest.raises(ValueError,
                       match=r"testapp:stable:3320201216094032 doesn't have a profile 'badprofile'"):
        FlatpakSourceInfo(testapp_source.flatpak_yaml,
                          testapp_source.modules,
                          testapp_source.base_module,
                          profile='badprofile')


def parse_manifest(lines):
    """A parse_manifest function to pass to FlatpackBuilder. The 'include' filed
    in the returned dictionary is not part of the FlatpakBuilder API ... it
    is used here for testing purposes (whether we expect the manifest entry
    to be in the result of FlatpakBuilder.get_components()"""

    components = []

    for line in lines:
        include, nevra = line.strip().split()
        assert include in ('true', 'false')

        name, ev, ra = nevra.rsplit('-', 2)
        epoch, version = ev.split(':', 1)
        release, arch = ra.rsplit('.', 1)

        components.append({
            'include': include == 'true',
            'name': name,
            'epoch': None if epoch == '(none)' else int(epoch),
            'version': version,
            'release': release,
            'arch': arch,
        })

    return components


def check_get_components(builder, tmpdir, manifest):
    manifestfile = tmpdir / "manifest"
    with open(manifestfile, "w") as f:
        f.write(manifest)

    manifest_lines = [l + "\n" for l in manifest.strip().split("\n")]

    expected_components = [c for c in parse_manifest(manifest_lines) if c['include']]
    print(expected_components)
    components = builder.get_components(manifestfile)

    def flatten(components):
        return set(f"{c['name']}-{c['epoch']}:{c['version']}-{c['release']}.{c['arch']}"
                   for c in components)

    assert flatten(components) == flatten(expected_components)


def test_app_basic(testapp_source, tmpdir):
    workdir = str(tmpdir / "work")
    os.mkdir(workdir)

    builder = FlatpakBuilder(testapp_source, workdir, "root",
                             parse_manifest=parse_manifest,
                             flatpak_metadata=FLATPAK_METADATA_BOTH)

    assert set(builder.get_install_packages()) == set([
        "testapp", "testapp-fancymath", "flatpak-runtime-config"
    ])
    assert set(builder.get_includepkgs()) == set([
        "glibc",
        "flatpak-runtime-config",
        "libfoo",
        "libfoo-0:1.2.4-1.module_f33+11439+4b44cd2d.x86_64",
        "testapp-0:1-1.x86_64",
        "testapp-fancymath-0:1-1.x86_64"
    ])

    bindir = tmpdir / "root/app/bin"
    os.makedirs(bindir)

    with open(bindir / "hello", "w") as f:
        os.fchmod(f.fileno(), 0o0755)

    check_call(["tar", "cfv", "export.tar", "-H", "pax", "--sort=name",
                "root"], cwd=tmpdir)

    with open(tmpdir / "export.tar", "rb") as f:
        outfile, manifest_file = (builder._export_from_stream(f, close_stream=False))

    check_call(["gzip", tmpdir / "export.tar"])

    builder.build_container(str(tmpdir / "export.tar.gz"))

    # libfoo from the module should be listed in builder.get_components()
    check_get_components(builder, tmpdir, dedent("""\
        false flatpak-runtime-config-0:29-5.x86_64
        false glibc-0:2.32-4.fc33.x86_64
        true  libfoo-0:1.2.4-1.module_f33+11439+4b44cd2d.x86_64
        true  testapp-0:1-1.x86_64
        true  testapp-fancymath-0:1-1.x86_64
    """))

    # But libfoo from the runtime should not
    check_get_components(builder, tmpdir, dedent("""\
        false flatpak-runtime-config-0:29-5.x86_64
        false glibc-0:2.32-4.fc33.x86_64
        false libfoo-0:1.2.3-1.fc33.x86_64
        true  testapp-0:1-1.x86_64
        true  testapp-fancymath-0:1-1.x86_64
    """))


def test_runtime_basic(runtime_source, tmpdir):
    workdir = str(tmpdir / "work")
    os.mkdir(workdir)

    builder = FlatpakBuilder(runtime_source, workdir, "root",
                             parse_manifest=parse_manifest,
                             flatpak_metadata=FLATPAK_METADATA_BOTH)

    assert set(builder.get_install_packages()) == set([
        "glibc", "flatpak-runtime-config", "libfoo"
    ])
    assert set(builder.get_includepkgs()) == set([
        "glibc", "flatpak-runtime-config", "libfoo"
    ])

    bindir = tmpdir / "root/usr/bin"
    os.makedirs(bindir)

    with open(bindir / "hello", "w") as f:
        os.fchmod(f.fileno(), 0o0755)

    check_call(["tar", "cfv", "export.tar", "-H", "pax", "--sort=name",
                "root"], cwd=tmpdir)

    with open(tmpdir / "export.tar", "rb") as f:
        outfile, manifest_file = (builder._export_from_stream(f, close_stream=False))

    check_call(["gzip", tmpdir / "export.tar"])

    builder.build_container(str(tmpdir / "export.tar.gz"))

    # builder.get_components() should not filter out any packages
    check_get_components(builder, tmpdir, dedent("""\
        true flatpak-runtime-config-0:29-5.x86_64
        true glibc-0:2.32-4.fc33.x86_64
        true libfoo-0:1.2.3-1.fc33.x86_64
    """))


def test_export_long_filenames(testapp_source, tmpdir):
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

    builder = FlatpakBuilder(testapp_source, workdir, "verylongrootname")

    with open(tmpdir / "export.tar", "rb") as f:
        outfile, manifest_file = (builder._export_from_stream(f, close_stream=False))

    os.mkdir(tmpdir / "processed")
    check_call(["tar", "xfv", outfile], cwd=tmpdir / "processed")

    binfiles = sorted(os.listdir(tmpdir / "processed/files/bin"))
    assert binfiles == ["testapp", verylongname, veryverylongname,
                        "zzzlink", "zzzlink2", "zzzlink3"]
