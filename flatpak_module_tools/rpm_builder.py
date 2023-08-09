import json
from pathlib import Path
import subprocess
from typing import Collection, List

import click
import koji
import networkx

from .build_scheduler import KojiBuildScheduler, MockBuildScheduler
from .build_context import BuildContext
from .console_logging import Status
from .mock import make_mock_cfg
from .utils import get_arch, error


_FLAGS_TO_REL = {
    koji.RPMSENSE_LESS: "<",
    koji.RPMSENSE_LESS | koji.RPMSENSE_EQUAL: "<=",
    koji.RPMSENSE_EQUAL: "=",
    koji.RPMSENSE_GREATER: ">",
    koji.RPMSENSE_GREATER | koji.RPMSENSE_EQUAL: ">=",
}


def flags_to_rel(flags):
    return _FLAGS_TO_REL[flags & (koji.RPMSENSE_LESS | koji.RPMSENSE_EQUAL | koji.RPMSENSE_GREATER)]


RPMSENSE_RPMLIB = (1 << 24)  # rpmlib(feature) dependency.


def print_explanation(explanation, prefix, buildrequiring=None):
    if explanation is None:
        print(f"{prefix}<in input>")
    else:
        if len(explanation) % 2 == 0:
            provide = explanation[0]
            provided_by = explanation[1]
            print(f"{prefix}{buildrequiring} buildrequires {provide}, provided by {provided_by}")
            start = 1
        else:
            start = 0

        for i in range(start, len(explanation) - 2, 2):
            required_by = explanation[i]
            provide = explanation[i + 1]
            provided_by = explanation[i + 2]
            print(f"{prefix}{required_by} requires {provide}, provided by {provided_by}")


def check_for_cycles(build_after, build_after_details):
    if len(build_after) == 1:
        # No need to buildorder a single SRPM. There might be a cycle from
        # the SRPM to itself, but we assume that we don't care. (We could
        # try to ignore such cycles more generally - might get tricky.)
        return False

    G = networkx.DiGraph()
    G.add_nodes_from(build_after)
    for package, after in build_after.items():
        for name in after:
            G.add_edge(package, name)

    cycles = list()
    cycles_iter = networkx.simple_cycles(G)  # type: ignore
    for cycle in cycles_iter:
        cycles.append(cycle)
        if len(cycles) == 25:
            break

    cycles.sort(key=lambda x: len(x))
    for c in cycles[0:5]:
        error("Found cycle")
        for i, x in enumerate(c):
            y = c[(i + 1) % len(c)]
            print(f"    {x} ⇒ {y}")
            print_explanation(
                build_after_details[x][y][0]["explanation"],
                prefix="        ",
                buildrequiring=x
            )
        print()

    if len(cycles) > 5:
        print("More than 5 cycles found, ignoring additional cycles")

    if len(cycles) > 0:
        raise click.ClickException("Cannot determine build order because of cycles")


