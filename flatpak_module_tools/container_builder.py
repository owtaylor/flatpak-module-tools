from abc import ABC, abstractmethod
from configparser import RawConfigParser
from dataclasses import dataclass
import json
from pathlib import Path
import os
import shlex
import shutil
import subprocess
from textwrap import dedent
from typing import Any, List, Optional, Sequence, Union

import koji

from flatpak_module_tools.mock import make_mock_cfg
from flatpak_module_tools.rpm_utils import create_rpm_manifest

from .container_spec import ContainerSpec
from .flatpak_builder import (
    FlatpakBuilder,
    PackageFlatpakSourceInfo, FLATPAK_METADATA_ANNOTATIONS
)
from .utils import (
    check_call, die, get_arch, log_call, header, important, info, RuntimeInfo
)


class BuildExecutor(ABC):
    def __init__(self, *, installroot: Path, workdir: Path, releasever: str, runtimever: str):
        self.installroot = installroot
        self.workdir = workdir
        self.releasever = releasever
        self.runtimever = runtimever

    @abstractmethod
    def init(self, repos: List[str]) -> None:
        ...

    @abstractmethod
    def install(self, packages: List[str]) -> None:
        ...

    @abstractmethod
    def write_file(self, path: Path, contents: str) -> None:
        pass

    @abstractmethod
    def check_call(self, cmd: Sequence[Union[str, Path]], *, cwd: Optional[Path] = None) -> None:
        ...

    @abstractmethod
    def popen(self, cmd: Sequence[Union[str, Path]], *,
              stdout=None, cwd: Optional[Path] = None) -> subprocess.Popen:
        ...


class MockExecutor(BuildExecutor):
    def init(self, repos: Sequence[str]):
        self.mock_cfg_path = self.workdir / "mock.cfg"
        to_install = [
            '/bin/bash',
            '/bin/mount',
            'coreutils',  # for mkdir
            'glibc-minimal-langpack',
            'shadow-utils',
            'tar',
        ]
        mock_cfg = make_mock_cfg(
            arch=get_arch(),
            chroot_setup_cmd=f"install {' '.join(to_install)}",
            releasever=self.releasever,
            repos=repos,
            root_cache_enable=False,
            runtimever=self.runtimever
        )
        with open(self.mock_cfg_path, "w") as f:
            f.write(mock_cfg)

        check_call(['mock', '-q', '-r', self.mock_cfg_path, '--clean'])

    def install(self, packages):
        root_dir = subprocess.check_output([
            'mock', '-r', self.mock_cfg_path,
            '--print-root-path'
        ], encoding="UTF-8").strip()
        check_call([
            'mock', '-r', self.mock_cfg_path,
            '--dnf-cmd', '--',
            '--installroot', root_dir / self.installroot.relative_to("/"),
            'install', '-y'
        ] + packages)

    def write_file(self, path, contents):
        temp_location = self.workdir / path.name

        with open(temp_location, "w") as f:
            f.write(contents)

        check_call([
            'mock', '-q', '-r', self.mock_cfg_path, '--copyin', temp_location, path
        ])

    def check_call(self, cmd, *, cwd=None):
        assert len(cmd) > 1  # avoid accidental shell interpretation

        args = ['mock', '-q', '-r', self.mock_cfg_path, '--chroot']
        if cwd:
            args += ['--cwd', cwd]
        args.append('--')
        args += cmd

        check_call(args)

    def popen(self, cmd, *, stdout=None, cwd=None):
        # mock --chroot logs the result, which we don't want here,
        # so we use --shell instead.

        args = ['mock', '-q', '-r', self.mock_cfg_path, '--shell']
        if cwd:
            args += ['--cwd', cwd]
        args.append(" ".join(shlex.quote(str(c)) for c in cmd))

        log_call(args)
        return subprocess.Popen(args, stdout=stdout)


class InnerExcutor(BuildExecutor):
    def init(self, repos):
        pass

    def install(self, packages):
        command = [
            "dnf",
            f"--installroot={self.installroot}",
            "install", "-y",
        ] + packages

        check_call(command)

    def write_file(self, path, contents):
        with open(path, "w") as f:
            f.write(contents)

    def check_call(self, cmd, *, cwd=None):
        check_call(cmd)

    def popen(self, cmd, *, stdout=None, cwd=None):
        return subprocess.Popen(cmd, stdout=stdout, cwd=cwd)


