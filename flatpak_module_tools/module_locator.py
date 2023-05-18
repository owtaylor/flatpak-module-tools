from collections import OrderedDict
from dataclasses import dataclass
import os
import re
import shutil

from .utils import check_call

from .utils import info, ModuleSpec
from .flatpak_builder import ModuleInfo
from .get_module_builds import get_module_builds

import gi
gi.require_version('Modulemd', '2.0')
from gi.repository import Modulemd   # type: ignore  # noqa: E402


class Build(ModuleInfo):
    def __init__(self, mmd, path: str):
        self.mmd = mmd
        self.name = mmd.props.module_name
        self.stream = mmd.props.stream_name
        self.version = mmd.props.version
        self.path = path

    def yum_config(self):
        exclude = ','.join(self.mmd.get_rpm_filters())

        return f"""[{self.name}-{self.stream}]
name={self.name}-{self.stream}
baseurl=file://{self.path}
enabled=1
excludepkgs={exclude}
priority=10
# just treat modular packages as normal packages - don't expect modular metadata
module_hotfixes=true
"""

    def has_module_metadata(self):
        # Parsing the repomd.xml file would be cleaner and almost as simple,
        # but this matches how the dnf finds the modules file

        for f in os.listdir(os.path.join(self.path, 'repodata')):
            if "modules.yaml" in f:
                return True

        return False


class LocalBuild(Build):
    def __init__(self, path):
        mmd_path = os.path.join(path, 'modules.yaml')
        mmd = Modulemd.ModuleStream.read_file(mmd_path, False)
        mmd = mmd.upgrade(Modulemd.ModuleStreamVersionEnum.TWO)

        super().__init__(mmd, path)

        self.rpms = [a + '.rpm' for a in mmd.get_rpm_artifacts()]

    def __repr__(self):
        return '<LocalBuild {name}:{stream}:{version}>'.format(**self.__dict__)  # noqa: FS002


class KojiBuild(Build):
    def __init__(self, mmd, path, koji_tag, rpms):
        super().__init__(mmd, path)

        self.rpms = rpms

        self.koji_tag = koji_tag

    def __repr__(self):
        return '<KojiBuild {name}:{stream}:{version}>'.format(**self.__dict__)  # noqa: FS002


def get_module_info(module_name, stream, version=None, koji_config=None, koji_profile='koji'):
    builds = get_module_builds(module_name, stream, version=version,
                               koji_config=koji_config, koji_profile=koji_profile,
                               include_rpms=True)

    if len(builds) == 0:
        raise RuntimeError(
            f"No module builds found for {ModuleSpec(module_name, stream, version).to_str()}"
        )
    elif len(builds) > 1:
        raise RuntimeError(
            "Multiple builds against different contexts found for "
            f"{ModuleSpec(module_name, stream, version)}"
        )
    build = builds[0]

    modulemd_str = build['extra']['typeinfo']['module']['modulemd_str']
    mmd = Modulemd.ModuleStream.read_string(modulemd_str, False)
    # Make sure that we have the v2 'dependencies' format
    mmd = mmd.upgrade(Modulemd.ModuleStreamVersionEnum.TWO)

    rpms = ['{name}-{epochnum}:{version}-{release}.{arch}.rpm'.format(  # noqa: FS002
                epochnum=rpm['epoch'] or 0, **rpm
            )
            for rpm in build['gmb_rpms']
            if rpm['arch'] in ('x86_64', 'noarch')]

    return mmd, build['extra']['typeinfo']['module']['content_koji_tag'], rpms


