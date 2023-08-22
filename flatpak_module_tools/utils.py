import codecs
from contextlib import contextmanager
from dataclasses import dataclass
import functools
import hashlib
import logging
import os
from tempfile import NamedTemporaryFile
import pipes
import subprocess
import sys
from typing import IO, Optional, NoReturn, cast

import click


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


@functools.lru_cache(maxsize=None)
def _get_rpm_arch():
    return subprocess.check_output(
        ["rpm", "--eval", "%{_arch}"], universal_newlines=True,
    ).strip()


class Arch:
    name: str
    oci: str
    flatpak: str
    rpm: str

    AARCH64: "Arch"
    X86_64: "Arch"
    PPC64LE: "Arch"
    S390X: "Arch"
    TESTARCH: "Arch"

    def __new__(cls, *,
                oci: Optional[str] = None,
                flatpak: Optional[str] = None,
                rpm: Optional[str] = None):

        if flatpak is None and oci is None and rpm is None:
            rpm = _get_rpm_arch()

        for v in cls.__dict__.values():
            if isinstance(v, Arch) and (v.oci == oci or v.flatpak == flatpak or v.rpm == rpm):
                return v
        else:
            raise KeyError(f"Can't find Arch(flatpak={flatpak}, oci={oci}, rpm={rpm})")

    @classmethod
    def _add(cls, name: str, flatpak: str, oci: str, rpm: str):
        obj = object.__new__(cls)
        obj.name = name
        obj.oci = oci
        obj.flatpak = flatpak
        obj.rpm = rpm
        setattr(cls, name, obj)

    def __repr__(self):
        return f"Arch.{self.name}"


Arch._add("AARCH64", "aarch64", "arm64", "aarch64")
Arch._add("X86_64", "x86_64",  "amd64", "x86_64")
Arch._add("PPC64LE", "ppc64le", "ppc64le", "ppc64le")
Arch._add("S390X", "s390x", "s390x",   "s390x")

# This is used in tests to test the case where the Flatpak and RPM names are
# different - this does not happen naturally at the moment as far as I know.
Arch._add("TESTARCH", "testarch_flatpak", "testarch_oci", "testarch_rpm")


def rpm_name_only(rpm_name):
    return rpm_name.rsplit("-", 2)[0]


@contextmanager
def atomic_writer(output_path):
    output_dir = os.path.dirname(output_path)
    tmpfile = NamedTemporaryFile(delete=False,
                                 dir=output_dir,
                                 prefix=os.path.basename(output_path))
    success = False
    try:
        writer = cast(IO[str], codecs.getwriter("utf-8")(tmpfile))
        yield writer
        writer.close()
        tmpfile.close()

        # We don't overwrite unchanged files, so that the modtime and
        # httpd-computed ETag stay the same.

        changed = True
        if os.path.exists(output_path):
            h1 = hashlib.sha256()
            with open(output_path, "rb") as f:
                h1.update(f.read())
            h2 = hashlib.sha256()
            with open(tmpfile.name, "rb") as f:
                h2.update(f.read())

            if h1.digest() == h2.digest():
                changed = False

        if changed:
            # Atomically write over result
            os.chmod(tmpfile.name, 0o644)
            os.rename(tmpfile.name, output_path)
            print(f"Wrote {output_path}")
        else:
            os.unlink(tmpfile.name)

        success = True
    finally:
        if not success:
            tmpfile.close()
            os.unlink(tmpfile.name)
