import logging
from pathlib import Path
from typing import List, Optional

import click

from .build_context import AutoBuildContext, ManualBuildContext
from .config import add_config_file, set_profile_name, get_profile
from .container_builder import ContainerBuilder
from .container_spec import ContainerSpec, ValidationError
from .console_logging import ConsoleHandler
from .flatpak_builder import (FLATPAK_METADATA_ANNOTATIONS,
                              FLATPAK_METADATA_BOTH,
                              FLATPAK_METADATA_LABELS)
from .installer import Installer
from .rpm_builder import RpmBuilder
from .utils import Arch, die, info, warn


def make_container_spec(location: str):
    try:
        return ContainerSpec(location)
    except ValidationError as e:
        raise click.ClickException(str(e))


def get_target(container_spec: ContainerSpec,
               target_option: Optional[str]) -> str:
    if target_option:
        return target_option
    else:
        if container_spec.flatpak.runtime_version is None:
            die("--target must be specified "
                "if flatpak:runtime_version is not set in container.yaml")
        profile = get_profile()
        release = profile.release_from_runtime_version(
            container_spec.flatpak.runtime_version
        )

        return profile.get_flatpak_koji_target(release)


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


@cli.command
@click.option('--flatpak-metadata',
              type=click.Choice([FLATPAK_METADATA_LABELS,
                                 FLATPAK_METADATA_ANNOTATIONS,
                                 FLATPAK_METADATA_BOTH], case_sensitive=False),
              default=FLATPAK_METADATA_BOTH,
              help='How to store Flatpak metadata in the container')
@click.option('--containerspec', metavar='CONTAINER_YAML', default='./container.yaml',
              help='Path to container.yaml - defaults to ./container.yaml')
@click.option('--local-repo', metavar='REPO_PATH', type=Path,
              help="Path to repository location for local builds - defaults to ./<arch>/rpms")
@click.option('--local-runtime', metavar='RUNTIME_TAR_GZ', type=Path,
              help="Path to local container build to use as runtime")
@click.option('--target', metavar='KOJI_TARGET',
              help='Koji target to build against. Determined from runtime_version if missing.')
@click.option('--allow-outdated', is_flag=True,
              help="Continue even if included packages will have an old version")
@click.option('--install', is_flag=True,
              help='automatically install Flatpak for the current user')
def build_container_local(flatpak_metadata, containerspec,
                          local_repo: Optional[Path], local_runtime: Optional[Path],
                          target: str, install: bool, allow_outdated: bool):
    """Build a container from local and remote RPMs"""

    container_spec = make_container_spec(containerspec)
    target = get_target(container_spec, target)

    local_repo_specified = bool(local_repo)
    local_repo = local_repo if local_repo_specified else Path(Arch().rpm) / "rpms"
    if not (local_repo / "repodata/repomd.xml").exists():
        if not local_repo_specified:
            warn("No repository at {local_repo}, ignoring")
        local_repo = None

    build_context = AutoBuildContext(
        profile=get_profile(),
        container_spec=container_spec, local_repo=local_repo,
        local_runtime=local_runtime, target=target
    )

    if not container_spec.flatpak.build_runtime:
        rpm_builder = RpmBuilder(build_context)
        rpm_builder.check(include_localrepo=True, allow_outdated=allow_outdated)

    container_builder = ContainerBuilder(build_context, flatpak_metadata=flatpak_metadata)
    tarfile = container_builder.build()

    if install:
        installer = Installer(profile=get_profile())
        installer.set_source_path(tarfile)
        installer.install()


@cli.command(name="assemble")
@click.option('--containerspec', metavar='CONTAINER_YAML', default='./container.yaml',
              help='Path to container.yaml - defaults to ./container.yaml')
