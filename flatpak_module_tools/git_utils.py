from functools import cached_property
from pathlib import Path
import subprocess

import click


class GitRepository:
    def __init__(self, path: Path):
        self.path = path

    def _git_output(self, cmd):
        return subprocess.check_output(["git"] + cmd, cwd=self.path, encoding="utf-8").strip()

    @cached_property
    def branch(self):
        branch = self._git_output(["branch", "--show-current"])
        if branch == "":
            raise click.ClickException("No current git branch")

        return branch

    @cached_property
    def merge_branch(self):
        try:
            merge = self._git_output(["config", f"branch.{self.branch}.merge"])
        except subprocess.CalledProcessError:
            raise click.ClickException("Can't find git remote tracking branch")

        if not merge.startswith("refs/heads/"):
            raise click.ClickException(f"Can't parse git remote tracking branch {merge}")

        return merge[len("refs/heads/"):]

    @cached_property
    def head_revision(self):
        return self._git_output(["rev-parse", "HEAD"])

    @cached_property
    def origin_url(self):
        return self._git_output(["remote", "get-url", "origin"])

    def check_clean(self):
        try:
            self._git_output(["diff-index", "--quiet", "HEAD", "--"])
        except subprocess.CalledProcessError:
            raise click.ClickException("Git repository has uncommitted changes")

        remote = self._git_output(["rev-parse", f"remotes/origin/{self.merge_branch}"])
        if self.head_revision != remote:
            raise click.ClickException(
                f"HEAD does not match origin/{self.merge_branch}. Unpushed changes?"
            )
