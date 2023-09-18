from enum import Enum, auto
from unittest.mock import patch

from flatpak_module_tools.config import Config


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


def make_config():
    config = Config()
    with patch("flatpak_module_tools.config.Config._iter_config_files"):
        config.read()

    for profile in config.profiles.values():
        profile.koji_options = {
            'topurl': 'https://kojifiles.example.com'
        }
        profile.koji_session = MockKojiSession()

    return config
