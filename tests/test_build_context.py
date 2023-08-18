from enum import Enum, auto
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from click import ClickException
import pytest

from flatpak_module_tools.build_context import AutoBuildContext, ManualBuildContext
from flatpak_module_tools.config import Config, ProfileConfig
from flatpak_module_tools.container_spec import ContainerSpec
from flatpak_module_tools.utils import Arch


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


class ID(int, Enum):
    ARCHIVE_FLATPAK_RUNTIME_PPC64LE = auto()
    BUILD_FLATPAK_RUNTIME = auto()
    REPO_F39_FLATPAK_APP_PACKAGES = auto()
    REPO_F39_FLATPAK_RUNTIME_PACKAGES = auto()
    TAG_F39_FLATPAK_APP_BUILD = auto()
    TAG_F39_FLATPAK_APP_PACKAGES = auto()
    TAG_F39_FLATPAK_CONTAINER_BUILD = auto()
    TAG_F39_FLATPAK_RUNTIME_PACKAGES = auto()
    TAG_F39_FLATPAK_UPDATES_CANDIDATE = auto()
    TARGET_F39_FLATPAK_CANDIDATE = auto()


RUNTIME_METADATA = """\
[Runtime]
name=org.fedoraproject.Platform
runtime=org.fedoraproject.Platform/aarch64/f39
sdk=org.fedoraproject.Sdk/aarch64/f39
"""

BUILDS = [{
    "build_id": ID.BUILD_FLATPAK_RUNTIME,
    "nvr": "flatpak-runtime-f39-1",
    "_archives": [{
        "id": ID.ARCHIVE_FLATPAK_RUNTIME_PPC64LE,
        "extra": {
            "docker": {
                "config": {
                    "config": {
                        "Labels": {
                            "org.flatpak.metadata": RUNTIME_METADATA,
                        }
                    }
                }
            },
            "image": {
                "arch": "ppc64le"
            }
        },
        "_rpms": [{
            "name": "glibc",
            "nvr": "glibc-2.37.9000-14.fc39"
        }]
    }]
}]


TAGS = [{
    "taginfo": {
        "id": ID.TAG_F39_FLATPAK_APP_BUILD,
        "name": "f39-flatpak-app-build",
    },
    "build_config": {
        "name": "f39-flatpak-app-build",
        "extra": {}
    }
}, {
    "taginfo": {
        "id": ID.TAG_F39_FLATPAK_APP_PACKAGES,
        "name": "f39-flatpak-app-packages",
    },
    "repo": {
        "id": ID.REPO_F39_FLATPAK_APP_PACKAGES,
        "tag_name": "f39-flatpak-app-packages",
        "dist": False,
    },
    "tagged": [{
        "name": "eog",
        "nvr": "44.2-4.fc39",
        "release": "4.fc39",
        "version": "44.2",
    }]
}, {
    "taginfo": {
        "id": ID.TAG_F39_FLATPAK_CONTAINER_BUILD,
        "name": "f39-flatpak-container-build",
    },
    "build_config": {
        "name": "f39-flatpak-container-build",
        "extra": {
            "flatpak.runtime_tag": "f39-flatpak-updates-candidate",
            "flatpak.app_package_tag": "f39-flatpak-app-packages",
            "flatpak.runtime_package_tag": "f39-flatpak-runtime-packages",
        }
    }
}, {
    "taginfo": {
        "id": ID.TAG_F39_FLATPAK_RUNTIME_PACKAGES,
        "name": "f39-flatpak-runtime-packages",
    },
    "repo": {
        "id": ID.REPO_F39_FLATPAK_RUNTIME_PACKAGES,
        "tag_name": "f39-flatpak-runtime-packages",
        "dist": False,
    },
}, {
    "taginfo": {
        "id": ID.TAG_F39_FLATPAK_UPDATES_CANDIDATE,
        "name": "f39-flatpak-updates-candidate",
    },
    "repo": {
        "id": ID.REPO_F39_FLATPAK_RUNTIME_PACKAGES,
        "tag_name": "f39-flatpak-runtime-packages",
    },
    "tagged": [{
        "build_id": ID.BUILD_FLATPAK_RUNTIME,
        "name": "flatpak-runtime",
        "nvr": "flatpak-runtime-f39-1",
        "release": "f39",
        "version": "1",
    }]
}]


TARGETS = [{
    "name": "f39-flatpak-candidate",
    "build_tag_name": "f39-flatpak-container-build",
}, {
    "name": "f39-flatpak-app",
    "build_tag_name": "f39-flatpak-app-build",
}]


