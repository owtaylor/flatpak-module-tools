import logging
from pathlib import Path
from typing import List

import click

from .config import add_config_file, set_profile_name, get_profile
from .container_builder import AssemblyOptions, ContainerBuilder
from .container_spec import ContainerSpec, ValidationError
from .console_logging import ConsoleHandler
from .flatpak_builder import (FLATPAK_METADATA_ANNOTATIONS,
                              FLATPAK_METADATA_BOTH,
                              FLATPAK_METADATA_LABELS)
from .installer import Installer
from .rpm_builder import RpmBuilder
from .utils import die, info


def make_container_spec(location):
    try:
        return ContainerSpec(location)
    except ValidationError as e:
        raise click.ClickException(str(e))


@click.group()
@click.option('-v', '--verbose', is_flag=True,
              help='Show verbose debugging output')
@click.option('-c', '--config', metavar='CONFIG_YAML', multiple=True,
              help='Additional configuration file to read')
@click.option('-p', '--profile', metavar='PROFILE_NAME', default='production',
              help='Alternate configuration profile to use')
def cli(verbose, config, profile):
    for c in reversed(config):
        add_config_file(c)

    set_profile_name(profile)
    try:
        get_profile()
    except KeyError:
        die(f"Unknown profile '{profile}'")

    handlers = [ConsoleHandler()]

    if verbose:
        logging.basicConfig(level=logging.INFO, handlers=handlers)
    else:
        logging.basicConfig(handlers=handlers, level=logging.WARNING)
        logging.getLogger("flatpak_module_tools").setLevel(level=logging.INFO)


@cli.command(name="build-container")
@click.option('--flatpak-metadata',
              type=click.Choice([FLATPAK_METADATA_LABELS,
                                 FLATPAK_METADATA_ANNOTATIONS,
                                 FLATPAK_METADATA_BOTH], case_sensitive=False),
              default=FLATPAK_METADATA_BOTH,
              help='How to store Flatpak metadata in the container')
@click.option('--containerspec', metavar='CONTAINER_YAML', default='./container.yaml',
              help='Path to container.yaml - defaults to ./container.yaml')
@click.option('--install', is_flag=True,
              help='automatically install Flatpak for the current user')
def build_container(flatpak_metadata, containerspec, install):
    """Build a container from local or remote module"""

    container_builder = ContainerBuilder(profile=get_profile(),
                                         container_spec=make_container_spec(containerspec),
                                         flatpak_metadata=flatpak_metadata)
    tarfile = container_builder.build()

    if install:
        installer = Installer(profile=get_profile())
        installer.set_source_path(tarfile)
        installer.install()


@cli.command(name="assemble")
@click.option('--containerspec', metavar='CONTAINER_YAML', default='./container.yaml',
              help='Path to container.yaml - defaults to ./container.yaml')
@click.option('--target', metavar='KOJI_TARGET',
              help='Koji target to build against')
@click.option('--nvr', metavar='NVR',
              help='name-version-release for built container')
@click.option('--runtime-nvr', metavar='NVR',
              help='name-version-release for runtime to build againset (apps only)')
@click.option('--runtime-repo', metavar='REPO_ID', type=int,
              help='Koji repository ID for runtime packages')
@click.option('--app-repo', metavar='REPO_ID', type=int,
              help='Koji repository ID for application packages (apps only)')
@click.option('--installroot', metavar='DIR', type=Path, default="/contents",
              help="Location to install packages")
@click.option('--workdir', metavar='DIR', type=Path, default="/tmp",
              help="Location to create temporary files")
@click.option('--resultdir', metavar='DIR', type=Path, default=".",
              help="Location to write output")
def assemble(
    containerspec,
    target: str | None,
    nvr: str | None,
    runtime_nvr: str | None,
    runtime_repo: int | None,
    app_repo: int | None,
    installroot: Path,
    workdir: Path,
    resultdir: Path,
):
    """Run as root inside a container to create the OCI"""

    container_spec = make_container_spec(containerspec)
    container_builder = PackageContainerBuilder(
        profile=get_profile(),
        container_spec=container_spec,
        flatpak_metadata=FLATPAK_METADATA_LABELS
    )

    if target:
        if nvr or runtime_nvr or runtime_repo or app_repo:
            die("--target cannot be specified together with "
                "--nvr, --runtime-nvr, --runtime-repo, or --app-repo")

        options = container_builder.assembly_options_from_target(target)
    else:
        if container_spec.flatpak.build_runtime:
            if not nvr or not runtime_repo:
                die("--nvr and --runtime-repo must be specified for runtimes")
            if runtime_nvr or app_repo:
                die("--nvr and --runtime-repo must not be specified for runtimes")

            options = AssemblyOptions(
                nvr=nvr, runtime_nvr=None, runtime_repo=runtime_repo, app_repo=None
            )
        else:
            if not nvr or not runtime_nvr or not runtime_repo or not app_repo:
                die("--nvr, --runtime-nvr, --runtime-repo, and --app-repo "
                    "must be specified for applications")

            options = AssemblyOptions(
                nvr=nvr, runtime_nvr=runtime_nvr, runtime_repo=runtime_repo, app_repo=app_repo
            )

    container_builder.assemble(
        options, installroot=installroot, workdir=workdir, resultdir=resultdir
    )


@cli.command()
@click.option('--koji', is_flag=True,
              help='Look up argument as NAME[:STREAM] in Koji')
@click.argument('path_or_url')
def install(koji, path_or_url):
    """Install a container as a Flatpak"""

    installer = Installer(profile=get_profile())
    if koji:
        installer.set_source_koji_name_stream(path_or_url)
    elif path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        installer.set_source_url(path_or_url)
    else:
        installer.set_source_path(path_or_url)

    installer.install()


@cli.command()
@click.option('--containerspec', metavar='CONTAINER_YAML', default='./container.yaml',
              help='path to container.yaml - defaults to ./container.yaml')
@click.option('--all-missing', is_flag=True,
              help='Build all packages needed to build ')
@click.argument('packages', nargs=-1, metavar="PKGS")
def build_rpms_local(containerspec, packages: List[str], all_missing: bool):
    spec = make_container_spec(containerspec)

    manual_packages: List[str] = []
    manual_repos: List[Path] = []

    for pkg in packages:
        if '/' in pkg:
            manual_repos.append(Path(pkg))
        else:
            manual_packages.append(pkg)

    builder = RpmBuilder(profile=get_profile(), container_spec=spec)
    if packages is [] and not all_missing:
        info("Nothing to rebuild, specify packages or --all-missing")
    else:
        builder.build_rpms_local(manual_packages, manual_repos, all_missing=all_missing)
