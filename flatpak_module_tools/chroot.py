from enum import Enum
from functools import cached_property
from pathlib import Path
import shlex
import subprocess
from textwrap import dedent
from typing import Dict, List, Optional

from flatpak_module_tools.build_executor import BuildExecutor


class UnshareType(Enum):
    # The chroot has already been set up with at least /dev/null
    NONE = 1
    # We need to create a mount namespace. This would be the case
    # where we have the CAP_SYSADMIN in the current user namespace,
    # but may not be able to create a new user namespace, for
    # example because we are chroot'ed and chroot'ed processes
    # are forbidden from creating a new user namespace.
    MOUNT = 2
    # We don't have CAP_SYSADMIN, so we need to create a mount
    # namespaces *and* a user namespace.
    USER = 3

    def make_command(self, args: List[str]) -> List[str]:
        if self == UnshareType.NONE:
            return args
        elif self == UnshareType.MOUNT:
            return ["unshare", "-m", "--"] + args
        elif self == UnshareType.USER:
            return ["unshare", "--map-users=all", "--map-groups=all", "-m", "--"] + args

        assert False


class Chroot:
    """Run commands in a chroot with /dev, /proc, etc, populated"""

    def __init__(self, executor: BuildExecutor):
        self.executor = executor
        self.path = executor.installroot

    def prepare(self):
        (self.path / "tmp").mkdir(mode=0o1777, parents=True)

    @cached_property
    def unshare_type(self):
        # If {installroot}/dev/null exists, we assume that /proc, /dev, etc
        # are already set up as well they can be can we can't do anything more,
        # otherwise we create bind mounts to the corresponding directories
        # in the outer environment.
        if (self.path / "dev/null").exists():
            return UnshareType.NONE

        for unshare_type in (UnshareType.MOUNT, UnshareType.USER):
            result = subprocess.run(unshare_type.make_command(["true"]),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode == 0:
                return unshare_type

        raise RuntimeError("Cannot figure out how to create installroot mounts")

    def check_call(self, args: List[str], mounts: Optional[Dict[Path, Path]] = None):
        run_sh_contents = f"cd {self.path}\n"
        if self.unshare_type != UnshareType.NONE:
            # The /var/cache/dnf bind mount allows mounting a persistent
            # cache directory into the outer environment and having it be
            # used across builds.
            run_sh_contents += dedent(f"""\
                for i in /proc /sys /dev /var/cache/dnf ; do
                    mkdir -p {self.path}$i
                    mount --rbind $i {self.path}$i
                done
                """)
        run_sh_contents += shlex.join(args)

        self.executor.write_file(Path("/tmp/run.sh"), run_sh_contents)

        command = self.unshare_type.make_command(["/bin/bash", "-ex", "/tmp/run.sh"])
        self.executor.check_call(command, mounts=mounts, enable_network=True)
