from abc import ABC, abstractmethod
import json
from pathlib import Path
import os
import shlex
import shutil
import subprocess
from textwrap import dedent
from typing import List, Optional, Sequence, Union

from .build_context import BuildContext
from .flatpak_builder import (
    FlatpakBuilder,
    PackageFlatpakSourceInfo, FLATPAK_METADATA_ANNOTATIONS
)
from .mock import make_mock_cfg
from .rpm_utils import create_rpm_manifest
from .utils import (
    check_call, die, get_arch, log_call, header, important, info
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
        check_call(cmd, cwd=cwd)

    def popen(self, cmd, *, stdout=None, cwd=None):
        return subprocess.Popen(cmd, stdout=stdout, cwd=cwd)


class ContainerBuilder:
    def __init__(self, context: BuildContext,
                 flatpak_metadata=FLATPAK_METADATA_ANNOTATIONS):
        self.context = context
        self.flatpak_metadata = flatpak_metadata

    def _add_labels_to_builder(self, builder, name, version, release):
        name_label = self.context.flatpak_spec.name or name
        component_label = self.context.flatpak_spec.component or name

        builder.add_labels({'name': name_label,
                            'com.redhat.component': component_label,
                            'version': version,
                            'release': release})

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
        if self.context.flatpak_spec.build_runtime:
            restrict_to = None
        else:
            restrict_to = installroot / "app"

        manifest = create_rpm_manifest(installroot, restrict_to)

        with open(f"{outname_base}.rpmlist.json", "w") as f:
            json.dump(manifest, f, indent=4)

        info(f"    wrote {outname_base}.rpmlist.json")

    def _run_build(self, executor: BuildExecutor, *,
                   installroot: Path, workdir: Path, resultdir: Path):

        self.executor = executor

        if self.context.flatpak_spec.build_runtime:
            runtime_info = None
        else:
            runtime_info = self.context.runtime_info

        source = PackageFlatpakSourceInfo(self.context.flatpak_spec, runtime_info)

        builder = FlatpakBuilder(source, workdir, ".", flatpak_metadata=self.flatpak_metadata)

        name, version, release = self.context.nvr.rsplit('-', 2)
        self._add_labels_to_builder(builder, name, version, release)

        repos = self.context.get_repos(for_container=True)

        info('Initializing installation path')
        self.executor.init(repos)

        info('Writing dnf.conf')
        self._write_dnf_conf(repos)

        info('Installing packages')
        self.executor.install(self.context.flatpak_spec.packages)

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

        outname_base = resultdir / f"{self.context.nvr}.{get_arch().rpm}.oci"
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

    def assemble(self, *,
                 installroot: Path, workdir: Path, resultdir: Path):

        if self.context.flatpak_spec.build_runtime:
            runtimever = self.context.nvr.rsplit('-', 2)[1]
        else:
            runtimever = self.context.runtime_info.version

        executor = InnerExcutor(
            installroot=installroot,
            workdir=workdir,
            releasever=self.context.release,
            runtimever=runtimever
        )

        self._run_build(
            executor, installroot=installroot, workdir=workdir, resultdir=resultdir
        )

    def build(self):
        header('BUILDING CONTAINER')
        important(f'container spec: {self.context.container_spec.path}')
        important('')

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

        if self.context.flatpak_spec.build_runtime:
            runtimever = self.context.nvr.rsplit('-', 2)[1]
        else:
            runtimever = self.context.runtime_info.version

        executor = MockExecutor(
            installroot=installroot,
            workdir=workdir,
            releasever=self.context.release,
            runtimever=runtimever
        )

        return self._run_build(
            executor, installroot=installroot, workdir=workdir, resultdir=resultdir
        )
