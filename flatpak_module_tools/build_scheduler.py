from abc import ABC, abstractmethod
import asyncio
import copy
from dataclasses import dataclass, field
import functools
import shlex
import subprocess
import sys
import threading
import click
from enum import Enum
import logging
from pathlib import Path
import re
import shutil
from typing import Collection, Dict, Iterable, List, Mapping, Optional, Set, TextIO, Union

import koji
from koji_cli.lib import activate_session

from .config import ProfileConfig
from .console_logging import LiveDisplay, RenderWhen
from .koji_utils import format_link, format_task
from .utils import rpm_name_only


logger = logging.getLogger(__name__)


class State(Enum):
    WAITING = 1,
    READY = 2,
    BUILDING = 3,
    DONE = 4,
    FAILED = 5,


@dataclass
class BuildItem():
    name: str
    state: State = State.WAITING
    status: str = ""
    log_files: List[Path] = field(default_factory=list)
    task: Optional[str] = None
    task_children: List[str] = field(default_factory=list)
    debug_messages: List[str] = field(default_factory=list)


class RepoWaiter:
    def __init__(self, session: koji.ClientSession, tag: str):
        self.session = session
        self.tag = tag
        self.wait_task = None
        self.last_repo_event: int = -1

    async def do_wait(self):
        while True:
            repo_event = self.session.getRepo(self.tag)["create_event"]
            if repo_event != self.last_repo_event:
                self.wait_task = None
                self.last_repo_event = repo_event
                return repo_event

            await asyncio.sleep(20)

    async def get_next_repo_event(self) -> int:
        if not self.wait_task:
            self.wait_task = asyncio.create_task(self.do_wait())

        return await self.wait_task

    async def wait_for_event(self, event):
        if self.last_repo_event < event:
            while True:
                next_repo_event = await self.get_next_repo_event()
                if next_repo_event >= event:
                    break


class BuildSchedulerDisplay(LiveDisplay):
    def __init__(self, items: Dict[str, BuildItem]):
        super().__init__()
        self.items = copy.deepcopy(items)
        self.lock = threading.Lock()

    def update_items(self, items: Iterable[BuildItem]):
        changed = False
        with self.lock:
            for item in items:
                old = self.items[item.name]
                if old != item:
                    self.items[item.name] = copy.copy(item)
                    changed = True

        if changed:
            self.update()

    def render(self, stream: TextIO, when: RenderWhen):
        with self.lock:
            print('--------------------------------------------', file=stream)
            for item in self.items.values():
                status = item.status
                if item.state == State.DONE:
                    fg = "green"
                elif item.state == State.FAILED:
                    fg = "red"
                elif item.state == State.BUILDING:
                    if when == RenderWhen.EXCEPTION:
                        fg = "red"
                    elif when == RenderWhen.INTERRUPTED:
                        fg = "yellow"
                        status = "Interrupted"
                    else:
                        fg = "blue"
                else:
                    fg = None
                print(
                    click.style(f"{item.name}: ", bold=True, fg=fg) + f"{status}", file=stream
                )
                if item.state != State.DONE:
                    for log_file in item.log_files:
                        print(f"    {log_file}", file=stream)
                    if item.task:
                        print(f"    {item.task}", file=stream)
                    for task_child in item.task_children:
                        print(f"        {task_child}", file=stream)
                    for debug_message in item.debug_messages:
                        print(f"    {debug_message}", file=stream)


