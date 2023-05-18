from pathlib import Path
import os
import shutil
import subprocess
from textwrap import dedent

from flatpak_module_tools.mock import make_mock_cfg

from .container_spec import ContainerSpec
from .flatpak_builder import (
    FlatpakBuilder,
    PackageFlatpakSourceInfo, FLATPAK_METADATA_ANNOTATIONS
)
from .rpm_builder import RpmBuilder
from .utils import (
    check_call, die, get_arch, log_call, header, important, info
)


class ContainerBuilder:
    def __init__(self, profile, container_spec: ContainerSpec, from_local=False, local_builds=[],
                 flatpak_metadata=FLATPAK_METADATA_ANNOTATIONS):
        self.profile = profile
        self.from_local = from_local
        self.local_builds = local_builds
        self.flatpak_metadata = flatpak_metadata

        self.container_spec = container_spec

    def build(self):
        header('BUILDING CONTAINER')
        important(f'container spec: {self.container_spec.path}')
        important('')

        rpm_builder = RpmBuilder(profile=self.profile, container_spec=self.container_spec)

        nvr = rpm_builder.get_main_package_nvr()
        name, version, _ = nvr.rsplit('-', 2)
        release = 1

        source = PackageFlatpakSourceInfo(
            self.container_spec.flatpak, rpm_builder.runtime_info
        )

        runtimever = source.spec.runtime_version
        assert runtimever

        repos = rpm_builder.get_repos(for_container=True)

        arch = get_arch()

        workdir = Path(arch.rpm) / "work/oci"
        if os.path.exists(workdir):
            info(f"Removing old output directory {workdir}")
            shutil.rmtree(workdir)

        os.makedirs(workdir)
        info(f"Writing results to {workdir}")

        builder = FlatpakBuilder(source, workdir, ".", flatpak_metadata=self.flatpak_metadata)

        component_label = source.spec.component or name
        name_label = source.spec.name or name

        builder.add_labels({'name': name_label,
                            'com.redhat.component': component_label,
                            'version': version,
                            'release': release})

        mock_cfg_path = workdir / "mock.cfg"
        mock_cfg = make_mock_cfg(
            arch=arch,
            chroot_setup_cmd='install /bin/bash glibc-minimal-langpack shadow-utils tar',
            includepkgs=builder.get_includepkgs(),
            releasever=self.profile.release_from_runtime_version(runtimever),
            repos=repos,
            root_cache_enable=False,
            runtimever=runtimever
        )
        with open(mock_cfg_path, "w") as f:
            f.write(mock_cfg)

        finalize_script = dedent("""\
            #!/bin/sh
            set -ex
            userdel -f mockbuild
            groupdel mock
            """ + builder.get_cleanup_script()) + dedent("""\
            cd /
            exec tar cf - --anchored --exclude='./sys/*' --exclude='./proc/*' --exclude='./dev/*' --exclude='./run/*' ./
        """)  # noqa: E501

        finalize_script_path = os.path.join(workdir, 'finalize.sh')
        with open(finalize_script_path, 'w') as f:
            f.write(finalize_script)
            os.fchmod(f.fileno(), 0o0755)

        info('Initializing installation path')
        check_call(['mock', '-q', '-r', mock_cfg_path, '--clean'])

        info('Installing packages')
        check_call(
            ['mock', '-r', mock_cfg_path, '--install'] + sorted(builder.get_install_packages())
        )

        info('Cleaning and exporting filesystem')
        check_call([
            'mock', '-q', '-r', mock_cfg_path, '--copyin', finalize_script_path, '/root/finalize.sh'
        ])

        builder.root = "."

        args = ['mock', '-q', '-r', mock_cfg_path, '--shell', '/root/finalize.sh']
        log_call(args)
        process = subprocess.Popen(args, stdout=subprocess.PIPE)
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
            die(f"finalize.sh failed (exit status={process.returncode})")

        ref_name, outfile, tarred_outfile = builder.build_container(filesystem_tar)

        local_outname = f"{name}-{version}-{release}.oci.tar.gz"

        info('Compressing result')
        with open(local_outname, 'wb') as f:
            subprocess.check_call(['gzip', '-c', tarred_outfile], stdout=f)

        important('Created ' + local_outname)

        return local_outname
