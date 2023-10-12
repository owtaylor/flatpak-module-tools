from dataclasses import dataclass
import logging
from pathlib import Path
import sys
from typing import List, Optional
from urllib.parse import urlparse

import click
from koji_cli.lib import activate_session

from flatpak_module_tools.git_utils import GitRepository

from .build_context import AutoBuildContext, ManualBuildContext
from .config import add_config_file, set_profile_name, get_profile
from .container_builder import ContainerBuilder
from .container_spec import ContainerSpec, ValidationError
from .console_logging import ConsoleHandler
from .flatpak_builder import (FLATPAK_METADATA_ANNOTATIONS,
                              FLATPAK_METADATA_BOTH,
                              FLATPAK_METADATA_LABELS)
from .flatpak_generator import FlatpakGenerator
from .koji_utils import watch_koji_task
from .installer import Installer
from .rpm_builder import RpmBuilder
from .utils import Arch, die, info, header, warn


@dataclass
class Paths:
    path: Path
    _local_repo: Optional[Path] = None
    _containerspec: Optional[Path] = None
    ignore_missing_local_repo: bool = False

    @property
    def workdir(self):
        archdir = self.path / Arch().rpm
        return archdir / "work"

    @property
    def oci_workdir(self):
        archdir = self.path / Arch().rpm
        return archdir / "work/oci"

    @property
    def resultdir(self):
        archdir = self.path / Arch().rpm
        return archdir / "result"

    @property
    def local_repo(self):
        if self._local_repo is not None:
            result = self.path / self._local_repo
        else:
            result = self.path / Path(Arch().rpm) / "rpms"

        if self.ignore_missing_local_repo:
            if not (result / "repodata/repomd.xml").exists():
                if self._local_repo is not None:
                    warn(f"No repository at {self._local_repo}, ignoring")

                return None

        return result

    @property
    def containerspec(self):
        if self._containerspec is not None:
            return self.path / self._containerspec
        else:
            return self.path / "container.yaml"


@dataclass
class CliData:
    path: Path

    @staticmethod
    def from_context(ctx: click.Context):
        assert isinstance(ctx.obj, CliData)
        return ctx.obj

    def paths(self, *, containerspec: Optional[Path] = None, local_repo: Optional[Path] = None,
              ignore_missing_local_repo: bool = True):
        """Create an appropriate Paths object for global and local options

        :param containerspec: --containerspec option - path to container.yaml
        :param local-repo: --local-repo option
        :param ignore_missing_local_repo: if True (the default), if the local
           repository is missing, ignore it unless --local-repo was specified
           in which case print a warning. If False, keep the specified or
           default local_repo value - this is for commands that *create*
           a local repository at the given location.
        """
        return Paths(path=self.path, _containerspec=containerspec, _local_repo=local_repo,
                     ignore_missing_local_repo=ignore_missing_local_repo)


def make_container_spec(paths: Paths):
    try:
        return ContainerSpec(paths.containerspec)
    except ValidationError as e:
        raise click.ClickException(str(e))
    except OSError as e:
        raise click.ClickException(str(e))


def get_target(paths: Paths,
               container_spec: ContainerSpec,
               target_option: Optional[str]) -> str:
    if target_option:
        return target_option
    else:
        profile = get_profile()

        if container_spec.flatpak.runtime_version:
            release = profile.release_from_runtime_version(
                container_spec.flatpak.runtime_version
            )
        else:
            try:
                merge_branch = GitRepository(paths.path).merge_branch
            except click.ClickException:
                die("Cannot determine git merge branch. "
                    "Must set flatpak:runtime_version in container.yaml "
                    "or specify --target")

            release = profile.release_from_runtime_version(merge_branch)
            if release == "":
                die(f"Cannot determine release from branch '{merge_branch}'. "
                    "Must set flatpak:runtime_version in container.yaml "
                    "or specify --target")

        return profile.get_flatpak_koji_target(release)


@click.group()
@click.option('-v', '--verbose', is_flag=True,
              help='Show verbose debugging output')
@click.option('-c', '--config', metavar='CONFIG_YAML', multiple=True,
              help='Additional configuration file to read')
@click.option('-p', '--profile', metavar='PROFILE_NAME', default='production',
              help='Alternate configuration profile to use')
@click.option("--path", metavar="PATH", type=Path,
              help="The directory to work in (defaults to current directory)")
@click.pass_context
def cli(ctx, verbose, config, profile, path: Optional[Path]):
    for c in reversed(config):
        add_config_file(c)

    set_profile_name(profile)
    try:
        get_profile()
    except KeyError:
        die(f"Unknown profile '{profile}'")

    if path is None:
        path = Path(".")
    ctx.obj = CliData(path=path)

    handlers = [ConsoleHandler()]

    if verbose:
        logging.basicConfig(level=logging.INFO, handlers=handlers)
    else:
        logging.basicConfig(handlers=handlers, level=logging.WARNING)
        logging.getLogger("flatpak_module_tools").setLevel(level=logging.INFO)