class ModuleLocator:
    @dataclass
    class Config:
        koji_config: str
        koji_profile: str
        cache_dir: str
        mock_resultsdir: str

    def __init__(self, profile):
        self.profile = profile
        self.conf = ModuleLocator.Config(
            koji_config=profile.koji_config,
            koji_profile=profile.koji_profile,
            cache_dir=os.path.expanduser('~/modulebuild/cache'),
            mock_resultsdir=os.path.expanduser('~/modulebuild/builds')
        )

        self.local_build_ids = []
        self._local_build_info = None

        self._cached_remote_builds = {}

    def add_local_build(self, build_id):
        self.local_build_ids.append(build_id)
        self._local_build_info = None

    def get_local_build_info(self):
        if self._local_build_info is not None:
            return self._local_build_info

        if not self.local_build_ids:
            self._local_build_info = {}
            return self._local_build_info

        builds = []
        try:
            for d in os.listdir(self.conf.mock_resultsdir):
                m = re.match('^module-(.*)-([^-]*)-([0-9]+)$', d)
                if m:
                    builds.append((m.group(1), m.group(2), int(m.group(3)), d))
        except OSError:
            pass

        # Sort with the biggest version first
        builds.sort(key=lambda x: x[2], reverse=True)

        result = {}

        for build_id in self.local_build_ids:
            parts = build_id.split(':')
            if len(parts) < 1 or len(parts) > 3:
                raise RuntimeError(
                    f'The local build "{build_id}" couldn\'t be be parsed '
                    "into NAME[:STREAM[:VERSION]]"
                )

            name = parts[0]
            stream = parts[1] if len(parts) > 1 else None
            version = int(parts[2]) if len(parts) > 2 else None

            found_build = None
            for build in builds:
                if name != build[0]:
                    continue
                if stream is not None and stream != build[1]:
                    continue
                if version is not None and version != build[2]:
                    continue

                found_build = build
                break

            if not found_build:
                raise RuntimeError(
                    f'The local build "{build_id}" '
                    f'couldn\'t be found in "{self.conf.mock_resultsdir}"')

            local_build = LocalBuild(os.path.join(
                self.conf.mock_resultsdir, found_build[3], 'results'
            ))

            if found_build[0] != local_build.name or \
               found_build[1] != local_build.stream or \
               found_build[2] != local_build.version:
                raise RuntimeError(
                    f'Parsed metadata results for "{found_build[3]}" '
                    'don\'t match the directory name'
                )
            result[(local_build.name, local_build.stream)] = local_build

        self._local_build_info = result
        return self._local_build_info

    def locate(self, name, stream, version=None):
        # FIXME: handle version
        key = (name, stream)
        if key in self.get_local_build_info():
            return self.get_local_build_info()[key]

        if key in self._cached_remote_builds:
            return self._cached_remote_builds[key]

        info(f"Querying Koji for information on {name}:{stream}")

        modulemd, koji_tag, rpms = get_module_info(name, stream,
                                                   version=version,
                                                   koji_config=self.conf.koji_config,
                                                   koji_profile=self.conf.koji_profile)

        path = os.path.join(self.conf.cache_dir, "modules", koji_tag)
        self._cached_remote_builds[key] = KojiBuild(modulemd, path, koji_tag, rpms)
        return self._cached_remote_builds[key]

    def ensure_downloaded(self, build):
        if os.path.exists(build.path):
            return

        # We want to re-use modules downloaded by MBS, but unfortunately, when
        # MBS downloads a module it doesn't include the module metadata, which
        # DNF wants to be able to do module dependencies. So we do a two step
        # process:
        #
        #  1) Use MBS code to download the module (which runs createrepo)
        #  2) Create another directory with a symlink to the MBS download,
        #     run createrepo *again* there, and then add in the module metadata
        #     with modifyrepo.
        #
        # (Adding module metadata directly to the MBS download directory would
        # break the module build, since MBS is expecting a bare repository
        # without module metadata.)
        #
        # Long term solution: Enable ODCS in Fedora infrastructure,
        #   stop downloading module builds

        koji_path = os.path.join(self.conf.cache_dir, "koji_tags", build.koji_tag)
        if not os.path.exists(koji_path):
            info(f"Downloading {build.name}:{build.stream} to {build.path}")
            from module_build_service.builder.utils import create_local_repo_from_koji_tag
            create_local_repo_from_koji_tag(self.conf, build.koji_tag, koji_path)

        os.makedirs(build.path)
        success = False
        try:
            os.symlink(os.path.join("../../koji_tags/", build.koji_tag),
                       os.path.join(build.path, "rpms"))
            check_call(["createrepo_c", build.path])

            modules_path = os.path.join(build.path, 'modules.yaml')
            repodata_path = os.path.join(build.path, 'repodata')
            mmd_index = Modulemd.ModuleIndex.new()
            mmd_index.add_module_stream(build.mmd)
            with open(modules_path, "w") as f:
                f.write(mmd_index.dump_to_string())
            check_call(['/usr/bin/modifyrepo_c', '--mdtype=modules', modules_path, repodata_path])
            success = True
        finally:
            if not success:
                shutil.rmtree(build.path)

    def _get_builds_recurse(self, builds, name, stream):
        if name == 'platform':
            # Pseudo-module doesn't quite exist
            return

        if name in builds:
            build = builds[name]
            if build.stream != stream:
                raise RuntimeError("Stream conflict for {}, both {} and {} are required",
                                   name, build.stream, stream)
            return build

        build = self.locate(name, stream)
        builds[name] = build

        dependencies = build.mmd.get_dependencies()
        # A built module should have its dependencies already expanded
        assert len(dependencies) <= 1

        if len(dependencies) == 1:
            for required_module in dependencies[0].get_runtime_modules():
                rs = dependencies[0].get_runtime_streams(required_module)
                # should already be expanded to a single stream
                assert len(rs) == 1
                self._get_builds_recurse(builds, required_module, rs[0])

    def get_builds(self, name, stream, version=None):
        builds = OrderedDict()

        self._get_builds_recurse(builds, name, stream)

        return builds
