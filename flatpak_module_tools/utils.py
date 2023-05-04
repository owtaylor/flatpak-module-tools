import functools
import logging
import click
import pipes
import re
import subprocess
import sys
from typing import NoReturn


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
    click.echo(' '.join(pipes.quote(a) for a in args))


def check_call(args, cwd=None):
    log_call(args)
    rv = subprocess.call(args, cwd=cwd)
    if rv != 0:
        die(f"{args[0]} failed (exit status={rv})")


class ModuleSpec:
    def __init__(self, name, stream, version=None, profile=None):
        self.name = name
        self.stream = stream
        self.version = version
        self.profile = profile

    def to_str(self, include_profile=True):
        result = self.name + ':' + self.stream
        if self.version:
            result += ':' + self.version
        if include_profile and self.profile:
            result += '/' + self.profile

        return result

    def __repr__(self):
        return f"ModuleSpec({self.to_str()})"

    def __eq__(self, other):
        return self.__dict__ == other.__dict__


def split_module_spec(module):
    # Current module naming guidelines are at:
    # https://docs.pagure.org/modularity/development/building-modules/naming-policy.html
    # We simplify the possible NAME:STREAM:CONTEXT:ARCH/PROFILE and only care about
    # NAME:STREAM or NAME:STREAM:VERSION with optional PROFILE. ARCH is determined by
    # the architecture. CONTEXT may become important in the future, but we ignore it
    # for now.
    #
    # Previously the separator was '-' instead of ':', which required hardcoding the
    # format of VERSION to distinguish between HYPHENATED-NAME-STREAM and NAME-STREAM-VERSION.
    # We support the old format for compatibility.
    #
    PATTERNS = [
        (r'^([^:/]+):([^:/]+):([^:/]+)(?:/([^:/]+))?$', 3, 4),
        (r'^([^:/]+):([^:/]+)(?:/([^:/]+))?$', None, 3),
        (r'^(.+)-([^-]+)-(\d{14})$', 3, None),
        (r'^(.+)-([^-]+)$', None, None)
    ]

    for pat, version_index, profile_index in PATTERNS:
        m = re.match(pat, module)
        if m:
            name = m.group(1)
            stream = m.group(2)
            version = None
            if version_index is not None:
                version = m.group(version_index)
            else:
                version = None
            if profile_index is not None:
                profile = m.group(profile_index)
            else:
                profile = None

            return ModuleSpec(name, stream, version, profile)

    raise RuntimeError(
        'Module specification should be NAME:STREAM[/PROFILE] or NAME:STREAM:VERSION[/PROFILE]. ' +
        '(NAME-STREAM and NAME-STREAM-VERSION supported for compatibility.)'
    )


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


def get_arch(oci_arch: str | None = None):
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
