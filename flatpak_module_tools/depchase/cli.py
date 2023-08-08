from collections import defaultdict
from dataclasses import dataclass
import json
import logging
import sys
from typing import DefaultDict, Dict, List, Tuple

import click

from ..config import add_config_file, set_profile_name, get_profile
from ..utils import Arch, die, get_arch, rpm_name_only
from . import depchase, fetchrepodata


def read_preinstalled_packages(runtime_profile):
    runtime_packages = set()
    with open(runtime_profile, "r") as f:
        for line in f:
            stripped = line.strip()
            if stripped != "" and stripped != "flatpak-runtime-config":
                runtime_packages.add(stripped)
    return runtime_packages


@dataclass
class CliData:
    tag: str
    arch: Arch
    refresh: fetchrepodata.Refresh
    local_repos: List[str]

    @staticmethod
    def from_context(ctx: click.Context):
        assert isinstance(ctx.obj, CliData)
        return ctx.obj

    def download_repo_metadata(self):
        fetchrepodata.download_repo_metadata(self.tag, self.arch, self.refresh)

    def get_metadata_location(self):
        return fetchrepodata.get_metadata_location(self.tag, self.arch)

    def make_pool(self):
        self.download_repo_metadata()
        return depchase.make_pool(self.tag, self.arch, self.local_repos)


@click.group()
@click.option(
    '-v', '--verbose', is_flag=True,
    help='Show verbose debugging output'
)
@click.option(
    '-c', '--config', metavar='CONFIG_YAML', multiple=True,
    help='Additional configuration file to read'
)
@click.option(
    '-p', '--profile', metavar='PROFILE_NAME', default='production',
    help='Alternate configuration profile to use'
)
@click.option(
    '-t', '--tag', metavar='KOJI_TAG', required=True,
    help='Koji build tag to use as package source'
)
@click.option('-a', '--arch', help='Architecture')
@click.option(
    '--refresh', type=click.Choice(['missing', 'always', 'auto']), default='auto',
    help="Whether to refresh metadata (only if missing, always, periodically)"
)
@click.option(
    '-l', '--local-repo', metavar='NAME:REPO_PATH', multiple=True,
    help="Add a local repository as a source for package resolution"
)
@click.pass_context
def cli(ctx, verbose, config, profile, tag, arch, refresh, local_repo):
    for c in reversed(config):
        add_config_file(c)

    set_profile_name(profile)
    try:
        get_profile()
    except KeyError:
        die(f"Unknown profile '{profile}'")

    refresh = fetchrepodata.Refresh[refresh.upper()]
    ctx.obj = CliData(
        arch=get_arch(arch), tag=tag, refresh=refresh, local_repos=local_repo
    )

    if verbose:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.basicConfig(level=logging.WARNING)


class PackageInfo:
    def __init__(self, name):
        self.name = name
        self.nvra = None
        self.repo = None
        self.source = None
        self.explanation: List[str] | None = None
        self.required_by: List[Tuple[str, str]] = []

    def print_explanation(self, prefix):
        if self.explanation is None:
            print(f"{prefix}<in input>")
        else:
            if len(self.explanation) % 2 == 0:
                provide = self.explanation[0]
                provided_by = self.explanation[1]
                print(f"{prefix}{provide} is provided by {provided_by}")
                start = 1
            else:
                start = 0

            for i in range(start, len(self.explanation) - 2, 2):
                required_by = self.explanation[i]
                provide = self.explanation[i + 1]
                provided_by = self.explanation[i + 2]
                print(f"{prefix}{required_by} requires {provide}, provided by {provided_by}")

    def to_json(self, include_source=True):
        result = {
            "name": self.name,
            "nvra": self.nvra,
            "repo": self.repo,
        }
        if include_source:
            result["source"] = self.source
        if self.explanation:
            result["explanation"] = self.explanation

        return result


