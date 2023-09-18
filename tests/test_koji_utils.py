from textwrap import dedent
import pytest
from flatpak_module_tools.config import ProfileConfig
from flatpak_module_tools.koji_utils import KojiRepo

from .mock_koji import ID, make_config


@pytest.fixture
def profile():
    return make_config().profiles["production"]


def test_repo(profile: ProfileConfig):
    repo = KojiRepo(
        profile=profile,
        id="latest",
        tag_name="f39-flatpak-app-build",
        dist=False
    )

    assert repo.baseurl == \
        "https://kojifiles.example.com/repos/f39-flatpak-app-build/latest/$basearch/"

    assert repo.dnf_config(priority=10, includepkgs=["glibc", "tzdata"]) == dedent("""\
        [f39-flatpak-app-build]
        name=f39-flatpak-app-build
        baseurl=https://kojifiles.example.com/repos/f39-flatpak-app-build/latest/$basearch/
        enabled=1
        skip_if_unavailable=False
        priority=10
        includepkgs=glibc,tzdata
    """)


def test_repo_dist(profile: ProfileConfig):
    repo = KojiRepo(
        profile=profile,
        id="latest",
        tag_name="f39-flatpak-app-build",
        dist=True
    )

    assert repo.baseurl == \
        "https://kojifiles.example.com/repos-dist/f39-flatpak-app-build/latest/$basearch/"

    assert repo.dnf_config(priority=10, includepkgs=["glibc", "tzdata"]) == dedent("""\
        [f39-flatpak-app-build]
        name=f39-flatpak-app-build
        baseurl=https://kojifiles.example.com/repos-dist/f39-flatpak-app-build/latest/$basearch/
        enabled=1
        skip_if_unavailable=False
        priority=10
        includepkgs=glibc,tzdata
    """)


def test_repo_from_koji_repo_id(profile: ProfileConfig):
    repo = KojiRepo.from_koji_repo_id(profile, ID.REPO_F39_FLATPAK_APP_PACKAGES)

    expected_baseurl = (
        "https://kojifiles.example.com/repos/"
        "f39-flatpak-app-packages/ID.REPO_F39_FLATPAK_APP_PACKAGES/$basearch/"
    )

    assert repo.dnf_config() == dedent(f"""\
        [f39-flatpak-app-packages]
        name=f39-flatpak-app-packages
        baseurl={expected_baseurl}
        enabled=1
        skip_if_unavailable=False
    """)
