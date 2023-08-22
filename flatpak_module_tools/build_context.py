from abc import ABC, abstractmethod
from configparser import RawConfigParser
from functools import cached_property
import json
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, List, Optional
from click import ClickException

import koji

from flatpak_module_tools.package_locator import PackageLocator

from .config import ProfileConfig
from .container_spec import ContainerSpec, Option
from .console_logging import Status
from .utils import Arch, RuntimeInfo


def _baseurl(profile: ProfileConfig, repo: Dict):
    pathinfo = koji.PathInfo(topdir=profile.koji_options['topurl'])

    if repo["dist"]:
        return pathinfo.distrepo(repo["id"], repo["tag_name"], None) + "/$basearch/"
    else:
        return pathinfo.repo(repo["id"], repo["tag_name"]) + "/$basearch/"


class BuildContext(ABC):
    """Holds information about what a Flatpak will be built against."""
    def __init__(self, *, profile: ProfileConfig, arch: Optional[Arch],
                 container_spec: ContainerSpec,
                 local_runtime: Optional[Path] = None):
        """
        :param profile: the configuration profile
        :param container_spec: the parsed container.yaml
        """
        self.profile = profile
        self.arch = arch or Arch()
        self.container_spec = container_spec
        self.flatpak_spec = container_spec.flatpak
        self.local_runtime = local_runtime

    @property
    @abstractmethod
    def nvr(self) -> str:
        """
        The name-verson-release of the container being built itself.

        This is part of BuildContext because it is typically auto-determined
        from the set of available source RPM packages.
        """
        ...

    @property
    @abstractmethod
    def runtime_archive(self) -> Dict:
        """Result of calling koji getArchive() from the archive file for the runtime container."""
        ...

    @property
    @abstractmethod
    def app_package_repo(self) -> Dict:
        """Dictionary with repository information for container package installation.

        Fields of the dictionary include:

          id: int: repository ID
          tag_name: str: name of the tag the repository was created from
          dist: bool: whether repository is a dist rpeo
        """
        ...

    @property
    @abstractmethod
    def app_build_repo(self) -> Dict:
        """Dictionary with repository tag_name and id for app container package build."""
        ...

    @property
    @abstractmethod
    def runtime_package_repo(self) -> Dict:
        """Dictionary with repository tag_name and id for runtime container package installation."""
        ...

    def local_runtime_aux_file(self, suffix: str):
        assert self.local_runtime

        if not self.local_runtime.exists():
            raise ClickException(f"{self.local_runtime}: does not exist")

        if not self.local_runtime.name.endswith(".tar.gz"):
            raise ClickException(f"{self.local_runtime}: does not end with .tar.gz")

        base = self.local_runtime.name[0:-7]
        result = self.local_runtime.with_name(base + suffix)

        if not result.exists():
            raise ClickException(
                f"{self.local_runtime}: auxiliary file {result.name} is missing"
            )

        return result

    @cached_property
    def runtime_packages(self):
        """List of names of packages included in the runtime"""

        if self.local_runtime:
            with open(self.local_runtime_aux_file(".rpmlist.json"), "r") as f:
                rpms = json.load(f)
        else:
            with Status("Listing runtime packages"):
                session = self.profile.koji_session
                rpms = session.listRPMs(imageID=self.runtime_archive["id"])

        return sorted(rpm["name"] for rpm in rpms)

    @cached_property
    def runtime_info(self):
        """RuntimeInfo object with information about the runtime we are building against"""

        if self.local_runtime:
            with open(self.local_runtime_aux_file(".config.json"), "r") as f:
                config_json = json.load(f)
        else:
            config_json = self.runtime_archive["extra"]["docker"]["config"]

        labels = config_json["config"]["Labels"]
        cp = RawConfigParser()
        cp.read_string(labels["org.flatpak.metadata"])

        runtime = cp.get("Runtime", "runtime")
        assert isinstance(runtime, str)
        runtime_id, runtime_arch, runtime_version = runtime.split("/")

        sdk = cp.get("Runtime", "sdk")
        assert isinstance(sdk, str)
        sdk_id, sdk_arch, sdk_version = sdk.split("/")

        return RuntimeInfo(runtime_id=runtime_id, sdk_id=sdk_id, version=runtime_version)

    @property
    def release(self):
        """Bare number for the operating system release (e.g. 39 for Fedora 39)"""
        if self.flatpak_spec.build_runtime:
            runtime_version = self.flatpak_spec.branch
        else:
            runtime_version = self.runtime_info.version

        return self.profile.release_from_runtime_version(runtime_version)

    def get_repos(self, *, for_container: bool, local_repo_path: Optional[Path] = None):
        """Return a list of DNF repository definitions for this context

        :param: for_container: if True, the repositories are for package installation
           when building a container; otherwise they are for build dependencies
           when building RPMs that will eventually be included in a container.
        :param local_repo_path - optional path to a DNF repository with application package builds
        """
        repos: List[str] = []

        def koji_repo(repo: Dict, priority: int, includepkgs: Optional[List[str]] = None):
            result = dedent(f"""\
                [{repo["tag_name"]}]
                name={repo["tag_name"]}
                baseurl={_baseurl(self.profile, repo)}
                priority={priority}
                enabled=1
                skip_if_unavailable=False
            """)
            if includepkgs is not None:
                result += dedent(f"""\
                    includepkgs={",".join(includepkgs)}
                """)
            return result

        if for_container:
            if self.flatpak_spec.build_runtime:
                repos.append(koji_repo(self.runtime_package_repo, 10))
            else:
                repos.append(koji_repo(self.runtime_package_repo, 10,
                                       includepkgs=self.runtime_packages))
                repos.append(koji_repo(self.app_package_repo, 20))
        else:
            if self.flatpak_spec.build_runtime:
                raise NotImplementedError("Runtime package building is not implemented")
            repos.append(koji_repo(self.app_build_repo, 20))

        if local_repo_path:
            repos.append(dedent(f"""\
                [local]
                name=local
                priority=20
                baseurl={local_repo_path}
                enabled=1
                skip_if_unavailable=False
            """))

        return repos