@dataclass
class AssemblyOptions:
    nvr: str
    runtime_nvr: str | None
    runtime_repo: int
    app_repo: int | None


class ContainerBuilder:
    def __init__(self, profile, container_spec: ContainerSpec, local_repo=None,
                 flatpak_metadata=FLATPAK_METADATA_ANNOTATIONS):
        self.profile = profile
        self.local_repo = None
        self.flatpak_metadata = flatpak_metadata

        self.container_spec = container_spec

    def _add_labels_to_builder(self, builder, name, version, release):
        name_label = self.container_spec.flatpak.name or name
        component_label = self.container_spec.flatpak.component or name

        builder.add_labels({'name': name_label,
                            'com.redhat.component': component_label,
                            'version': version,
                            'release': release})

    def _get_build_config_extra(self, build_config, key):
        result = build_config['extra'].get(key)
        if result is None:
            die(
                f"Build tag '{build_config['name']}' doesn't have {key} set"
            )

        return result

    def _get_repo_id(self, tag):
        repo_info = self.profile.koji_session.getRepo(tag)
        return repo_info['id']

    def _runtime_assembly_options_from_build_tag(self, build_tag: str):
        session = self.profile.koji_session
        build_config = session.getBuildConfig(build_tag)

        runtime_package_tag = self._get_build_config_extra(
            build_config, 'flatpak.runtime_package_tag'
        )
        runtime_repo = session.getRepo(runtime_package_tag)

        name = self.container_spec.flatpak.component or self.container_spec.flatpak.name
        version = self.container_spec.flatpak.branch
        release = 1

        return AssemblyOptions(
            nvr=f"{name}-{version}-{release}",
            runtime_nvr=None,
            runtime_repo=runtime_repo['id'],
            app_repo=None,
        )

    def _app_assembly_options_from_build_tag(self, build_tag: str):
        session = self.profile.koji_session
        build_config = session.getBuildConfig(build_tag)

        runtime_tag = self._get_build_config_extra(build_config, 'flatpak.runtime_tag')
        runtime_package_tag = self._get_build_config_extra(
            build_config, 'flatpak.runtime_package_tag'
        )
        app_package_tag = self._get_build_config_extra(build_config, 'flatpak.app_package_tag')

        flatpak_spec = self.container_spec.flatpak

        # Find main package

        main_package = flatpak_spec.packages[0]
        latest_tagged = session.listTagged(
            build_tag, package=main_package, latest=True, inherit=True
        )

        if not latest_tagged:
            die(f"Can't find build for {main_package} in {build_tag}")

        name = (flatpak_spec.component or
                flatpak_spec.name or
                latest_tagged[0]["name"])
        version = latest_tagged[0]["version"]
        release = 1

        # Find runtime

        tagged_builds = session.listTagged(
            runtime_tag, package=flatpak_spec.runtime_name, latest=True, inherit=True,
        )
        if len(tagged_builds) == 0:
            die(
                f"Can't find build for {flatpak_spec.runtime_name} in {runtime_tag}"
            )

        runtime_nvr = tagged_builds[0]["nvr"]
        runtime_version = tagged_builds[0]["version"]

        if flatpak_spec.runtime_version and flatpak_spec.runtime_version != runtime_version:
            die(
                f"Runtime '{runtime_nvr}' doesn't match "
                f"'runtime_version: {flatpak_spec.runtime_version}' in container.yaml"
            )

        return AssemblyOptions(
            nvr=f"{name}-{version}-{release}",
            runtime_nvr=runtime_nvr,
            runtime_repo=self._get_repo_id(runtime_package_tag),
            app_repo=self._get_repo_id(app_package_tag)
        )

    def assembly_options_from_target(self, target: str):
        session = self.profile.koji_session
        target_info = session.getBuildTarget(target)
        build_tag = target_info['build_tag_name']

        if self.container_spec.flatpak.build_runtime:
            return self._runtime_assembly_options_from_build_tag(build_tag)
        else:
            return self._app_assembly_options_from_build_tag(build_tag)

    def _get_runtime_archive(self, runtime_nvr: str):
        session = self.profile.koji_session

        build = session.getBuild(runtime_nvr)
        archives = session.listArchives(buildID=build["build_id"])
        return next(a for a in archives if a["extra"]["image"]["arch"] == get_arch().rpm)

    def _get_runtime_info(self, runtime_archive: dict[str, Any]):
        labels = runtime_archive["extra"]["docker"]["config"]["config"]["Labels"]
        cp = RawConfigParser()
        cp.read_string(labels["org.flatpak.metadata"])

        runtime = cp.get("Runtime", "runtime")
        assert isinstance(runtime, str)
        runtime_id, runtime_arch, runtime_version = runtime.split("/")

        sdk = cp.get("Runtime", "sdk")
        assert isinstance(sdk, str)
        sdk_id, sdk_arch, sdk_version = sdk.split("/")

        return RuntimeInfo(runtime_id=runtime_id, sdk_id=sdk_id, version=runtime_version)

    def _get_repos(
            self,
            options: AssemblyOptions,
            runtime_archive: dict[str, Any] | None,
            installroot: Path
    ):
        session = self.profile.koji_session
        topurl = self.profile.koji_options['topurl']
        pathinfo = koji.PathInfo(topdir=topurl)

        runtime_repo_info = session.repoInfo(options.runtime_repo)

        def baseurl(repo_info):
            arch = get_arch().rpm
            return f"{pathinfo.repo(repo_info['id'], repo_info['tag_name'])}/{arch}"

        if self.container_spec.flatpak.build_runtime:
            return [
                dedent(f"""\
                    [{runtime_repo_info['tag_name']}]
                    name={runtime_repo_info['tag_name']}
                    baseurl={baseurl(runtime_repo_info)}
                    priority=10
                    """)
            ]
        else:
            assert options.app_repo
            app_rpm_repo_info = session.repoInfo(options.app_repo)

            assert runtime_archive
            rpms = session.listRPMs(imageID=runtime_archive["id"])
            runtime_packages = sorted(rpm["name"] for rpm in rpms)

            return [
                dedent(f"""\
                    [{runtime_repo_info['tag_name']}]
                    name={runtime_repo_info['tag_name']}
                    baseurl={baseurl(runtime_repo_info)}
                    priority=10
                    includepkgs={",".join(runtime_packages)}
                    """),
                dedent(f"""\
                    [{app_rpm_repo_info['tag_name']}]
                    name={app_rpm_repo_info['tag_name']}
                    baseurl={baseurl(app_rpm_repo_info)}
                    priority=20
                    """)
            ]

    def _write_dnf_conf(self, repos):
        dnfdir = self.executor.installroot / "etc/dnf"
        self.executor.check_call([
            "mkdir", "-p", dnfdir
        ])

        dnf_conf = dedent("""\
            [main]
            cachedir=/var/cache/dnf
            debuglevel=1
            logfile=/var/log/dnf.log
            reposdir=/dev/null
            retries=20
            obsoletes=1
            gpgcheck=0
            assumeyes=1
            keepcache=1
            install_weak_deps=0
            strict=1

            # repos
        """)

        dnf_conf += "\n".join(repos)
        self.executor.write_file(dnfdir / "dnf.conf", dnf_conf)

    def _cleanup_tree(self, builder: FlatpakBuilder, installroot: Path):
        script = builder.get_cleanup_script()
        if not script:
            return

        self.executor.write_file(installroot / "tmp/cleanup.sh", script)
        self.executor.check_call(["chroot", ".", "/bin/sh", "/tmp/cleanup.sh"], cwd=installroot)

    def _copy_manifest_and_config(self, oci_dir: str, outname_base: Path):
        index_json = os.path.join(oci_dir, "index.json")
        with open(index_json) as f:
            index_json_contents = json.load(f)
            manifest_digest = index_json_contents["manifests"][0]["digest"]

        assert manifest_digest.startswith("sha256:")
        manifest_path = os.path.join(oci_dir, "blobs", "sha256", manifest_digest[7:])
        with open(manifest_path) as f:
            manifest_json_contents = json.load(f)
            config_digest = manifest_json_contents["config"]["digest"]

        assert config_digest.startswith("sha256:")
        config_path = os.path.join(oci_dir, "blobs", "sha256", config_digest[7:])

        shutil.copy(manifest_path, f"{outname_base}.manifest.json")
        info(f"    wrote {outname_base}.manifest.json")
        shutil.copy(config_path, f"{outname_base}.config.json")
        info(f"    wrote {outname_base}.config.json")

    def _create_rpm_manifest(self, installroot, outname_base: Path):
        if self.container_spec.flatpak.build_runtime:
            restrict_to = None
        else:
            restrict_to = installroot / "app"

        manifest = create_rpm_manifest(installroot, restrict_to)

        with open(f"{outname_base}.rpmlist.json", "w") as f:
            json.dump(manifest, f, indent=4)

        info(f"    wrote {outname_base}.rpmlist.json")

    def _run_build(self, executor: BuildExecutor, options: AssemblyOptions, *,
                   installroot: Path, workdir: Path, resultdir: Path):

        self.executor = executor

        if self.container_spec.flatpak.build_runtime:
            runtime_info = None
            runtime_archive = None
        else:
            assert options.runtime_nvr
            runtime_archive = self._get_runtime_archive(options.runtime_nvr)
            runtime_info = self._get_runtime_info(runtime_archive)

        source = PackageFlatpakSourceInfo(self.container_spec.flatpak, runtime_info)

        builder = FlatpakBuilder(source, workdir, ".", flatpak_metadata=self.flatpak_metadata)

        name, version, release = options.nvr.rsplit('-', 2)
        self._add_labels_to_builder(builder, name, version, release)

        repos = self._get_repos(options, runtime_archive, installroot)

        info('Initializing installation path')
        self.executor.init(repos)

        info('Writing dnf.conf')
        self._write_dnf_conf(repos)

        info('Installing packages')
        self.executor.install(self.container_spec.flatpak.packages)

        info('Cleaning tree')
        self._cleanup_tree(builder, installroot)

        info('Exporting tree')
        tar_args = [
            'tar', 'cf', '-',
            '--anchored',
            '--exclude=./sys/*',
            '--exclude=./proc/*',
            '--exclude=./dev/*',
            '--exclude=./run/*',
            "."
        ]

        process = self.executor.popen(tar_args, cwd=installroot, stdout=subprocess.PIPE)
        assert process.stdout is not None

        # When mock is using systemd-nspawn, systemd-nspawn dies with EPIPE if the output
        # stream is closed before it exits, even if the child of systemd-nspawn isn't
        # writing anything.
        # https://github.com/systemd/systemd/issues/11533
        filesystem_tar, manifestfile = builder._export_from_stream(
            process.stdout, close_stream=False
        )
        process.wait()
        process.stdout.close()
        if process.returncode != 0:
            die(f"tar failed (exit status={process.returncode})")

        ref_name, oci_dir, oci_tar = builder.build_container(filesystem_tar)

        outname_base = resultdir / f"{options.nvr}.{get_arch().rpm}.oci"
        local_outname = f"{outname_base}.tar.gz"

        info('Compressing result')
        with open(local_outname, 'wb') as f:
            subprocess.check_call(['gzip', '-c', oci_tar], stdout=f)

        important('Created ' + local_outname)

        info('Creating RPM manifest')
        self._create_rpm_manifest(installroot, outname_base)

        info('Extracting container manifest and config')
        self._copy_manifest_and_config(oci_dir, outname_base)

        return local_outname

    def assemble(self, options: AssemblyOptions, *,
                 installroot: Path, workdir: Path, resultdir: Path):

        runtimever = options.nvr.rsplit('-', 2)[1]
        executor = InnerExcutor(
            installroot=installroot,
            workdir=workdir,
            releasever=self.profile.release_from_runtime_version(runtimever),
            runtimever=runtimever
        )

        self._run_build(
            executor, options, installroot=installroot, workdir=workdir, resultdir=resultdir
        )

    def build(self, target: str):
        header('BUILDING CONTAINER')
        important(f'container spec: {self.container_spec.path}')
        important('')

        options = self.assembly_options_from_target(target)

        arch = get_arch()
        workdir = Path(arch.rpm) / "work/oci"
        if os.path.exists(workdir):
            info(f"Removing old working directory {workdir}")
            shutil.rmtree(workdir)

        workdir.mkdir(parents=True, exist_ok=True)

        resultdir = Path(arch.rpm) / "result"
        resultdir.mkdir(parents=True, exist_ok=True)

        info(f"Writing results to {resultdir}")

        installroot = Path("/contents")

        runtimever = options.nvr.rsplit('-', 2)[1]
        executor = MockExecutor(
            installroot=installroot,
            workdir=workdir,
            releasever=self.profile.release_from_runtime_version(runtimever),
            runtimever=runtimever
        )

        return self._run_build(
            executor, options, installroot=installroot, workdir=workdir, resultdir=resultdir
        )