@cli.command
@click.option("--allow-outdated", is_flag=True,
              help="Continue even if included packages will have an old version")
@click.option("--arch", "arches", metavar="ARCH", type=str, multiple=True,
              help="Limit a scratch build to an arch. May be provided multiple times")
@click.option("--background", is_flag=True,
              help="Run the build at a low priority")
@click.option("--containerspec", metavar="CONTAINER_YAML", type=Path,
              help="Path to container.yaml - defaults to <path>/container.yaml")
@click.option("--nowait", is_flag=True,
              help="Don't wait on build")
@click.option("--scratch", is_flag=True,
              help="Scratch build")
@click.option("--skip-tag", is_flag=True,
              help="Do not attempt to tag build")
@click.option("--target", metavar="KOJI_TARGET",
              help="Koji target to build against. Determined from runtime_version if missing")
@click.pass_context
def build_container(ctx,
                    allow_outdated: bool,
                    arches: List[str],
                    background: bool,
                    containerspec: Optional[Path],
                    nowait: bool,
                    scratch: bool,
                    skip_tag: bool,
                    target: str):
    """Build a container in Koji"""

    paths = CliData.from_context(ctx).paths(containerspec=containerspec)
    profile = get_profile()

    # Need to be logged in to build
    activate_session(profile.koji_session, profile.koji_options)

    # Check that all changes are committed and pushed

    repository = GitRepository(paths.path)
    repository.check_clean()

    # Check that necessary dependencies are built

    container_spec = make_container_spec(paths)
    target = get_target(paths, container_spec, target)

    build_context = AutoBuildContext(
        profile=profile,
        container_spec=container_spec,
        target=target
    )

    if not container_spec.flatpak.build_runtime:
        rpm_builder = RpmBuilder(build_context, workdir=paths.workdir)
        rpm_builder.check(include_localrepo=False, allow_outdated=allow_outdated)

    # Determine the source URL

    parsed_origin_url = urlparse(repository.origin_url)
    src = profile.build_source_base + parsed_origin_url.path + "#" + repository.head_revision

    # Process options

    opts = {}
    if arches:
        if not scratch:
            die("--arch can only be specified for scratch builds")
        opts["arch_override"] = " ".join(arches)
    if scratch:
        opts["scratch"] = True
    if skip_tag:
        opts["skip_tag"] = True

    # Priority is relative to koji.PRIO_DEFAULT
    priority = 5 if background else None

    # Now build

    click.echo()
    header("Building")

    task_id = profile.koji_session.flatpakBuild(src, target, opts=opts, priority=priority)

    if not watch_koji_task(profile, task_id, nowait=nowait):
        sys.exit(1)


@cli.command
@click.option('--flatpak-metadata',
              type=click.Choice([FLATPAK_METADATA_LABELS,
                                 FLATPAK_METADATA_ANNOTATIONS,
                                 FLATPAK_METADATA_BOTH], case_sensitive=False),
              default=FLATPAK_METADATA_BOTH,
              help='How to store Flatpak metadata in the container')
@click.option('--containerspec', metavar='CONTAINER_YAML', type=Path,
              help='Path to container.yaml - defaults to <path>/container.yaml')
@click.option('--local-repo', metavar='REPO_PATH', type=Path,
              help="Path to repository location for local builds - defaults to <path>/<arch>/rpms")
@click.option('--local-runtime', metavar='RUNTIME_TAR_GZ', type=Path,
              help="Path to local container build to use as runtime")
@click.option('--target', metavar='KOJI_TARGET',
              help='Koji target to build against. Determined from runtime_version if missing.')
@click.option('--allow-outdated', is_flag=True,
              help="Continue even if included packages will have an old version")
@click.option('--install', is_flag=True,
              help='automatically install Flatpak for the current user')
@click.pass_context
def build_container_local(ctx,
                          flatpak_metadata, containerspec: Optional[Path],
                          local_repo: Optional[Path], local_runtime: Optional[Path],
                          target: str, install: bool, allow_outdated: bool):
    """Build a container from local and remote RPMs"""

    paths = CliData.from_context(ctx).paths(containerspec=containerspec, local_repo=local_repo)
    container_spec = make_container_spec(paths)
    target = get_target(paths, container_spec, target)

    build_context = AutoBuildContext(
        profile=get_profile(),
        container_spec=container_spec, local_repo=paths.local_repo,
        local_runtime=local_runtime, target=target
    )

    if not container_spec.flatpak.build_runtime:
        rpm_builder = RpmBuilder(build_context, workdir=paths.workdir)
        rpm_builder.check(include_localrepo=True, allow_outdated=allow_outdated)

    container_builder = ContainerBuilder(build_context, flatpak_metadata=flatpak_metadata)
    tarfile = container_builder.build(workdir=paths.oci_workdir, resultdir=paths.resultdir)

    if install:
        installer = Installer(profile=get_profile())
        installer.set_source_path(tarfile)
        installer.install()