def collate_packages(
    transaction, *,
    requested_packages=None,
    requested_requires=None,
    preinstalled_packages=None
):
    packages: Dict[str, PackageInfo] = {}

    all_needed_packages = transaction.get_full_infos()
    new_package_infos = [p for p in all_needed_packages if p.name not in preinstalled_packages]

    requested_for_requires = {}
    if requested_requires:
        for dep in requested_requires:
            for pkg in transaction.get_packages_providing_dep(dep):
                requested_for_requires[rpm_name_only(pkg)] = dep

    for p in new_package_infos:
        srpm = rpm_name_only(p.srpm)
        package_info = packages.get(p.name, None)
        if package_info is None:
            package_info = PackageInfo(p.name)
            package_info.nvra = p.rpm
            package_info.repo = p.repo
            package_info.source = srpm
            packages[p.name] = package_info
        else:
            package_info.nvra = p.rpm
            package_info.repo = p.repo
            package_info.source = srpm

        for requirement, provided_bys in p.requires.items():
            for provided_by in provided_bys:
                other_name = rpm_name_only(provided_by)
                if preinstalled_packages and other_name in preinstalled_packages:
                    continue
                other = packages.get(other_name)
                if other is None:
                    other = PackageInfo(other_name)
                    packages[other_name] = other
                other.required_by.append((p.name, requirement))

    def get_explanation(package_name, seen=None):
        if seen is None:
            seen = []

        dep = requested_for_requires.get(package_name)
        if dep:
            return [dep]

        for requiring_package, req in packages[package_name].required_by:
            if requiring_package in seen:
                continue
            if requested_packages and requiring_package in requested_packages:
                return [requiring_package, req]
            else:
                explanation = get_explanation(requiring_package, seen + [requiring_package])
                if explanation is None:
                    continue
                explanation.extend((requiring_package, req))
                return explanation

        return None

    for package in packages.values():
        if not requested_packages or package.name not in requested_packages:
            package.explanation = get_explanation(package.name)
            assert package.explanation is not None
            package.explanation.append(package.name)

    return packages


def print_packages(packages, json_output, source):
    if source:
        source_packages: DefaultDict[str, List[PackageInfo]] = defaultdict(list)
        for package in packages.values():
            assert package.source
            source_packages[package.source].append(package)

        if json_output:
            json.dump(
                {
                    k: [p.to_json(include_source=False)
                        for p in sorted(source_packages[k], key=lambda package: package.name)]
                    for k in sorted(source_packages)
                },
                sys.stdout, indent=4
            )
        else:
            for source_name in sorted(source_packages):
                print(source_name)
                packages_for_source = source_packages[source_name]
                for package in sorted(packages_for_source, key=lambda package: package.name):
                    print("    " + package.name)
                    package.print_explanation("        ")
    else:
        if json_output:
            json.dump(
                [packages[p].to_json() for p in sorted(packages.keys())], sys.stdout, indent=4
            )
        else:
            for package_name in sorted(packages):
                package = packages[package_name]
                print(package_name)
                package.print_explanation("     ")


@cli.command("resolve-packages")
@click.option(
    "--json", 'json_output', is_flag=True, default=False,
    help="Output dependencies in JSON format"
)
@click.option(
    "--source", is_flag=True, default=False, help="Group output by source package"
)
@click.option(
    "--ignore-requires", metavar="PKG:DEP", multiple=True,
    help="Ignore the dependecy of PKG on DEP."
)
@click.option(
    "--preinstalled", metavar='PACKAGE_LIST', required=False,
    help="List of packages to assume that are already installed"
)
@click.argument("pkgs", metavar='PKGS', nargs=-1, required=True)
@click.pass_context
def resolve_packages(ctx, pkgs, json_output, source, ignore_requires, preinstalled):
    """Resolve all dependencies for a set of packages"""
    pool = CliData.from_context(ctx).make_pool()
    for x in ignore_requires:
        pkg, dep = x.split(':', 1)
        depchase.remove_requires(pool, pkg, dep)

    transaction = depchase.Transaction(pool)
    transaction.add_packages(pkgs)
    if preinstalled:
        preinstalled_packages = read_preinstalled_packages(preinstalled)
        transaction.set_hints(preinstalled_packages)
    else:
        preinstalled_packages = []

    transaction.solve()

    packages = collate_packages(
        transaction, requested_packages=pkgs, preinstalled_packages=preinstalled_packages
    )
    print_packages(packages, json_output, source)


