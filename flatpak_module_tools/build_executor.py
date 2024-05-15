from abc import ABC, abstractmethod
from functools import cached_property
from pathlib import Path
import shlex
import subprocess
from typing import Dict, Optional, Sequence, Union

from .build_context import BuildContext
from .mock import make_mock_cfg
from .utils import (
    atomic_writer, check_call, log_call
)


class BuildExecutor(ABC):
    def __init__(self, *, context: BuildContext,
                 installroot: Path, workdir: Path, releasever: str, runtimever: str):
        self.context = context
        self.installroot = installroot
        self.workdir = workdir
        self.releasever = releasever
        self.runtimever = runtimever

    @abstractmethod
    def init(self) -> None:
        ...

    @abstractmethod
    def write_file(self, path: Path, contents: str) -> None:
        pass

    @abstractmethod
    def check_call(self, cmd: Sequence[Union[str, Path]], *,
                   cwd: Optional[Path] = None,
                   mounts: Optional[Dict[Path, Path]] = None,
                   enable_network: bool = False) -> None:
        ...

    @abstractmethod
    def popen(self, cmd: Sequence[Union[str, Path]], *,
              stdout=None, cwd: Optional[Path] = None) -> subprocess.Popen:
        ...

    @property
    @abstractmethod
    def absolute_installroot(self) -> Path:
        ...


class MockExecutor(BuildExecutor):
    @property
    def _bootstrap_koji_repo(self):
        # We need a repository to install the basic buildroot tools
        # (dnf, mount, tar, etc) from. The runtime package repo works.

        return self.context.runtime_package_repo.dnf_config()

    def init(self):
        self.mock_cfg_path = self.workdir / "mock.cfg"
        to_install = [
            '/bin/bash',
            '/bin/mount',
            'coreutils',  # for mkdir
            'dnf',
            'glibc-minimal-langpack',
            'shadow-utils',
            'tar',
        ]
        mock_cfg = make_mock_cfg(
            arch=self.context.arch,
            chroot_setup_cmd=f"install {' '.join(to_install)}",
            releasever=self.releasever,
            repos=[self._bootstrap_koji_repo],
            root_cache_enable=True,
            runtimever=self.runtimever
        )
        with atomic_writer(self.mock_cfg_path) as f:
            f.write(mock_cfg)

        check_call(['mock', '-q', '-r', self.mock_cfg_path, '--clean'])

    def write_file(self, path, contents):
        temp_location = self.workdir / path.name

        with open(temp_location, "w") as f:
            f.write(contents)

        check_call([
            'mock', '-q', '-r', self.mock_cfg_path, '--copyin', temp_location, path
        ])

    def check_call(self, cmd, *,
                   cwd=None,
                   mounts: Optional[Dict[Path, Path]] = None,
                   enable_network: bool = False):
        # mock --chroot logs the result, which we don't want here,
        # so we use --shell instead.

        args = ['mock', '-q', '-r', self.mock_cfg_path, '--shell']
        if cwd:
            args += ['--cwd', cwd]

        if enable_network:
            args.append("--enable-network")
        if mounts:
            for inner_path, outer_path in mounts.items():
                args += (
                    "--plugin-option",
                    f"bind_mount:dirs=[('{outer_path}', '{inner_path}')]"
                )

        args.append(" ".join(shlex.quote(str(c)) for c in cmd))
        check_call(args)

    def popen(self, cmd, *, stdout=None, cwd=None):
        # mock --chroot logs the result, which we don't want here,
        # so we use --shell instead.

        args = ['mock', '-q', '-r', self.mock_cfg_path, '--shell']
        if cwd:
            args += ['--cwd', cwd]
        args.append(" ".join(shlex.quote(str(c)) for c in cmd))

        log_call(args)
        return subprocess.Popen(args, stdout=stdout)

    @cached_property
    def absolute_installroot(self):
        args = ['mock', '-q', '-r', self.mock_cfg_path, '--print-root-path']
        root_path = subprocess.check_output(args, universal_newlines=True).strip()
        return Path(root_path) / self.installroot.relative_to("/")


class InnerExcutor(BuildExecutor):
    def init(self):
        pass

    def write_file(self, path, contents):
        with open(path, "w") as f:
            f.write(contents)

    def check_call(self, cmd, *, cwd=None, mounts=None, enable_network=False):
        assert not mounts  # Not supported for InnerExecutor
        check_call(cmd, cwd=cwd)

    def popen(self, cmd, *, stdout=None, cwd=None):
        return subprocess.Popen(cmd, stdout=stdout, cwd=cwd)

    @property
    def absolute_installroot(self):
        return self.installroot