@cli.command(name="assemble")
@click.option('--containerspec', metavar='CONTAINER_YAML', type=Path,
              help='Path to container.yaml - defaults to <path>/container.yaml')
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
@click.pass_context
def assemble(
    ctx,
    containerspec: Optional[Path],
    target: Optional[str],
    nvr: Optional[str],
    runtime_nvr: Optional[str],
    runtime_repo: Optional[int],
    app_repo: Optional[int],
    installroot: Path,
    workdir: Path,
    resultdir: Path,
):
    """Run as root inside a container to create the OCI"""

    paths = CliData.from_context(ctx).paths(containerspec=containerspec)
    container_spec = make_container_spec(paths)

    if nvr or runtime_nvr or runtime_repo or app_repo:
        if target:
            die("--target cannot be specified together with "
                "--nvr, --runtime-nvr, --runtime-repo, or --app-repo")

        if container_spec.flatpak.build_runtime:
            if not nvr or not runtime_repo:
                die("--nvr and --runtime-repo must be specified for runtimes")
            if runtime_nvr or app_repo:
                die("--runtime-nvr and --app-repo must not be specified for runtimes")

        else:
            if not nvr or not runtime_nvr or not runtime_repo or not app_repo:
                die("--nvr, --runtime-nvr, --runtime-repo, and --app-repo "
                    "must be specified for applications")

        build_context = ManualBuildContext(
            profile=get_profile(), container_spec=container_spec,
            nvr=nvr, runtime_nvr=runtime_nvr, runtime_repo=runtime_repo, app_repo=app_repo
        )

    else:
        target = get_target(paths, container_spec, target)
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
@click.option('--containerspec', metavar='CONTAINER_YAML', type=Path,
              help='Path to container.yaml - defaults to <path>/container.yaml')
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
@click.pass_context
def build_rpms(
    ctx,
    containerspec: Optional[Path], local_runtime: Optional[Path], target: Optional[str],
    packages: List[str], auto: bool, allow_outdated
):
    """Rebuild rpms needed for the container in Koji"""

    paths = CliData.from_context(ctx).paths(containerspec=containerspec)

    spec = make_container_spec(paths)
    target = get_target(paths, spec, target)

    if not packages and not auto:
        info("Nothing to rebuild, specify packages or --auto")
        return

    build_context = AutoBuildContext(
        profile=get_profile(),
        container_spec=spec, local_runtime=local_runtime, target=target
    )

    builder = RpmBuilder(build_context, workdir=paths.workdir)
    builder.build_rpms(packages, auto=auto, allow_outdated=allow_outdated)


@cli.command()
@click.option('--containerspec', metavar='CONTAINER_YAML', type=Path,
              help='Path to container.yaml - defaults to <path>/container.yaml')
@click.option('--local-repo', metavar='REPO_PATH', type=Path,
              help="Path to repository location for dependencies and results - "
              "defaults to <path>/<arch>/rpms")
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
@click.pass_context
def build_rpms_local(
    ctx,
    containerspec: Optional[Path], local_repo: Optional[Path],
    local_runtime: Optional[Path], target: Optional[str],
    packages: List[str], auto: bool, allow_outdated: bool
):
    """Rebuild rpms needed for the container locally"""

    paths = CliData.from_context(ctx).paths(containerspec=containerspec, local_repo=local_repo,
                                            ignore_missing_local_repo=False)
    spec = make_container_spec(paths)
    target = get_target(paths, spec, target)

    if not packages and not auto:
        info("Nothing to rebuild, specify packages or --auto")
        return

    manual_packages: List[str] = []
    manual_repos: List[Path] = []

    for pkg in packages:
        if '/' in pkg:
            manual_repos.append(Path(pkg))
        else:
            manual_packages.append(pkg)

    build_context = AutoBuildContext(
        profile=get_profile(),
        container_spec=spec, local_repo=paths.local_repo, local_runtime=local_runtime,
        target=target,
    )

    builder = RpmBuilder(build_context, workdir=paths.workdir)
    builder.build_rpms_local(
        manual_packages, manual_repos, auto=auto, allow_outdated=allow_outdated
    )


@cli.command()
@click.option("--flathub", metavar="ID_OR_SEARCH_TERM",
              help="Initialize from a Flathub Flatpak.")
@click.option("--runtime-name", metavar="RUNTIME",
              help="Specify runtime-name, defaults to 'flatpak-runtime'")
@click.option("--runtime-version", metavar="VERSION",
              help="Specify runtime-version, defaults to latest stable release")
@click.option("--output-containerspec", metavar="FILE",
              help="Write container specification to FILE"
                   " instead of container.yaml.")
@click.option("--force", "-f", is_flag=True,
              help="Overwriting existing output files")
@click.argument("package", metavar='PACKAGE', required=True)
@click.pass_context
def init(
    ctx,
    output_containerspec: Optional[Path],
    flathub: Optional[str],
    force: bool,
    runtime_name: Optional[str],
    runtime_version: Optional[str],
    package: List[str]
):
    """Generate container.yaml from an RPM"""
    fg = FlatpakGenerator(package)
    fg.run(output_containerspec, force=force, flathub=flathub, runtime_name=runtime_name, runtime_version=runtime_version)
