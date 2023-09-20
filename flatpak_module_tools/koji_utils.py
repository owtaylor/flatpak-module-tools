from dataclasses import dataclass
from textwrap import dedent
import time
from typing import List, Optional, TextIO

import click
import koji

from .config import ProfileConfig
from .console_logging import LiveDisplay, RenderWhen
from .utils import error


@dataclass
class KojiRepo:
    profile: ProfileConfig
    id: str
    tag_name: str
    dist: bool

    @property
    def baseurl(self) -> str:
        pathinfo = koji.PathInfo(topdir=self.profile.koji_options['topurl'])

        if self.dist:
            return pathinfo.distrepo(self.id, self.tag_name, None) + "/$basearch/"
        else:
            return pathinfo.repo(self.id, self.tag_name) + "/$basearch/"

    def dnf_config(self, priority: Optional[int] = None, includepkgs: Optional[List[str]] = None):
        result = dedent(f"""\
            [{self.tag_name}]
            name={self.tag_name}
            baseurl={self.baseurl}
            enabled=1
            skip_if_unavailable=False
        """)

        if priority is not None:
            result += dedent(f"""\
                priority={priority}
        """)

        if includepkgs is not None:
            result += dedent(f"""\
                includepkgs={",".join(includepkgs)}
            """)

        return result

    @classmethod
    def from_koji_repo_id(cls, profile: ProfileConfig, repo_id: int):
        repo_info = profile.koji_session.repoInfo(repo_id)
        return cls(
            profile=profile,
            id=repo_info["id"],
            tag_name=repo_info["tag_name"],
            dist=repo_info["dist"]
        )


def _format_link(href, text):
    OSC = "\033]"
    ST = "\033\\"
    return f"{OSC}8;;{href}{ST}{text}{OSC}8;;{ST}"


def format_task(profile: ProfileConfig, task_info):
    label = koji.taskLabel(task_info)
    state = koji.TASK_STATES[task_info["state"]].lower()

    if state == "failed" or state == "canceled":
        fg = "red"
    elif state == "closed":
        fg = "green"
    elif state == "open":
        fg = "yellow"
    else:
        fg = None

    formatted_state = click.style(state, fg=fg, bold=True)

    url_base = profile.koji_options['weburl']
    url = f"{url_base}/taskinfo?taskID={task_info['id']}"
    return f"{_format_link(url, task_info['id'])} {label}: {formatted_state}"


class WatcherDisplay(LiveDisplay):
    def __init__(self, profile: ProfileConfig, task_id: int):
        super().__init__()

        self.profile = profile
        self.task_id = task_id
        self.task_info = None
        self.task_children = []

    def query(self):
        session = self.profile.koji_session

        self.task_info = session.getTaskInfo(self.task_id, request=True)
        self.task_children = session.getTaskChildren(self.task_id, request=True)

    def render(self, stream: TextIO, when: RenderWhen):
        if not self.task_info:
            return

        print(format_task(self.profile, self.task_info), file=stream)
        for child in self.task_children:
            print("    " + format_task(self.profile, child), file=stream)


def watch_koji_task(profile: ProfileConfig, task_id: int):
    with WatcherDisplay(profile, task_id) as display:
        while True:
            display.query()
            display.update()

            assert display.task_info
            state = koji.TASK_STATES[display.task_info['state']]

            if state == "FAILED" or state == "CANCELLED" or state == "CLOSED":
                break

            time.sleep(20)

    click.echo()
    if state == "FAILED":
        error("Build failed")
        return False
    elif state == "CANCELLED":
        error("Build was cancelled")
        return False
    elif state == "CLOSED":
        builds = profile.koji_session.listBuilds(taskID=task_id)
        if builds:  # no builds for scratch build
            build = builds[0]

            url_base = profile.koji_options['weburl']
            url = f"{url_base}/buildinfo?buildID={build['build_id']}"
            click.echo(f"Building {_format_link(url, build['nvr'])} succeeded!")
        else:
            click.echo("Build succeeded!")

        return True
    else:
        assert False
