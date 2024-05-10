from abc import ABC, abstractmethod
from functools import cached_property
import json
from pathlib import Path
import os
import shlex
import shutil
import subprocess
from textwrap import dedent
from typing import Dict, Optional, Sequence, Union

from .build_context import BuildContext
from .flatpak_builder import (
    FlatpakBuilder,
    PackageFlatpakSourceInfo, FLATPAK_METADATA_ANNOTATIONS
)
from .mock import make_mock_cfg
from .rpm_utils import create_rpm_manifest
from .utils import (
    atomic_writer, check_call, die, log_call, header, important, info
)


class BuildExecutor(ABC):
    def __init__(self, *, context: BuildContext,
                 installroot: Path, workdir: Path, releasever: str, runtimever: str):
        self.context = context
        self.installroot = installroot
        self.workdir = workdir
        self.releasever = releasever
        self.runtimever = runtimever

    @abstractmethod
    def init(self) -> None:
        ...

    @abstractmethod
    def write_file(self, path: Path, contents: str) -> None:
        pass

    @abstractmethod
    def check_call(self, cmd: Sequence[Union[str, Path]], *,
                   cwd: Optional[Path] = None,
                   mounts: Optional[Dict[Path, Path]] = None,
                   enable_network: bool = False) -> None:
        ...

    @abstractmethod
    def popen(self, cmd: Sequence[Union[str, Path]], *,
              stdout=None, cwd: Optional[Path] = None) -> subprocess.Popen:
        ...

    @property
    @abstractmethod
    def absolute_installroot(self) -> Path:
        ...


class MockExecutor(BuildExecutor):
    @property
    def _bootstrap_koji_repo(self):
        # We need a repository to install the basic buildroot tools
        # (dnf, mount, tar, etc) from. The runtime package repo works.

        return self.context.runtime_package_repo.dnf_config()

    def init(self):
        self.mock_cfg_path = self.workdir / "mock.cfg"
        to_install = [
            '/bin/bash',
            '/bin/mount',
            'coreutils',  # for mkdir
            'dnf',
            'glibc-minimal-langpack',
            'shadow-utils',
            'tar',
        ]
        mock_cfg = make_mock_cfg(
            arch=self.context.arch,
            chroot_setup_cmd=f"install {' '.join(to_install)}",
            releasever=self.releasever,
            repos=[self._bootstrap_koji_repo],
            root_cache_enable=True,
            runtimever=self.runtimever
        )
        with atomic_writer(self.mock_cfg_path) as f:
            f.write(mock_cfg)

        check_call(['mock', '-q', '-r', self.mock_cfg_path, '--clean'])

    def write_file(self, path, contents):
        temp_location = self.workdir / path.name

        with open(temp_location, "w") as f:
            f.write(contents)

        check_call([
            'mock', '-q', '-r', self.mock_cfg_path, '--copyin', temp_location, path
        ])

    def check_call(self, cmd, *,
                   cwd=None,
                   mounts: Optional[Dict[Path, Path]] = None,
                   enable_network: bool = False):
        # mock --chroot logs the result, which we don't want here,
        # so we use --shell instead.

        args = ['mock', '-q', '-r', self.mock_cfg_path, '--shell']
        if cwd:
            args += ['--cwd', cwd]

        if enable_network:
            args.append("--enable-network")
        if mounts:
            for inner_path, outer_path in mounts.items():
                args += (
                    "--plugin-option",
                    f"bind_mount:dirs=[('{outer_path}', '{inner_path}')]"
                )

        args.append(" ".join(shlex.quote(str(c)) for c in cmd))
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

    @cached_property
    def absolute_installroot(self):
        args = ['mock', '-q', '-r', self.mock_cfg_path, '--print-root-path']
        root_path = subprocess.check_output(args, universal_newlines=True).strip()
        return Path(root_path) / self.installroot.relative_to("/")


class InnerExcutor(BuildExecutor):
    def init(self):
        pass

    def write_file(self, path, contents):
        with open(path, "w") as f:
            f.write(contents)

    def check_call(self, cmd, *, cwd=None, mounts=None, enable_network=False):
        assert not mounts  # Not supported for InnerExecutor
        check_call(cmd, cwd=cwd)

    def popen(self, cmd, *, stdout=None, cwd=None):
        return subprocess.Popen(cmd, stdout=stdout, cwd=cwd)

    @property
    def absolute_installroot(self):
        return self.installroot


