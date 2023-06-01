from dataclasses import dataclass
import functools
import logging
import click
import pipes
import subprocess
import sys
from typing import Optional, NoReturn


def error(msg):
    click.secho('error: ', fg='red', bold=True, err=True, nl=False)
    click.echo(msg, err=True)


def die(msg) -> NoReturn:
    error(msg)
    sys.exit(1)


def warn(msg):
    click.secho('warning: ', fg='yellow', bold=True, err=True, nl=False)
    click.echo(msg, err=True)


def important(msg):
    click.secho(msg, err=True, bold=True)


def info(msg):
    click.secho('info: ', fg='blue', bold=True, err=True, nl=False)
    click.echo(msg, err=True)


def verbose(msg):
    if logging.root.level <= logging.INFO:
        click.secho('verbose: ', fg='black', bold=True, err=True, nl=False)
        click.echo(msg, err=True)


def header(msg):
    important(msg)
    important('=' * len(msg))


def log_call(args):
    click.secho('running: ', fg='blue', bold=True, err=True, nl=False)
    click.echo(' '.join(pipes.quote(str(a)) for a in args), err=True)


def check_call(args, cwd=None, stdout=None):
    log_call(args)
    rv = subprocess.call(args, cwd=cwd, stdout=stdout)
    if rv != 0:
        die(f"{args[0]} failed (exit status={rv})")


@dataclass
class RuntimeInfo:
    runtime_id: str
    sdk_id: str
    version: str


class Arch:
    def __init__(self, oci, flatpak, rpm):
        self.oci = oci
        self.flatpak = flatpak
        self.rpm = rpm


ARCHES = {
    arch.oci: arch for arch in [
        Arch(oci="amd64", flatpak="x86_64", rpm="x86_64"),
        Arch(oci="arm64", flatpak="aarch64", rpm="aarch64"),
        Arch(oci="s390x", flatpak="s390x", rpm="s390x"),
        Arch(oci="ppc64le", flatpak="ppc64le", rpm="ppc64le"),
        # This is used in tests to test the case where the Flatpak and RPM names are
        # different - this does not happen naturally at the moment as far as I know.
        Arch(oci="testarch", flatpak="testarch", rpm="testarch_rpm"),
    ]
}


@functools.lru_cache(maxsize=None)
def _get_rpm_arch():
    return subprocess.check_output(
        ["rpm", "--eval", "%{_arch}"], universal_newlines=True,
    ).strip()


def get_arch(oci_arch: Optional[str] = None):
    if oci_arch:
        return ARCHES[oci_arch]
    else:
        rpm_arch = _get_rpm_arch()
        for arch in ARCHES.values():
            if arch.rpm == rpm_arch:
                return arch

        raise RuntimeError(f"Unknown RPM arch '{format(rpm_arch)}'")


def rpm_name_only(rpm_name):
    return rpm_name.rsplit("-", 2)[0]
