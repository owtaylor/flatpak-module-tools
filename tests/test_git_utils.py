from pathlib import Path
import subprocess

import click
import pytest

from flatpak_module_tools.git_utils import GitRepository


@pytest.fixture(scope="session")
def source_repo_path(tmp_path_factory) -> Path:
    repo_path = tmp_path_factory.mktemp("src-repo")
    repo = GitRepository(repo_path)

    with open(repo_path / "README.md", "w") as f:
        f.write("Content!\n")

    repo._git_output(["init", "-b", "rawhide"])
    repo._git_output(["add", "README.md"])
    repo._git_output(["commit", "-m", "Initial import"])

    return repo_path


@pytest.fixture
def repo(tmp_path, source_repo_path) -> GitRepository:
    subprocess.check_call(["git", "clone", source_repo_path, "repo"], cwd=tmp_path)

    return GitRepository(tmp_path / "repo")


def test_git_repository_branch(repo: GitRepository):
    assert repo.branch == "rawhide"


def test_git_repository_branch_none(repo: GitRepository):
    with open(repo.path / "README.md", "w") as f:
        f.write("New contents")
    repo._git_output(["commit", "-a", "-m", "Update README"])
    repo._git_output(["checkout", "HEAD^"])

    with pytest.raises(click.ClickException, match=r"No current git branch"):
        repo.branch


def test_git_repository_merge_branch_none(repo: GitRepository):
    repo._git_output(["checkout", "-b", "new_branch", "HEAD"])

    with pytest.raises(click.ClickException, match=r"Can't find git remote tracking branch"):
        repo.merge_branch


def test_git_repository_merge_branch_bad(repo: GitRepository):
    repo._git_output(["config", "branch.rawhide.merge", "NOT_A_REF"])

    with pytest.raises(click.ClickException,
                       match=r"Can't parse git remote tracking branch NOT_A_REF"):
        repo.merge_branch


def test_git_repository_merge_branch(repo: GitRepository):
    repo._git_output(["checkout", "-b", "other", "origin/rawhide"])
    assert repo.branch == "other"
    assert repo.merge_branch == "rawhide"


def test_git_repository_origin_url(repo: GitRepository, source_repo_path: Path):
    assert repo.origin_url == str(source_repo_path)


def test_git_repository_check_clean(repo: GitRepository):
    repo.check_clean()

    with open(repo.path / "README.md", "w") as f:
        f.write("New contents")

    repo = GitRepository(repo.path)  # reset cached properties
    with pytest.raises(click.ClickException,
                       match=r"Git repository has uncommitted changes"):
        repo.check_clean()

    repo._git_output(["commit", "-a", "-m", "Update README"])

    repo = GitRepository(repo.path)  # reset cached properties
    with pytest.raises(click.ClickException,
                       match=r"HEAD does not match origin/rawhide. Unpushed changes?"):
        repo.check_clean()