class BuildScheduler(ABC):
    def __init__(
        self,
        profile: ProfileConfig,
        build_after: Mapping[str, Collection[str]],
        parallel_jobs: int = 3
    ):
        self.profile = profile
        self.build_after = build_after
        self.parallel_jobs = parallel_jobs
        self.items: Dict[str, BuildItem] = {}
        self.running: Set[asyncio.Task] = set()
        self.slots = [False for i in range(0, self.parallel_jobs)]

    @abstractmethod
    async def build_item(self, item: BuildItem, slot: int, last_batch: bool):
        ...

    def add_item(self, item: BuildItem):
        self.items[item.name] = item

    def update_running_items(self):
        num_building = 0
        for item in self.items.values():
            if item.state == State.WAITING:
                after = self.build_after.get(item.name, ())
                not_ready = [
                    other.name
                    for other in (self.items.get(n) for n in after)
                    if other and other.state != State.DONE
                ]
                if not_ready:
                    item.status = f"Waiting for: {' '.join(not_ready)}"
                else:
                    item.state = State.READY
                    item.status = "Ready"
            elif item.state == State.BUILDING:
                num_building += 1

        for item in self.items.values():
            if num_building < self.parallel_jobs and item.state == State.READY:
                item.state = State.BUILDING
                self.running.add(asyncio.create_task(self.run_build_item(item)))
                num_building += 1

        self.display.update_items(self.items.values())

    def update_item(self, item: BuildItem,
                    state: Optional[State] = None,
                    status: Optional[str] = None,
                    log_files: Optional[List[Path]] = None,
                    task: Optional[str] = None,
                    task_children: Optional[List[str]] = None,
                    debug_messages: Optional[List[str]] = None):

        need_update = state == (State.DONE or state == State.FAILED) and state != item.state
        if state is not None:
            item.state = state
        if status is not None:
            item.status = status
        if log_files is not None:
            item.log_files = log_files
        if task is not None:
            item.task = task
        if task_children is not None:
            item.task_children = task_children
        if debug_messages is not None:
            item.debug_messages = debug_messages

        if need_update:
            self.update_running_items()
        else:
            self.display.update_items((item,))

    async def run_build_item(self, item):
        last_batch = True
        for other, after in self.build_after.items():
            if item.name in after:
                last_batch = False

        for i, occupied in enumerate(self.slots):
            if not occupied:
                self.slots[i] = True
                slot = i
                break
        else:
            assert False, "Can't allocate slot"

        await self.build_item(item, slot, last_batch)

        if item.state == State.BUILDING:
            item.state = State.FAILED
            item.status = "Build method exited in RUNNING state"
            self.update_running_items()

        if item.state == State.DONE:
            self.slots[slot] = False
        else:
            # We don't want to reuse a slot for a failed job, since (in the local case)
            # the user may want to examine the buildroot.
            self.slots.append(False)

    async def do_build(self):
        self.update_running_items()
        while self.running:
            done, _ = await asyncio.wait(self.running, return_when="FIRST_COMPLETED")
            for task in done:
                exception = task.exception()
                if exception:
                    raise exception
            self.running -= done

    def build(self):
        self.display = BuildSchedulerDisplay(self.items)
        with self.display:
            asyncio.run(self.do_build())


@dataclass(init=False)
class MockBuildItemKoji(BuildItem):
    nvr: str

    def __init__(self, nvr: str):
        super().__init__(name=rpm_name_only(nvr))
        self.nvr = nvr


@dataclass(init=False)
class MockBuildItemRepo(BuildItem):
    path: Path

    def __init__(self, path: Path):
        super().__init__(name=path.name)
        self.path = path


