import jinja2
import os
import yaml
import re
import shutil
import subprocess
import sys
from textwrap import dedent

from .flatpak_builder import FlatpakBuilder, FlatpakSourceInfo
from .module_locator import ModuleLocator
from .utils import check_call, die, header, important, info, split_module_spec

class ContainerBuilder(object):
    def __init__(self, profile, containerspec, from_local=False, local_builds=[]):
        self.profile = profile
        self.containerspec = os.path.abspath(containerspec)
        self.from_local = from_local
        self.local_builds = local_builds

        with open(self.containerspec) as f:
            container_yaml = yaml.safe_load(f)

        compose_yaml = container_yaml.get('compose', {})
        modules = compose_yaml.get('modules', [])
        if not modules:
            die("No modules specified in the compose section of '{}'".format(self.containerspec))

        self.flatpak_yaml = container_yaml.get('flatpak', None)
        if not self.flatpak_yaml:
            die("No flatpak section in '{}'".format(containerspec))

        compose_yaml = container_yaml.get('compose', {})
        modules = compose_yaml.get('modules', [])
        if not modules:
            die("No modules specified in the compose section of '{}'".format(self.containerspec))

        if len(modules) > 1:
            warn("Multiple modules specified in compose section of '{}', using first".format(containerspec))

        self.module_spec = split_module_spec(modules[0])

    def _get_platform_version(self, builds):
        # Streams should already be expanded in the modulemd's that we retrieve
        #  modules were built against a particular dependency.
        def get_stream(module, req, req_stream):
            req_stream_list = req_stream.get()
            if len(req_stream_list) != 1:
                die("%s: stream list for '%s' is not expanded (%s)" %
                    (module.props.name, req, req_stream_list))
            return req_stream_list[0]

        platform_stream = None

        # Find the platform stream to get the base package set
        for build in builds.values():
            for dep in build.mmd.peek_dependencies():
                for req, req_stream in dep.peek_requires().items():
                    if req == 'platform':
                        platform_stream = get_stream(build.mmd, req, req_stream)

        if platform_stream is None:
            die("Unable to determine base OS version from 'platform' module stream")

        m = re.match(self.profile.platform_stream_pattern, platform_stream)
        if m is None:
            die("'platform' module stream '{}' doesn't match '{}'".format(
                platform_stream, self.profile.platform_stream_pattern))

        return m.group(1)

    def build(self):
        header('BUILDING CONTAINER')
        important('container spec: {}'.format(self.containerspec))
        important('')

        module_build_id = self.module_spec.to_str(include_profile=False)

        locator = ModuleLocator(self.profile)
        if self.from_local:
            locator.add_local_build(module_build_id)
        for build_id in self.local_builds:
            locator.add_local_build(build_id)

        builds = locator.get_builds(self.module_spec.name, self.module_spec.stream, self.module_spec.version)
        base_build = list(builds.values())[0]

        builddir = os.path.expanduser("~/modulebuild/flatpaks")
        workdir = os.path.join(builddir, "{}-{}-{}".format(base_build.name, base_build.stream, base_build.version))
        if os.path.exists(workdir):
            info("Removing old output directory {}".format(workdir))
            shutil.rmtree(workdir)

        os.makedirs(workdir)
        info("Writing results to {}".format(workdir))

        has_modulemd = {}

        for build in builds.values():
            locator.ensure_downloaded(build)
            has_modulemd[build.name + ':' + build.stream] = build.has_module_metadata()

        repos = [build.yum_config() for build in builds.values()]

        source = FlatpakSourceInfo(self.flatpak_yaml, builds, base_build, self.module_spec.profile)

        builder = FlatpakBuilder(source, workdir, ".")

        env = jinja2.Environment(loader=jinja2.PackageLoader('flatpak_module_tools', 'templates'),
                                 autoescape=False)
        template = env.get_template('mock.cfg.j2')

        output_path = os.path.join(workdir, 'mock.cfg')

        # Check if DNF is new enough to support modules, if not, all packages from modular
        # repositories will automatically be enabled. If yes, we'll need to enable any
        # modular repositories - *if they actually have module metadata*
        try:
            subprocess.check_output(['dnf', 'module', 'list', '--enabled'],
                                    stderr=subprocess.STDOUT)
            have_dnf_module=True
        except subprocess.CalledProcessError:
            have_dnf_module=False

        platform_version = self._get_platform_version(builds)
        base_repo_url = self.profile.base_repo_url.format(platform=platform_version)
        template.stream(arch='x86_64',
                        base_repo_url=base_repo_url,
                        includepkgs=builder.get_includepkgs(),
                        repos=repos).dump(output_path)

        finalize_script = dedent("""\
            #!/bin/sh
            set -ex
            userdel -f mockbuild
            groupdel mock
            """ + builder.get_cleanup_script()) + dedent("""\
            (cd / && tar cf - --anchored --exclude='./sys/*' --exclude='./proc/*' --exclude='./dev/*' --exclude='./run/*' ./)
        """)

        finalize_script_path = os.path.join(workdir, 'finalize.sh')
        with open(finalize_script_path, 'w') as f:
            f.write(finalize_script)
            os.fchmod(f.fileno(), 0o0755)

        info('Initializing installation path')
        check_call(['mock', '-q', '-r', output_path, '--clean'])

        # Using mock's config_opts['module_enable'] doesn't work because dnf tries
        # to enable modules before chroot_setup_cmd installs system-release, but
        # dnf needs /etc/os-release to figure out the platform module. So we do it this way.
        # https://github.com/rpm-software-management/mock/issues/232#issuecomment-456340663
        if have_dnf_module:
            info('Enabling modules')
            to_enable = [x for x in builder.get_enable_modules() if has_modulemd[x]]
            check_call(['mock', '-r', output_path, '--dnf-cmd', 'module', 'enable'] + to_enable)

        info('Installing packages')
        check_call(['mock', '-r', output_path, '--install'] + sorted(builder.get_install_packages()))

        info('Cleaning and exporting filesystem')
        check_call(['mock', '-q', '-r', output_path, '--copyin', finalize_script_path, '/root/finalize.sh'])

        builder.root = "."

        process = subprocess.Popen(['mock', '-q', '-r', output_path, '--shell', '/root/finalize.sh'],
                                  stdout=subprocess.PIPE)
        filesystem_tar, manifestfile = builder._export_from_stream(process.stdout)
        process.wait()
        if process.returncode != 0:
            die("finalize.sh failed (exit status=%d)" % rv)

        ref_name, outfile, tarred_outfile = builder.build_container(filesystem_tar)

        local_outname = "{}-{}-{}.oci.tar.gz".format(base_build.name, base_build.stream, base_build.version)

        info('Compressing result')
        with open(local_outname, 'wb') as f:
            subprocess.check_call(['gzip', '-c', tarred_outfile], stdout=f)

        important('Created ' + local_outname)

        return local_outname
