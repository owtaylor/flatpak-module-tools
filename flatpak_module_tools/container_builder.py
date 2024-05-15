import json
from pathlib import Path
import os
import shutil
import subprocess
from textwrap import dedent

from flatpak_module_tools.chroot import Chroot

from .build_context import BuildContext
from .build_executor import InnerExcutor, MockExecutor
from .flatpak_builder import (
    FlatpakBuilder,
    PackageFlatpakSourceInfo, FLATPAK_METADATA_ANNOTATIONS
)
from .rpm_utils import create_rpm_manifest
from .utils import (
    die, header, important, info
)


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

        # If {installroot}/dev/null exists, we assume that /proc, /dev, etc
        # are already set up as well they can be can we can't do anything more,
        # otherwise we create bind mounts to the corresponding directories
        # in the outer environment.
        #
        # The /var/cache/dnf bind mount allows mounting a persistent
        # cache directory into the outer environment and having it be
        # used across builds.

        need_bind_mounts = not (installroot / "dev/null").exists()
        install_sh = ""

        if need_bind_mounts:
            install_sh += dedent(f"""\
                for i in /proc /sys /dev /var/cache/dnf ; do
                    mkdir -p {installroot}/$i
                    mount --rbind $i {installroot}/$i
                done
                """)

        install_sh += dedent(f"""\
            dnf --installroot={installroot} install -y {package_str}
            """)

        (installroot / "tmp").mkdir(mode=0o1777, parents=True)

        cleanup_script = self.builder.get_cleanup_script()
        if cleanup_script and cleanup_script.strip() != "":
            self.executor.write_file(installroot / "tmp/cleanup.sh", cleanup_script)
            install_sh += dedent(f"""\
            cd {installroot}
            chroot . /bin/sh -ex /tmp/cleanup.sh
            """)

        self.executor.write_file(Path("/tmp/install.sh"), install_sh)

        if self.context.local_repo:
            inner_local_repo_path = self._inner_local_repo_path
            assert inner_local_repo_path
            mounts = {
                inner_local_repo_path: self.context.local_repo
            }
        else:
            mounts = None

        install_command = []
        if need_bind_mounts:
            install_command += ["unshare", "-m", "--map-users=all", "--map-groups=all", "--"]

        install_command += ["/bin/bash", "-ex", "/tmp/install.sh"]
        self.executor.check_call(install_command,  mounts=mounts, enable_network=True)

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

    def _export_container(self, *, resultdir: Path,
                          result_filename: str | None = None,
                          write_aux_files: bool = False):
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

        if result_filename:
            outname_base = resultdir / result_filename
            result_path = outname_base
        else:
            outname_base = resultdir / f"{self.context.nvr}.{self.context.arch.rpm}.oci"
            result_path = f"{outname_base}.tar"

        info('Tarring result')
        with open(result_path, 'wb') as f:
            files = os.listdir(oci_dir)
            subprocess.check_call(['tar', '-cnf', '-', *files], stdout=f, cwd=oci_dir)

        important(f"Created {result_path}")

        if write_aux_files:
            info('Creating RPM manifest')
            self._create_rpm_manifest(outname_base)

            info('Extracting container manifest and config')
            self._copy_manifest_and_config(oci_dir, outname_base)

        return str(result_path)

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

    def install_contents(self, *,
                         installroot: Path, workdir: Path,
                         write_dnf_conf: bool = True,
                         install_runtime_config: bool = True):
        self.executor = InnerExcutor(
            context=self.context,
            installroot=installroot,
            workdir=workdir,
            releasever=self.context.release,
            runtimever=self._runtimever
        )
        self.executor.init()
        self.builder = self._create_builder(workdir=workdir,
                                            install_runtime_config=install_runtime_config)

        self._install_contents(write_dnf_conf=write_dnf_conf)

    def export_container(self, *,
                         installroot: Path, workdir: Path, resultdir: Path,
                         result_filename: str | None = None,
                         write_aux_files: bool = True):
        self.executor = InnerExcutor(
            context=self.context,
            installroot=installroot,
            workdir=workdir,
            releasever=self.context.release,
            runtimever=self._runtimever
        )
        self.executor.init()
        self.builder = self._create_builder(workdir=workdir)

        self._export_container(resultdir=resultdir, result_filename=result_filename,
                               write_aux_files=write_aux_files)

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