class MockBuildScheduler(BuildScheduler):
    def __init__(
        self,
        *,
        mock_cfg: str,
        profile: ProfileConfig,
        repo_path: Path,
        workdir: Path,
        build_after: Mapping[str, Collection[str]],
        parallel_jobs: int = 3
    ):
        super().__init__(profile, build_after, parallel_jobs=parallel_jobs)
        self.base_workdir = workdir / "rpms"
        self.repo_path = repo_path
        self.mock_cfg = mock_cfg
        self.mock_cfg_path = self.base_workdir / 'mock.cfg'
        self.repo_lock = asyncio.Lock()

    def add_koji_item(self, nvr):
        self.add_item(MockBuildItemKoji(nvr))

    def add_repo_item(self, path):
        self.add_item(MockBuildItemRepo(path))

    def build(self):
        # The repo will be referenced in the mock config, since it
        # is a build-requirements source as well as a destination,
        # so we need to make sure it valid before we start building
        self.repo_path.mkdir(parents=True, exist_ok=True)
        if not (self.repo_path / "repodata/repomd.xml").exists():
            asyncio.run(self.createrepo())

        self.mock_cfg_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.mock_cfg_path, "w") as f:
            f.write(self.mock_cfg)

        super().build()

    async def createrepo(self):
        async with self.repo_lock:
            logpath = self.base_workdir / "createrepo.log"
            self.base_workdir.mkdir(parents=True, exist_ok=True)
            with open(logpath, "wb") as logfile:
                proc = await asyncio.create_subprocess_exec(
                    "createrepo_c",
                    "--general-compress-type=gz",
                    self.repo_path, stdout=logfile, stderr=logfile
                )
                if await proc.wait() != 0:
                    logger.error(
                        "createrepo_c failed, see %s", logpath
                    )
                    sys.exit(1)

    async def build_item(self, item: BuildItem, slot: int, last_batch: bool):
        U = functools.partial(self.update_item, item)

        workdir = self.base_workdir / item.name
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir(parents=True, exist_ok=True)

        if isinstance(item, MockBuildItemKoji):
            topurl = self.profile.source_koji_options["topurl"]
            path_info = koji.PathInfo(topdir=topurl)
            name, version, release = item.nvr.rsplit("-", 2)

            location = path_info.build({
                "name": name,
                "version": version,
                "release": release,
            }) + "/" + path_info.rpm({
                "name": name,
                "version": version,
                "release": release,
                "arch": "src"
            })
        else:
            assert isinstance(item, MockBuildItemRepo)

            U(State.BUILDING, status="Building SRPM")

            logpath = workdir / "build_srpm.log"
            with open(logpath, "w") as logfile:
                proc = await asyncio.create_subprocess_exec(
                    "fedpkg", "srpm",
                    cwd=item.path,
                    stdout=logfile, stderr=logfile
                )
                if await proc.wait() != 0:
                    U(State.FAILED, f"'fedpkg srpm' failed, see {logpath}")

            with open(logpath, "r") as logfile:
                for line in logfile:
                    m = re.match(r"Wrote:\s*(\S*)", line)
                    if m:
                        location = m.group(1)
                        break
                else:
                    raise RuntimeError(
                        f"failed to find location in 'fedpkg srpm' output, see {logpath}"
                    )

        def make_mock_cmd(*extra_args: Union[str, Path]):
            return (
                "mock",
                "-r", self.mock_cfg_path,
                "--uniqueext", str(slot),
            ) + extra_args

        build_cmd = make_mock_cmd(
            "--resultdir", workdir,
            "--rebuild",
            "--no-cleanup-after",
            location
        )

        U(status=str(workdir))

        with open(workdir / "mock_output.log", "w") as outfile:
            proc = await asyncio.create_subprocess_exec(
                *build_cmd, stdout=outfile, stderr=outfile
            )

            def update_log_files():
                log_files = sorted(
                    (child for child in workdir.iterdir() if child.name.endswith(".log")),
                    key=lambda child: child.name
                )

                state_log = workdir / "state.log"
                try:
                    state_stack = []
                    with open(state_log, "r") as f:
                        for line in f:
                            m = re.match(r'^.*?(Start|Finish):\s*(.*?)\s*$', line)
                            if m:
                                if m.group(1) == "Start":
                                    state_stack.append(m.group(2))
                                elif m.group(1) == "Finish":
                                    if len(state_stack) > 0 and m.group(2) == state_stack[-1]:
                                        state_stack.pop()
                    status = state_stack[-1] if len(state_stack) > 0 else "Building"
                except FileNotFoundError:
                    status = "Building"

                U(status=status, log_files=log_files)

            async def periodically_update_logfiles():
                while True:
                    update_log_files()
                    await asyncio.sleep(0.1)

            update_logfiles_task = asyncio.create_task(periodically_update_logfiles())
            returncode = await proc.wait()
            update_logfiles_task.cancel()
            update_log_files()

            if returncode == 0:
                U(status="cleaning buildroot")

                cleanup_cmd = make_mock_cmd("--clean")
                cleanup_proc = await asyncio.create_subprocess_exec(
                    "mock", *cleanup_cmd, stdout=outfile, stderr=outfile
                )
                if await cleanup_proc.wait() != 0:
                    logger.warning(
                        f"'{' '.join(shlex.quote(str(c)) for c in cleanup_cmd)}' failed"
                    )

                U(status="moving result RPMS")
                async with self.repo_lock:
                    for child in workdir.iterdir():
                        if child.name.endswith(".rpm"):
                            dest = self.repo_path / child.name
                            dest.unlink(missing_ok=True)
                            shutil.move(child, dest)

                U(status="createrepo")
                await self.createrepo()
                U(State.DONE, status="Built successfully")
            else:
                print_root_cmd = make_mock_cmd("--print-root")
                root = subprocess.check_output(print_root_cmd, encoding="utf-8").strip()

                shell_cmd = make_mock_cmd("--shell")
                U(State.FAILED,
                  status="Build failed",
                  debug_messages=[
                      f"chroot: {root}",
                      f"Enter chroot: {' '.join(shlex.quote(str(c)) for c in shell_cmd)}"
                  ])