class MockKojiSession:
    def _find_tag(self, name_or_id):
        for tag in TAGS:
            if (tag["taginfo"]["name"] == name_or_id or tag["taginfo"]["id"] == name_or_id):
                return tag
        raise RuntimeError(f"Unknown tag '{name_or_id}'")

    def repoInfo(self, repo_id):
        for tag in TAGS:
            if "repo" in tag and tag["repo"]["id"] == repo_id:
                return tag["repo"]
        raise RuntimeError(f"Unknown repo_id '{repo_id}'")

    def getBuild(self, id_or_nvr):
        for build in BUILDS:
            if build["build_id"] == id_or_nvr or build["nvr"] == id_or_nvr:
                return build
        raise RuntimeError(f"Unknown build '{id_or_nvr}'")

    def getBuildTarget(self, target_name):
        for target in TARGETS:
            if target["name"] == target_name:
                return target
        raise RuntimeError(f"Unknown target '{target_name}'")

    def getBuildConfig(self, tag_name):
        return self._find_tag(tag_name)["build_config"]

    def listArchives(self, buildID):
        for build in BUILDS:
            if build["build_id"] == buildID:
                return build["_archives"]

    def listRPMs(self, imageID):
        for build in BUILDS:
            for archive in build["_archives"]:
                if archive["id"] == imageID:
                    return archive["_rpms"]

    def listTagged(self, tag_name, package, latest=False, inherit=False):
        tag = self._find_tag(tag_name)
        return [b for b in tag["tagged"] if b["name"] == package]


@pytest.fixture
def config():
    config = Config()
    with patch("flatpak_module_tools.config.Config._iter_config_files"):
        config.read()

    for profile in config.profiles.values():
        profile.koji_options = {
            'topurl': 'https://kojifiles.example.com'
        }
        profile.koji_session = MockKojiSession()

    return config


@pytest.fixture
def profile(config: Config):
    return config.profiles["production"]


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

    assert context.runtime_package_repo["id"] == ID.REPO_F39_FLATPAK_RUNTIME_PACKAGES
    assert context.runtime_package_repo["tag_name"] == "f39-flatpak-runtime-packages"
    assert context.app_package_repo["id"] == ID.REPO_F39_FLATPAK_APP_PACKAGES
    assert context.app_package_repo["tag_name"] == "f39-flatpak-app-packages"

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
            priority=10
            enabled=1
            skip_if_unavailable=False
            includepkgs=glibc
            """),
        dedent(f"""\
            [f39-flatpak-app-packages]
            name=f39-flatpak-app-packages
            baseurl={fap_baseurl}
            priority=20
            enabled=1
            skip_if_unavailable=False
            """)]

    with pytest.raises(NotImplementedError):
        assert context.get_repos(for_container=False)


def test_auto_build_context_app(app_container_spec, profile: ProfileConfig):
    context = AutoBuildContext(profile=profile, container_spec=app_container_spec,
                               target="f39-flatpak-candidate", arch=Arch.PPC64LE)

    assert context.nvr == APP_NVR

    assert context.runtime_package_repo["id"] == "latest"
    assert context.runtime_package_repo["tag_name"] == "f39-flatpak-runtime-packages"
    assert context.app_package_repo["id"] == "latest"
    assert context.app_package_repo["tag_name"] == "f39-flatpak-app-packages"

    assert context.release == "39"

    assert context.get_repos(for_container=True, local_repo_path=Path("ppc64le/rpms")) == [
        dedent("""\
            [f39-flatpak-runtime-packages]
            name=f39-flatpak-runtime-packages
            baseurl=https://kojifiles.example.com/repos/f39-flatpak-runtime-packages/latest/$basearch/
            priority=10
            enabled=1
            skip_if_unavailable=False
            includepkgs=glibc
            """),
        dedent("""\
            [f39-flatpak-app-packages]
            name=f39-flatpak-app-packages
            baseurl=https://kojifiles.example.com/repos/f39-flatpak-app-packages/latest/$basearch/
            priority=20
            enabled=1
            skip_if_unavailable=False
            """),
        dedent("""\
            [local]
            name=local
            priority=0
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
            priority=20
            enabled=1
            skip_if_unavailable=False
            """)
    ]


def test_auto_build_context_runtime(runtime_container_spec, profile: ProfileConfig):
    context = AutoBuildContext(profile=profile, container_spec=runtime_container_spec,
                               target="f39-flatpak-candidate", arch=Arch.PPC64LE)

    assert context.nvr == RUNTIME_NVR
    assert context.runtime_package_repo["id"] == "latest"
    assert context.runtime_package_repo["tag_name"] == "f39-flatpak-runtime-packages"

    assert context.release == "39"

    assert context.get_repos(for_container=True) == [
        dedent("""\
            [f39-flatpak-runtime-packages]
            name=f39-flatpak-runtime-packages
            baseurl=https://kojifiles.example.com/repos/f39-flatpak-runtime-packages/latest/$basearch/
            priority=10
            enabled=1
            skip_if_unavailable=False
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
    with patch.object(MockKojiSession, "getBuildConfig", return_value={
        "name": "f39-flatpak-container-build",
        "extra": {
            "flatpak.runtime_tag": "f39-flatpak-updates-candidate",
            "flatpak.app_package_tag": "f39-flatpak-updates-candidate",  # Intentionally broken
            "flatpak.runtime_package_tag": "f39-flatpak-runtime-packages",
        }
    }):
        with pytest.raises(
            ClickException,
            match=r"Can't find build for eog in f39-flatpak-updates-candidate"
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
