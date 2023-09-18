from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from click import ClickException
import pytest

from flatpak_module_tools.build_context import AutoBuildContext, ManualBuildContext
from flatpak_module_tools.config import ProfileConfig
from flatpak_module_tools.container_spec import ContainerSpec
from flatpak_module_tools.package_locator import VersionInfo
from flatpak_module_tools.utils import Arch

from .mock_koji import ID, MockKojiSession, make_config


APP_CONTAINER_YAML = """\
flatpak:
    name: eog
    id: org.gnome.eog
    branch: stable
    runtime-name: flatpak-runtime
    runtime-version: f39
    packages:
    - eog
    command: eog
    rename-appdata-file: eog.appdata.xml
    finish-args: |-
        --share=ipc
        --socket=fallback-x11
        --socket=wayland
        --filesystem=host
        --metadata=X-DConf=migrate-path=/org/gnome/eog/
        --talk-name=org.gtk.vfs.*
        --filesystem=xdg-run/gvfsd
        --filesystem=xdg-run/gvfs:ro
        --env=GDK_PIXBUF_MODULE_FILE=/app/lib64/gdk-pixbuf-2.0/2.10.0/loaders.cache
    cleanup-commands: |
        GDK_PIXBUF_MODULEDIR=/app/lib64/gdk-pixbuf-2.0/2.10.0/loaders/ \
            gdk-pixbuf-query-loaders-64 > /app/lib64/gdk-pixbuf-2.0/2.10.0/loaders.cache
        gdk-pixbuf-query-loaders-64 >> /app/lib64/gdk-pixbuf-2.0/2.10.0/loaders.cache
"""


RUNTIME_CONTAINER_YAML = """\
flatpak:
    id: org.fedoraproject.Platform
    build-runtime: true
    name: f39/flatpak-runtime
    component: flatpak-runtime
    branch: f39
    sdk: org.fedoraproject.Sdk
    finish-args: >
        --env=LD_LIBRARY_PATH=/app/lib64
    packages: [glibc]
"""


RUNTIME_NVR = "flatpak-runtime-f39-1"
APP_NVR = "eog-flatpak-44.2-1"
APP_VERSION_INFO = VersionInfo(epoch=None, version="44.2", release="1")


@pytest.fixture
def profile():
    return make_config().profiles["production"]


@pytest.fixture
def app_container_spec(tmp_path):
    with open(tmp_path / "container.yaml", "w") as f:
        f.write(APP_CONTAINER_YAML)

    return ContainerSpec(tmp_path / "container.yaml")


@pytest.fixture
def runtime_container_spec(tmp_path):
    with open(tmp_path / "container.yaml", "w") as f:
        f.write(RUNTIME_CONTAINER_YAML)

    return ContainerSpec(tmp_path / "container.yaml")


def test_manual_build_context(app_container_spec, profile: ProfileConfig):
    context = ManualBuildContext(profile=profile, container_spec=app_container_spec,
                                 nvr=APP_NVR, runtime_nvr=RUNTIME_NVR,
                                 runtime_repo=ID.REPO_F39_FLATPAK_RUNTIME_PACKAGES,
                                 app_repo=ID.REPO_F39_FLATPAK_APP_PACKAGES,
                                 arch=Arch.PPC64LE)

    assert context.runtime_package_repo.id == ID.REPO_F39_FLATPAK_RUNTIME_PACKAGES
    assert context.runtime_package_repo.tag_name == "f39-flatpak-runtime-packages"
    assert context.app_package_repo.id == ID.REPO_F39_FLATPAK_APP_PACKAGES
    assert context.app_package_repo.tag_name == "f39-flatpak-app-packages"

    assert context.release == "39"

    with pytest.raises(NotImplementedError):
        context.app_build_repo

    assert context.runtime_archive["id"] == ID.ARCHIVE_FLATPAK_RUNTIME_PPC64LE

    frp_baseurl = "https://kojifiles.example.com/repos/" + \
        "f39-flatpak-runtime-packages/ID.REPO_F39_FLATPAK_RUNTIME_PACKAGES/$basearch/"
    fap_baseurl = "https://kojifiles.example.com/repos/" + \
        "f39-flatpak-app-packages/ID.REPO_F39_FLATPAK_APP_PACKAGES/$basearch/"
    assert context.get_repos(for_container=True) == [
        dedent(f"""\
            [f39-flatpak-runtime-packages]
            name=f39-flatpak-runtime-packages
            baseurl={frp_baseurl}
            enabled=1
            skip_if_unavailable=False
            priority=10
            includepkgs=glibc
            """),
        dedent(f"""\
            [f39-flatpak-app-packages]
            name=f39-flatpak-app-packages
            baseurl={fap_baseurl}
            enabled=1
            skip_if_unavailable=False
            priority=20
            """)]

    with pytest.raises(NotImplementedError):
        assert context.get_repos(for_container=False)


