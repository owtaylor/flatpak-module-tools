from configparser import RawConfigParser
from functools import cached_property
import json
from pathlib import Path
import subprocess
from textwrap import dedent
from typing import Collection, List

import click
import koji
import networkx

from .build_scheduler import MockBuildScheduler
from .container_spec import ContainerSpec
from .console_logging import Status
from .mock import make_mock_cfg
from .utils import get_arch, die, error, RuntimeInfo


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

    return len(cycles) > 0


class RpmBuilder:
    def __init__(self, *, profile, container_spec: ContainerSpec, target: str | None = None):
        self.profile = profile
        self._koji_session = None
        self.flatpak_spec = container_spec.flatpak
        self.repo_path = Path.cwd() / get_arch().rpm / "rpms"
        self.workdir = Path.cwd() / get_arch().rpm / "work"

        assert self.flatpak_spec.runtime_version
        release = self.profile.release_from_runtime_version(self.flatpak_spec.runtime_version)

        target = self.profile.get_rpm_koji_target(release)
        assert isinstance(target, str)
        self.rpm_koji_target = target

        target = self.profile.get_flatpak_koji_target(release)
        assert isinstance(target, str)
        self.flatpak_koji_target = target

    @cached_property
    def _flatpak_target_info(self):
        return self.profile.koji_session.getBuildTarget(self.flatpak_koji_target)

    @cached_property
    def _rpm_target_info(self):
        return self.profile.koji_session.getBuildTarget(self.rpm_koji_target)

    @cached_property
    def _image_archive(self):
        session = self.profile.koji_session
        dest_tag = self._flatpak_target_info["dest_tag_name"]
        tagged_builds = session.listTagged(
            dest_tag, package=self.flatpak_spec.runtime_name, latest=True, inherit=True,
        )
        if len(tagged_builds) == 0:
            die(f"Can't find build for {self.flatpak_spec.runtime_name} in {dest_tag}")

        latest_build = tagged_builds[0]

        archives = session.listArchives(buildID=latest_build["build_id"])
        return next(a for a in archives if a["extra"]["image"]["arch"] == "x86_64")

    @cached_property
    def runtime_packages(self):
        with Status("Listing runtime packages"):
            session = self.profile.koji_session
            rpms = session.listRPMs(imageID=self._image_archive["id"])
            return sorted(rpm["name"] for rpm in rpms)

    @cached_property
    def runtime_info(self):
        labels = self._image_archive["extra"]["docker"]["config"]["config"]["Labels"]
        cp = RawConfigParser()
        cp.read_string(labels["org.flatpak.metadata"])

        runtime = cp.get("Runtime", "runtime")
        assert isinstance(runtime, str)
        runtime_id, runtime_arch, runtime_version = runtime.split("/")

        sdk = cp.get("Runtime", "sdk")
        assert isinstance(sdk, str)
        sdk_id, sdk_arch, sdk_version = sdk.split("/")

        return RuntimeInfo(runtime_id=runtime_id, sdk_id=sdk_id, version=runtime_version)

    def _run_depchase(self, cmd: str, args: List[str]):
        packages_file = self.workdir / \
              f"{self.flatpak_spec.runtime_name}-{self.flatpak_spec.runtime_version}.packages"
        with open(packages_file, "w") as f:
            for pkg in self.runtime_packages:
                print(pkg, file=f)

        return subprocess.check_output(
            ["flatpak-module-depchase",
                "--local-repo=local:x86_64/rpms",
                cmd,
                "--preinstalled", packages_file] + args,
            encoding="utf-8"
        )

    def _find_missing_packages(self, manual_packages: List[str] = [], confirm=True):
        # Access first to get logging output in the right order
        self.runtime_packages

        packages = self.flatpak_spec.packages
        with Status(f"Finding dependencies of {', '.join(packages)} not in runtime"):
            output = self._run_depchase(
                "resolve-packages",
                [
                    "--json",
                    "--source",
                ] + packages
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
        session = self.profile.koji_session
        build_tag = self._rpm_target_info["build_tag_name"]

        with Status("Getting latest builds from koji"):
            return {
                package: session.listTagged(
                                build_tag, package=package, inherit=True, latest=True
                            )[0]
                for package in sorted(to_build)
            }

    def get_main_package_nvr(self):
        packages = self.flatpak_spec.packages
        output = self._run_depchase(
            "resolve-packages",
            [
                "--json",
            ] + [packages[0]]
        )

        package_info = [p for p in json.loads(output) if p["name"] == packages[0]][0]
        nvra = package_info["nvra"]
        n, v, ra = nvra.rsplit("-", 2)
        r, a = ra.rsplit(".", 1)
        return f"{n}-{v}-{r}"

    def get_build_requires(self, build_id):
        session = self.profile.koji_session
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

    def get_repos(self, *, for_container: bool):
        repos: List[str] = []

        if for_container:
            flatpak_build_tag = self._flatpak_target_info["build_tag_name"]
            repos.append(dedent(f"""\
                [{flatpak_build_tag}]
                name={flatpak_build_tag}
                baseurl=https://kojipkgs.fedoraproject.org/repos/{flatpak_build_tag}/latest/$basearch/
                priority=10
                enabled=1
                skip_if_unavailable=False
                includepkgs={",".join(sorted(self.runtime_packages))}
            """))

        if not for_container:
            rpm_build_tag = self._rpm_target_info["build_tag_name"]
            repos.append(dedent(f"""\
                [{rpm_build_tag}]
                name={rpm_build_tag}
                baseurl=https://kojipkgs.fedoraproject.org/repos/{rpm_build_tag}/latest/$basearch/
                priority=10
                enabled=1
                skip_if_unavailable=False
            """))

        repos.append(dedent(f"""\
            [local]
            name=local
            priority=20
            baseurl={self.repo_path}
            enabled=1
            skip_if_unavailable=False
        """))

        return repos

    def check(self):
        pass

    def build_rpms(self):
        pass

    def build_rpms_local(
            self, manual_packages: List[str], manual_repos: List[Path], all_missing=False
    ):
        self.runtime_packages

        repo_map = {
            repo.name: repo for repo in manual_repos
        }
        all_manual_packages = list(manual_packages)
        all_manual_packages.extend(repo_map.keys())

        if all_missing:
            to_build = self._find_missing_packages(all_manual_packages)
        else:
            to_build = set(manual_packages)

        if not to_build:
            return

        latest_builds = self._get_latest_builds(to_build)

        with Status("Getting build requirements from koji"):
            build_requires_map = {}
            for package in to_build:
                build_requires_map[package] = \
                    self.get_build_requires(latest_builds[package]["id"])

            build_after = {}
            build_after_details = {}

        with Status("") as status:
            EXPANDING_MESSAGE = "Expanding build requirements to determine build order"

            for package in to_build:
                status.message = f"{EXPANDING_MESSAGE}: {package}"
                build_requires = build_requires_map[package]
                if not build_requires:
                    build_after[package] = set()

                output = self._run_depchase(
                    "resolve-requires",
                    [
                        "--source",
                        "--json",
                    ] + build_requires)

                resolved_build_requires = json.loads(output)
                after = {
                    required_name for required_name in resolved_build_requires
                    if required_name != package and required_name in to_build
                }
                build_after[package] = after

                build_after_details[package] = {
                    required_name:
                        details for required_name, details in resolved_build_requires.items()
                    if required_name != package and required_name in to_build
                }

            status.message = EXPANDING_MESSAGE

        if check_for_cycles(build_after, build_after_details):
            return

        # FIXME: workaround until we have an appropriate build tag with the right @build
        FLATPAK_RPM_MACROS = "https://kojipkgs.fedoraproject.org//work/tasks/6924/100776924/" + \
            "flatpak-rpm-macros-39-1.fc39.x86_64.rpm"

        mock_cfg = make_mock_cfg(
            arch=get_arch(),
            chroot_setup_cmd=f"install @build {FLATPAK_RPM_MACROS}",
            includepkgs=(),
            releasever=self.profile.release_from_runtime_version(self.runtime_info.version),
            repos=self.get_repos(for_container=False),
            root_cache_enable=False,
            runtimever=self.runtime_info.version
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