class ContainerBuilder:
    def __init__(self, context: BuildContext,
                 flatpak_metadata=FLATPAK_METADATA_ANNOTATIONS):
        self.context = context
        self.flatpak_metadata = flatpak_metadata

    def _add_labels_to_builder(self, name, version, release):
        component_label = name
        name_label = self.context.container_spec.flatpak.get_name_label(component_label)
        self.builder.add_labels({'name': name_label,
                                 'com.redhat.component': component_label,
                                 'version': version,
                                 'release': release})

    @property
    def _inner_local_repo_path(self):
        if self.context.local_repo:
            return Path("/mnt/localrepo")
        else:
            return None

    @property
    def _runtimever(self):
        if self.context.flatpak_spec.build_runtime:
            return self.context.nvr.rsplit('-', 2)[1]
        else:
            return self.context.runtime_info.version

    def _clean_workdir(self, workdir: Path):
        for child in workdir.iterdir():
            if child.name == "mock.cfg":
                # Save this so the timestamp is preserved, and the root cache works
                pass
            elif child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    def _write_dnf_conf(self):
        dnfdir = self.executor.installroot / "etc/dnf"
        self.executor.check_call([
            "mkdir", "-p", dnfdir
        ])

        dnf_conf = dedent("""\
            [main]
            cachedir=/var/cache/dnf
            debuglevel=2
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

        dnf_conf += "\n".join(
            self.context.get_repos(for_container=True, local_repo_path=self._inner_local_repo_path)
        )
        self.executor.write_file(dnfdir / "dnf.conf", dnf_conf)

    def _install_packages(self):
        installroot = self.executor.installroot
        packages = self.builder.get_install_packages()
        package_str = " ".join(shlex.quote(p) for p in packages)
        install_sh = dedent(f"""\
            for i in /proc /sys /dev /var/cache/dnf ; do
                mkdir -p {installroot}/$i
                mount --rbind $i {installroot}/$i
            done
            dnf --installroot={installroot} install -y {package_str}
            """)

        (installroot / "tmp").mkdir(mode=0o1777, parents=True)

        self.executor.write_file(Path("/tmp/install.sh"), install_sh)

        if self.context.local_repo:
            inner_local_repo_path = self._inner_local_repo_path
            assert inner_local_repo_path
            mounts = {
                inner_local_repo_path: self.context.local_repo
            }
        else:
            mounts = None

        self.executor.check_call(["/bin/bash", "-ex", "/tmp/install.sh"],
                                 mounts=mounts, enable_network=True)

        cleanup_script = self.builder.get_cleanup_script()
        if cleanup_script and cleanup_script.strip() != "":
            installroot = self.executor.installroot
            self.executor.write_file(installroot / "tmp/cleanup.sh", cleanup_script)
            self.executor.check_call(
                ["chroot", ".", "/bin/sh", "-ex", "/tmp/cleanup.sh"],
                cwd=installroot
            )

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

    def _create_rpm_manifest(self, outname_base: Path):
        if self.context.flatpak_spec.build_runtime:
            restrict_to = None
        else:
            restrict_to = self.executor.absolute_installroot / "app"

        manifest = create_rpm_manifest(self.executor.absolute_installroot, restrict_to)

        with open(f"{outname_base}.rpmlist.json", "w") as f:
            json.dump(manifest, f, indent=4)

        info(f"    wrote {outname_base}.rpmlist.json")

    def _create_builder(self, *, workdir: Path, install_runtime_config: bool = True):
        if self.context.flatpak_spec.build_runtime:
            runtime_info = None
        else:
            runtime_info = self.context.runtime_info

        source = PackageFlatpakSourceInfo(self.context.flatpak_spec, runtime_info)

        return FlatpakBuilder(source, workdir, ".", flatpak_metadata=self.flatpak_metadata,
                              install_runtime_config=install_runtime_config)

    def _install_contents(self, write_dnf_conf: bool = True):
        if write_dnf_conf:
            info('Writing dnf.conf')
            self._write_dnf_conf()

        info('Installing packages and cleaning tree')
        self._install_packages()

    def _export_container(self, *, resultdir: Path):
        name, version, release = self.context.nvr.rsplit('-', 2)
        self._add_labels_to_builder(name, version, release)

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

        process = self.executor.popen(
            tar_args, cwd=self.executor.installroot, stdout=subprocess.PIPE
        )
        assert process.stdout is not None

        # When mock is using systemd-nspawn, systemd-nspawn dies with EPIPE if the output
        # stream is closed before it exits, even if the child of systemd-nspawn isn't
        # writing anything.
        # https://github.com/systemd/systemd/issues/11533
        filesystem_tar, manifestfile = self.builder._export_from_stream(
            process.stdout, close_stream=False
        )
        process.wait()
        process.stdout.close()
        if process.returncode != 0:
            die(f"tar failed (exit status={process.returncode})")

        ref_name, oci_dir = self.builder.build_container(filesystem_tar, tar_outfile=False)

        outname_base = resultdir / f"{self.context.nvr}.{self.context.arch.rpm}.oci"
        local_outname = f"{outname_base}.tar"

        info('Tarring result')
        with open(local_outname, 'wb') as f:
            files = os.listdir(oci_dir)
            subprocess.check_call(['tar', '-cnf', '-', *files], stdout=f, cwd=oci_dir)

        important('Created ' + local_outname)

        info('Creating RPM manifest')
        self._create_rpm_manifest(outname_base)

        info('Extracting container manifest and config')
        self._copy_manifest_and_config(oci_dir, outname_base)

        return local_outname

    def assemble(self, *,
                 installroot: Path, workdir: Path, resultdir: Path):

        self.executor = InnerExcutor(
            context=self.context,
            installroot=installroot,
            workdir=workdir,
            releasever=self.context.release,
            runtimever=self._runtimever
        )

        info('Initializing installation path')
        self.executor.init()

        self.builder = self._create_builder(workdir=workdir)

        self._install_contents()
        self._export_container(resultdir=resultdir)

    def build(self, workdir: Path, resultdir: Path):
        header('BUILDING CONTAINER')
        important(f'container spec: {self.context.container_spec.path}')
        important('')

        if os.path.exists(workdir):
            info(f"Cleaning old working directory {workdir}")
            self._clean_workdir(workdir)

        workdir.mkdir(parents=True, exist_ok=True)
        resultdir.mkdir(parents=True, exist_ok=True)

        info(f"Writing results to {resultdir}")

        installroot = Path("/contents")

        self.executor = MockExecutor(
            context=self.context,
            installroot=installroot,
            workdir=workdir,
            releasever=self.context.release,
            runtimever=self._runtimever
        )
        self.executor.init()
        self.builder = self._create_builder(workdir=workdir)

        self._install_contents()
        return self._export_container(resultdir=resultdir)