def test_auto_build_context_app(app_container_spec, profile: ProfileConfig):
    context = AutoBuildContext(profile=profile, container_spec=app_container_spec,
                               target="f39-flatpak-candidate",
                               local_repo=Path("ppc64le/rpms"),
                               arch=Arch.PPC64LE)

    with patch("flatpak_module_tools.package_locator.PackageLocator.find_latest_version",
               return_value=APP_VERSION_INFO):
        assert context.nvr == APP_NVR

    assert context.runtime_package_repo.id == "latest"
    assert context.runtime_package_repo.tag_name == "f39-flatpak-runtime-packages"
    assert context.app_package_repo.id == "latest"
    assert context.app_package_repo.tag_name == "f39-flatpak-app-packages"

    assert context.release == "39"

    assert context.get_repos(for_container=True) == [
        dedent("""\
            [f39-flatpak-runtime-packages]
            name=f39-flatpak-runtime-packages
            baseurl=https://kojifiles.example.com/repos/f39-flatpak-runtime-packages/latest/$basearch/
            enabled=1
            skip_if_unavailable=False
            priority=10
            includepkgs=glibc
            """),
        dedent("""\
            [f39-flatpak-app-packages]
            name=f39-flatpak-app-packages
            baseurl=https://kojifiles.example.com/repos/f39-flatpak-app-packages/latest/$basearch/
            enabled=1
            skip_if_unavailable=False
            priority=20
            """),
        dedent("""\
            [local]
            name=local
            priority=20
            baseurl=ppc64le/rpms
            enabled=1
            skip_if_unavailable=False
            """)
    ]

    assert context.get_repos(for_container=False) == [
        dedent("""\
            [f39-flatpak-app-build]
            name=f39-flatpak-app-build
            baseurl=https://kojifiles.example.com/repos/f39-flatpak-app-build/latest/$basearch/
            enabled=1
            skip_if_unavailable=False
            priority=20
            """),
        dedent("""\
            [local]
            name=local
            priority=20
            baseurl=ppc64le/rpms
            enabled=1
            skip_if_unavailable=False
            """)
    ]


def test_auto_build_context_runtime(runtime_container_spec, profile: ProfileConfig):
    context = AutoBuildContext(profile=profile, container_spec=runtime_container_spec,
                               target="f39-flatpak-candidate", arch=Arch.PPC64LE)

    assert context.nvr == RUNTIME_NVR
    assert context.runtime_package_repo.id == "latest"
    assert context.runtime_package_repo.tag_name == "f39-flatpak-runtime-packages"

    assert context.release == "39"

    assert context.get_repos(for_container=True) == [
        dedent("""\
            [f39-flatpak-runtime-packages]
            name=f39-flatpak-runtime-packages
            baseurl=https://kojifiles.example.com/repos/f39-flatpak-runtime-packages/latest/$basearch/
            enabled=1
            skip_if_unavailable=False
            priority=10
            """)
    ]

    with pytest.raises(NotImplementedError, match=r"Runtime package building is not implemented"):
        context.get_repos(for_container=False)


def test_auto_build_context_bad_target(app_container_spec, profile: ProfileConfig):
    context = AutoBuildContext(profile=profile, container_spec=app_container_spec,
                               target="f39-flatpak-candidate", arch=Arch.PPC64LE)
    with patch.object(MockKojiSession, "getBuildConfig", return_value={
        "name": "f39-flatpak-container-build",
        "extra": {
            "name": "f39-flatpak-container-build",
            "flatpak.runtime_tag": "f39-flatpak-updates-candidate",
            # "flatpak.app_package_tag": "f39-flatpak-updates-candidate",
            "flatpak.runtime_package_tag": "f39-flatpak-runtime-packages",
        }
    }):
        with pytest.raises(
            ClickException,
            match=(r"f39-flatpak-container-build doesn't have "
                   r"flatpak.app_package_tag set in extra data")
        ):
            context.app_package_repo


def test_auto_build_context_no_app_package(app_container_spec, profile: ProfileConfig):
    context = AutoBuildContext(profile=profile, container_spec=app_container_spec,
                               target="f39-flatpak-candidate", arch=Arch.PPC64LE)
    with patch("flatpak_module_tools.package_locator.PackageLocator.find_latest_version",
               return_value=None):
        with pytest.raises(
            ClickException,
            match=r"Can't find build for eog in f39-flatpak-app-packages"
        ):
            context.nvr


def test_auto_build_context_no_runtime(app_container_spec, profile: ProfileConfig):
    context = AutoBuildContext(profile=profile, container_spec=app_container_spec,
                               target="f39-flatpak-candidate", arch=Arch.PPC64LE)
    with patch.object(MockKojiSession, "getBuildConfig", return_value={
        "name": "f39-flatpak-container-build",
        "extra": {
            "flatpak.runtime_tag": "f39-flatpak-app-packages",  # Intentionally broken
            "flatpak.app_package_tag": "f39-flatpak-app-packages",
            "flatpak.runtime_package_tag": "f39-flatpak-runtime-packages",
        }
    }):
        with pytest.raises(
            ClickException,
            match=r"Can't find build for flatpak-runtime in f39-flatpak-app-packages"
        ):
            context.runtime_info.version