class AutoBuildContext(BuildContext):
    """BuildContext subclass for determining all the information based on the Koji target."""

    def __init__(self, *, profile: ProfileConfig, container_spec: ContainerSpec, target: str,
                 local_runtime: Optional[Path] = None, arch: Optional[Arch] = None):
        """
        :param profile: the configuration profile
        :param container_spec: the parsed container.yaml
        :param target: target used to build containers
        :param local_runtime_build: path to local build of runtime
        """
        super().__init__(
            profile=profile,
            arch=arch,
            container_spec=container_spec,
            local_runtime=local_runtime)
        self.container_target = target

    @cached_property
    def nvr(self):
        if self.flatpak_spec.build_runtime:
            name = self.container_spec.flatpak.component or self.container_spec.flatpak.name
            return f"{name}-{self.container_spec.flatpak.branch}-1"
        else:
            main_package = self.flatpak_spec.packages[0].name
            repo = self.app_package_repo
            locator = PackageLocator()
            locator.add_repo(_baseurl(self.profile, repo))
            version_info = locator.find_latest_version(main_package, arch=self.arch)

            if not version_info:
                raise ClickException(f"Can't find build for {main_package} in {repo['tag_name']}")

            name = self.flatpak_spec.get_component_label(version_info.name)
            version = version_info.version
            release = 1

            return f"{name}-{version}-{release}"

    @cached_property
    def _container_target_info(self):
        return self.profile.koji_session.getBuildTarget(self.container_target)

    @cached_property
    def _container_build_config(self):
        return self.profile.koji_session.getBuildConfig(
            self._container_target_info["build_tag_name"]
        )

    def _get_container_build_config_extra(self, key, default: Any = Option.REQUIRED):
        build_config = self._container_build_config
        value: Optional[str] = build_config['extra'].get(key)
        if value is None:
            if default == Option.REQUIRED:
                raise ClickException(f"{build_config['name']} doesn't have {key} set in extra data")
            else:
                return default

        return value

    @cached_property
    def runtime_archive(self):
        session = self.profile.koji_session
        runtime_tag = self._get_container_build_config_extra('flatpak.runtime_tag')
        tagged_builds = session.listTagged(
            runtime_tag, package=self.flatpak_spec.runtime_name, latest=True, inherit=True,
        )
        if len(tagged_builds) == 0:
            raise ClickException(
                f"Can't find build for {self.flatpak_spec.runtime_name} in {runtime_tag}"
            )

        latest_build = tagged_builds[0]

        archives = session.listArchives(buildID=latest_build["build_id"])
        return next(a for a in archives if a["extra"]["image"]["arch"] == self.arch.rpm)

    @property
    def runtime_package_repo(self):
        return {
            'id': "latest",
            'tag_name': self._get_container_build_config_extra("flatpak.runtime_package_tag"),
            'dist': self._get_container_build_config_extra(
                "flatpak.runtime_package_dist_repo", False
            )
        }

    @property
    def app_package_repo(self):
        return {
            'id': "latest",
            'tag_name': self._get_container_build_config_extra("flatpak.app_package_tag"),
            'dist': self._get_container_build_config_extra(
                "flatpak.app_package_dist_repo", False
            )
        }

    @cached_property
    def _rpm_target_info(self):
        rpm_target = self.profile.get_rpm_koji_target(self.release)
        return self.profile.koji_session.getBuildTarget(rpm_target)

    @property
    def app_build_repo(self):
        return {
            'id': "latest",
            'tag_name': self._rpm_target_info["build_tag_name"],
            'dist': False,
        }


class ManualBuildContext(BuildContext):
    """BuildContext subclass when the context information has been precomputed

    The typical use of this is that Koji determines a set of application repositories,
    a runtime to build against, and so forth, then fires off builds across multiple
    architectures with the exact same context.
    """
    def __init__(self, *, profile: ProfileConfig, container_spec: ContainerSpec,
                 nvr: str,
                 runtime_nvr: Optional[str],
                 runtime_repo: int,
                 app_repo: Optional[int],
                 arch: Optional[Arch] = None):
        """
        :param profile: the configuration profile
        :param container_spec: the parsed container.yaml
        :param nvr: name-version-release for the container to be created
        :param runtime_nvr: name-version-release for the runtime container to build against
            (only for application container builds)
        :param runtime_repo: repository ID of repository for runtime packages
        :param app_repo: repository ID for repository for application packages
            (only for application container builds)
        """
        super().__init__(profile=profile, arch=arch, container_spec=container_spec)
        self._nvr = nvr
        self.runtime_nvr = runtime_nvr
        self.runtime_repo = runtime_repo
        self.app_repo = app_repo

    @property
    def nvr(self):
        return self._nvr

    @cached_property
    def runtime_archive(self):
        assert self.runtime_nvr
        build = self.profile.koji_session.getBuild(self.runtime_nvr)
        session = self.profile.koji_session

        archives = session.listArchives(buildID=build["build_id"])
        return next(a for a in archives if a["extra"]["image"]["arch"] == self.arch.rpm)

    @cached_property
    def runtime_package_repo(self):
        return self.profile.koji_session.repoInfo(self.runtime_repo)

    @cached_property
    def app_package_repo(self):
        assert self.app_repo
        return self.profile.koji_session.repoInfo(self.app_repo)

    @property
    def app_build_repo(self):
        raise NotImplementedError("ManualBuildSource can only be used for container builds")