@dataclass(init=False)
class KojiBuildItem(BuildItem):
    nvr: str

    def __init__(self, nvr: str):
        super().__init__(name=rpm_name_only(nvr))
        self.nvr = nvr


class KojiBuildScheduler(BuildScheduler):
    def __init__(
        self,
        *,
        profile: ProfileConfig,
        target: str,
        build_after: Mapping[str, Collection[str]],
        parallel_jobs: int = 5
    ):
        super().__init__(profile, build_after, parallel_jobs=parallel_jobs)
        self.target = target

    def add_koji_item(self, nvr):
        self.add_item(KojiBuildItem(nvr))

    def build(self):
        activate_session(self.profile.koji_session, self.profile.koji_options)

        self.build_tag = self.profile.koji_session.getBuildTarget(self.target)["build_tag_name"]
        self.repo_waiter = RepoWaiter(self.profile.koji_session, self.build_tag)

        super().build()

    async def build_item(self, item: BuildItem, slot: int, last_batch: bool):
        assert isinstance(item, KojiBuildItem)

        U = functools.partial(self.update_item, item)

        U(State.BUILDING, status="Getting source URL")
        assert isinstance(item, KojiBuildItem)
        build = self.profile.source_koji_session.getBuild(item.nvr)
        source_url = build["source"]

        U(status="Starting build")
        session = self.profile.koji_session
        task_id = session.build(source_url, self.target)
        url_base = self.profile.koji_options['weburl']
        taskurl = f"{url_base}/taskinfo?taskID={task_id}"

        while True:
            task_info = session.getTaskInfo(task_id, request=True)
            formatted_task = format_task(self.profile, task_info)
            state = koji.TASK_STATES[task_info['state']]
            task_children = session.getTaskChildren(task_id, request=True)
            formatted_task_children = [
                format_task(self.profile, task_child) for task_child in task_children]

            if state == "FAILED":
                U(State.FAILED, status=f"task {format_link(taskurl, task_id)} failed")
                return
            elif state == "CANCELED":
                U(State.FAILED, status=f"task {format_link(taskurl, task_id)} was canceled")
                return
            elif state == "CLOSED":
                break

            U(status="",
              task=formatted_task, task_children=formatted_task_children)
            await asyncio.sleep(20)

        tag_info = session.listTagged(
            self.build_tag, inherit=True, latest=True, package=item.name
        )[0]
        U(status=f"Waiting for {tag_info['nvr']} to appear in {self.build_tag}")

        assert tag_info["create_event"] is not None
        await self.repo_waiter.wait_for_event(tag_info["create_event"])

        U(State.DONE, status=f"{tag_info['nvr']} built successfully")