@click.option('--target', metavar='KOJI_TARGET',
              help='Koji target to build against. Determined from runtime_version if missing.')
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

    if nvr or runtime_nvr or runtime_repo or app_repo:
        if target:
            die("--target cannot be specified together with "
                "--nvr, --runtime-nvr, --runtime-repo, or --app-repo")

        if container_spec.flatpak.build_runtime:
            if not nvr or not runtime_repo:
                die("--nvr and --runtime-repo must be specified for runtimes")
            if runtime_nvr or app_repo:
                die("--nvr and --runtime-repo must not be specified for runtimes")

        else:
            if not nvr or not runtime_nvr or not runtime_repo or not app_repo:
                die("--nvr, --runtime-nvr, --runtime-repo, and --app-repo "
                    "must be specified for applications")

        build_context = ManualBuildContext(
            profile=get_profile(), container_spec=container_spec,
            nvr=nvr, runtime_nvr=runtime_nvr, runtime_repo=runtime_repo, app_repo=app_repo
        )

    else:
        target = get_target(container_spec, target)
        build_context = AutoBuildContext(
            profile=get_profile(), container_spec=container_spec, target=target
        )

    container_builder = ContainerBuilder(
        context=build_context,
        flatpak_metadata=FLATPAK_METADATA_LABELS
    )
    container_builder.assemble(
        installroot=installroot, workdir=workdir, resultdir=resultdir
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
@click.option('--local-runtime', metavar='RUNTIME_TAR_GZ', type=Path,
              help="Path to local container build to use as runtime")
@click.option('--target', metavar='KOJI_TARGET',
              help=('Koji target for Flatpak **container** building. '
                    'Determined from runtime_version if missing.'))
@click.option('--auto', is_flag=True,
              help='Build all packages needed to build container')
@click.option('--allow-outdated', is_flag=True,
              help="Don't rebuild packages that are present but have an old version")
@click.argument('packages', nargs=-1, metavar="PKGS")
def build_rpms(
    containerspec: str, local_runtime: Optional[Path], target: Optional[str],
    packages: List[str], auto: bool, allow_outdated
):
    """Rebuild rpms needed for the container in Koji"""

    spec = make_container_spec(containerspec)
    target = get_target(spec, target)

    build_context = AutoBuildContext(
        profile=get_profile(),
        container_spec=spec, local_runtime=local_runtime, target=target
    )

    builder = RpmBuilder(build_context)
    if packages is [] and not auto:
        info("Nothing to rebuild, specify packages or --auto")
    else:
        builder.build_rpms(packages, auto=auto, allow_outdated=allow_outdated)


@cli.command()
@click.option('--containerspec', metavar='CONTAINER_YAML', default='./container.yaml',
              help='path to container.yaml - defaults to ./container.yaml')
@click.option('--local-repo', metavar='REPO_PATH', type=Path,
              help="Path to repository location for dependencies and results - "
              "defaults to ./<arch>/rpms")
@click.option('--local-runtime', metavar='RUNTIME_TAR_GZ', type=Path,
              help="Path to local container build to use as runtime")
@click.option('--target', metavar='KOJI_TARGET',
              help=('Koji target for Flatpak **container** building. '
                    'Determined from runtime_version if missing.'))
@click.option('--auto', is_flag=True,
              help='Build all packages needed to build container')
@click.option('--allow-outdated', is_flag=True,
              help="Don't rebuild packages that are present but have an old version")
@click.argument('packages', nargs=-1, metavar="PKGS")
def build_rpms_local(
    containerspec: str, local_repo: Optional[Path],
    local_runtime: Optional[Path], target: Optional[str],
    packages: List[str], auto: bool, allow_outdated: bool
):
    """Rebuild rpms needed for the container locally"""

    spec = make_container_spec(containerspec)
    target = get_target(spec, target)

    manual_packages: List[str] = []
    manual_repos: List[Path] = []

    for pkg in packages:
        if '/' in pkg:
            manual_repos.append(Path(pkg))
        else:
            manual_packages.append(pkg)

    if local_repo is None:
        # Unlike for build_container_local(), we want to include the local_repo
        # even if it doesn't exist, since it's also the place where we store results
        local_repo = Path(Arch().rpm) / "rpms"

    build_context = AutoBuildContext(
        profile=get_profile(),
        container_spec=spec, local_repo=local_repo, local_runtime=local_runtime, target=target,
    )

    builder = RpmBuilder(build_context)
    if packages is [] and not auto:
        info("Nothing to rebuild, specify packages or --all-missing")
    else:
        builder.build_rpms_local(
            manual_packages, manual_repos, auto=auto, allow_outdated=allow_outdated
        )