@cli.command("resolve-requires")
@click.option(
    "--json", 'json_output', is_flag=True, default=False,
    help="Output dependencies in JSON format"
)
@click.option(
    "--source", is_flag=True, default=False, help="Group output by source package"
)
@click.option(
    "--ignore-requires", metavar="PKG:DEP", multiple=True,
    help="Ignore the dependecy of PKG on DEP."
)
@click.option(
    "--preinstalled", metavar='PACKAGE_LIST', required=False,
    help="List of packages to assume that are already installed"
)
@click.argument("requires", metavar='REQUIRES', nargs=-1, required=True)
@click.pass_context
def resolve_requires(ctx, requires, json_output, source, ignore_requires, preinstalled):
    """Resolve all packages to be installed list of requirements"""
    pool = CliData.from_context(ctx).make_pool()
    for x in ignore_requires:
        pkg, dep = x.split(':', 1)
        depchase.remove_requires(pool, pkg, dep)

    transaction = depchase.Transaction(pool)
    transaction.add_provides(requires)
    if preinstalled:
        preinstalled_packages = read_preinstalled_packages(preinstalled)
        transaction.set_hints(preinstalled_packages)
    else:
        preinstalled_packages = []

    transaction.solve()

    packages = collate_packages(
        transaction, requested_requires=requires, preinstalled_packages=preinstalled_packages
    )
    print_packages(packages, json_output, source)


@cli.command
@click.option("--runtime-profile", metavar='PROFILE_FILE', required=True)
@click.argument("pkgs", metavar='PKGS', nargs=-1, required=True)
@click.pass_context
def flatpak_report(ctx, pkgs, runtime_profile, quiet=False):
    if not quiet:
        print("Initializing", file=sys.stderr)
    runtime_packages = read_preinstalled_packages(runtime_profile)
    pool = CliData.from_context(ctx).make_pool()

    packages = {}
    flatpaks = {}

    for p in runtime_packages:
        packages[p] = {
            'name': p,
            'runtime': True,
            'used_by': [],
            'srpm': rpm_name_only(depchase.get_srpm_for_rpm(pool, p)),
        }

    for pkg in pkgs:
        if not quiet:
            print("Calculating deps for", pkg, file=sys.stderr)

        transaction = depchase.Transaction(pool)
        transaction.add_packages(pkgs)
        transaction.set_hints(runtime_packages)
        transaction.solve()
        all_needed_packages = transaction.get_packages()

        assert isinstance(all_needed_packages, set)
        extra_packages = all_needed_packages - runtime_packages - {pkg}
        used_runtime_packages = all_needed_packages - extra_packages - {pkg}
        flatpaks[pkg] = {
            'runtime': sorted(used_runtime_packages),
            'extra': sorted(extra_packages),
        }
        flatpaks[pkg]['srpm'] = rpm_name_only(depchase.get_srpm_for_rpm(pool, pkg))
        for p in all_needed_packages:
            if p == pkg:
                continue
            data = packages.get(p, None)
            if data is None:
                data = {
                    'name': p,
                    'runtime': False,
                    'used_by': [],
                    'srpm': rpm_name_only(depchase.get_srpm_for_rpm(pool, p)),
                }
                packages[p] = data
            data['used_by'].append(pkg)

    json.dump({
        'packages': packages,
        'flatpaks': flatpaks
    }, sys.stdout, indent=4)


@cli.command
@click.option(
    "--print-location", is_flag=True, default=False,
    help="Output dependencies in JSON format"
)
@click.pass_context
def fetch_metadata(ctx, print_location):
    """Fetch latest repository metadata"""

    cli_data = CliData.from_context(ctx)

    cli_data.download_repo_metadata()
    if print_location:
        print(cli_data.get_metadata_location())