class RpmBuilder:
    def __init__(self, context: BuildContext):
        self.context = context
        self.profile = context.profile
        self.flatpak_spec = context.flatpak_spec
        self.repo_path = Path.cwd() / get_arch().rpm / "rpms"
        self.workdir = Path.cwd() / get_arch().rpm / "work"

    def _run_depchase(self, cmd: str, args: List[str], *,
                      include_localrepo: bool,
                      include_packages: bool,
                      refresh: str = "missing"):
        if include_packages:
            packages_file = self.workdir / \
                f"{self.flatpak_spec.runtime_name}-{self.flatpak_spec.runtime_version}.packages"
            with open(packages_file, "w") as f:
                for pkg in self.context.runtime_packages:
                    print(pkg, file=f)
            packages = ["--preinstalled", packages_file]
        else:
            packages = []

        arch = get_arch()

        local_repo = []
        if include_localrepo:
            local_repo_path = Path(arch.rpm) / "rpms"
            if (local_repo_path / "repodata/repomd.xml").exists():
                local_repo = [f"--local-repo=local:{local_repo_path}"]

        rpm_build_tag = self.context.app_build_repo["tag_name"]
        return subprocess.check_output(
            ["flatpak-module-depchase",
                f"--profile={self.profile.name}",
                f"--arch={arch.oci}",
                f"--tag={rpm_build_tag}",
                f"--refresh={refresh}"] + local_repo + [cmd] + packages + args,
            encoding="utf-8"
        )

    def _refresh_metadata(self, include_localrepo: bool = True):
        self._run_depchase(
            "fetch-metadata", [],
            include_localrepo=include_localrepo,
            include_packages=False,
            refresh="always"
        )

    def _find_missing_packages(self, manual_packages: List[str] = [], *,
                               confirm: bool = True, include_localrepo: bool):
        # Access first to get logging output in the right order
        self.context.runtime_packages

        packages = self.flatpak_spec.get_packages_for_arch(get_arch())
        with Status(f"Finding dependencies of {', '.join(packages)} not in runtime"):
            output = self._run_depchase(
                "resolve-packages",
                [
                    "--json",
                    "--source",
                ] + packages,
                include_localrepo=include_localrepo,
                include_packages=True,
            )
            data = json.loads(output)

        print("Needed for installation:")

        to_rebuild = set(manual_packages)
        for source_rpm, binary_rpm_details in data.items():
            all_rebuilt = True
            for details in binary_rpm_details:
                release_arch = details["nvra"].rsplit("-", 2)[2]
                release = release_arch.rsplit(".", 1)[0]
                if details["repo"] == "local":
                    repo = " (local)"
                else:
                    repo = ""
                if not release.endswith("app"):
                    all_rebuilt = False
                    click.secho(f"    {details['nvra']}{repo}", bold=True)
                else:
                    click.secho(f"    {details['nvra']}{repo}")
            if not all_rebuilt:
                to_rebuild.add(source_rpm)

        print("To rebuild:", ", ".join(sorted(to_rebuild)))

        if not to_rebuild:
            return set()

        while confirm:
            choice = click.prompt("Proceed?", type=click.Choice(["y", "n", "?"]))
            if choice == "y":
                break
            if choice == "n":
                return set()
            else:
                for source_rpm in sorted(to_rebuild):
                    print(source_rpm)
                    if source_rpm in manual_packages:
                        print("    <specified manually>")
                    else:
                        for details in data[source_rpm]:
                            print(f"    {details['name']}")
                            if "explanation" in details:
                                print_explanation(details["explanation"], "        ")
                            else:
                                print("        <from container.yaml>")

        return to_rebuild

    def _get_latest_builds(self, to_build: Collection[str]):
        session = self.profile.source_koji_session
        source_tag = self.profile.get_source_koji_tag(self.context.release)
        with Status("Getting latest builds from koji"):
            return {
                package: session.listTagged(
                                source_tag, package=package, inherit=True, latest=True
                            )[0]
                for package in sorted(to_build)
            }

    def get_build_requires(self, build_id):
        session = self.profile.source_koji_session
        latest_src_rpm = session.listRPMs(build_id, arches=["src"])[0]
        result: List[str] = []

        deps = session.getRPMDeps(latest_src_rpm["id"], depType=koji.DEP_REQUIRE)
        for dep in deps:
            if dep["flags"] & RPMSENSE_RPMLIB != 0:
                continue
            if dep["version"] != "":
                result.append(f"{dep['name']} {flags_to_rel(dep['flags'])} {dep['version']}")
            else:
                result.append(dep["name"])

        return result

    def _compute_build_order(self, latest_builds, *, include_localrepo: bool):
        with Status("Getting build requirements from koji"):
            build_requires_map = {}
            for package in latest_builds:
                build_requires_map[package] = \
                    self.get_build_requires(latest_builds[package]["id"])

            build_after = {}
            build_after_details = {}

        with Status("") as status:
            EXPANDING_MESSAGE = "Expanding build requirements to determine build order"

            for package in latest_builds:
                status.message = f"{EXPANDING_MESSAGE}: {package}"
                build_requires = build_requires_map[package]
                if not build_requires:
                    build_after[package] = set()
                    build_after_details[package] = {}
                    continue

                output = self._run_depchase(
                    "resolve-requires",
                    [
                        "--source",
                        "--json",
                    ] + build_requires,
                    include_localrepo=include_localrepo,
                    include_packages=True)

                resolved_build_requires = json.loads(output)
                after = {
                    required_name for required_name in resolved_build_requires
                    if required_name != package and required_name in latest_builds
                }
                build_after[package] = after

                build_after_details[package] = {
                    required_name:
                        details for required_name, details in resolved_build_requires.items()
                    if required_name != package and required_name in latest_builds
                }

            status.message = EXPANDING_MESSAGE

        check_for_cycles(build_after, build_after_details)

        return build_after

    def check(self):
        pass

    def build_rpms(
            self, manual_packages: List[str], all_missing=False
    ):
        self._refresh_metadata(include_localrepo=False)

        # FIXME - probably should use a temporary workdir in this case
        self.workdir.mkdir(parents=True, exist_ok=True)

        if all_missing:
            to_build = self._find_missing_packages(manual_packages, include_localrepo=False)
        else:
            to_build = set(manual_packages)

        if not to_build:
            return

        latest_builds = self._get_latest_builds(to_build)
        build_after = self._compute_build_order(latest_builds, include_localrepo=False)

        target = self.profile.get_rpm_koji_target(self.context.release)
        builder = KojiBuildScheduler(
            profile=self.profile,
            target=target,
            build_after=build_after
        )

        for package_name, package in latest_builds.items():
            builder.add_koji_item(package["nvr"])

        builder.build()

    def build_rpms_local(
            self, manual_packages: List[str], manual_repos: List[Path], all_missing=False
    ):
        self.repo_path.mkdir(parents=True, exist_ok=True)
        self.workdir.mkdir(parents=True, exist_ok=True)

        self._refresh_metadata()

        self.context.runtime_packages

        repo_map = {
            repo.name: repo for repo in manual_repos
        }
        all_manual_packages = list(manual_packages)
        all_manual_packages.extend(repo_map.keys())

        if all_missing:
            to_build = self._find_missing_packages(all_manual_packages, include_localrepo=True)
        else:
            to_build = set(manual_packages)

        if not to_build:
            return

        latest_builds = self._get_latest_builds(to_build)

        build_after = self._compute_build_order(latest_builds, include_localrepo=True)

        mock_cfg = make_mock_cfg(
            arch=get_arch(),
            chroot_setup_cmd="install @build",
            releasever=self.context.release,
            repos=self.context.get_repos(for_container=False),
            root_cache_enable=False,
            runtimever=self.context.runtime_info.version
        )

        builder = MockBuildScheduler(
            mock_cfg=mock_cfg,
            profile=self.profile,
            repo_path=self.repo_path,
            build_after=build_after
        )
        for package_name, package in latest_builds.items():
            if package_name in repo_map:
                builder.add_repo_item(repo_map[package_name])
            else:
                builder.add_koji_item(package["nvr"])  # type: ignore

        builder.build()
