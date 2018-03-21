import module_build_service as mbs

from collections import OrderedDict
import os
import re
import sys

import gi
gi.require_version('Modulemd', '1.0')
from gi.repository import Modulemd

from pdc_client import PDCClient

from module_build_service.builder.utils import create_local_repo_from_koji_tag

from utils import die, info
from flatpak_builder import ModuleInfo

class Build(ModuleInfo):
    def yum_config(self):
        exclude = ','.join(self.mmd.props.rpm_filter.get())

        return """[{name}-{stream}]
name={name}-{stream}
baseurl=file://{path}
enabled=1
excludepkgs={exclude}
priority=10
""".format(name=self.name, stream=self.stream, path=self.path, exclude=exclude)

class LocalBuild(Build):
    def __init__(self, path):
        mmd_path = os.path.join(path, 'modules.yaml')
        mmds = Modulemd.Module.new_all_from_file(mmd_path)
        mmd = mmds[0]
        mmd.upgrade()

        self.name = mmd.props.name
        self.stream = mmd.props.stream
        self.version = mmd.props.version

        self.path = path
        self.mmd = mmd

        self.rpms = [a + '.rpm' for a in mmd.props.rpm_artifacts.get()]

    def __repr__(self):
        return '<LocalBuild {name}:{stream}:{version}>'.format(**self.__dict__)

class KojiBuild(Build):
    def __init__(self, module, path):
        self.name = module['name']
        self.stream = module['stream']
        self.version = module['version']

        self.path = path
        self.mmd = Modulemd.Module.new_from_string(module['modulemd'])
        # Make sure that we have the v2 'dependencies' format
        self.mmd.upgrade()

        self.koji_tag = module['koji_tag']

    def __repr__(self):
        return '<KojiBuild {name}:{stream}:{version}>'.format(**self.__dict__)

class ModuleLocator(object):
    class Config(object):
        pass

    def __init__(self):
        self.conf = ModuleLocator.Config()
        self.conf.pdc_url = 'https://pdc.fedoraproject.org/rest_api/v1'
        self.conf.pdc_insecure = False
        self.conf.pdc_develop = True

        self.conf.koji_config = '/etc/module-build-service/koji.conf'
        self.conf.koji_profile = 'koji'

        self.conf.cache_dir = os.path.expanduser('~/modulebuild/cache')
        self.conf.mock_resultsdir = os.path.expanduser('~/modulebuild/builds')

        # The effect of develop=True is that requests to the PDC are made without authentication;
        # since we our interaction with the PDC is read-only, this is fine for our needs and
        # makes things simpler.
        self.pdc_client = PDCClient(server=self.conf.pdc_url, ssl_verify=True, develop=True)

        self.local_build_ids = []
        self._local_build_info = None

        self._cached_remote_builds = {}

    def download_tag(self, name, stream, tag):
        repo_dir = os.path.join(self.conf.cache_dir, "koji_tags", tag)
        info("Downloading %s:%s to %s" % (name, stream, repo_dir))
        create_local_repo_from_koji_tag(self.conf, tag, repo_dir)

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
        builds.sort(lambda a, b: -cmp(a[2], b[2]))

        result = {}

        build_dir = self.conf.mock_resultsdir
        for build_id in self.local_build_ids:
            parts = build_id.split(':')
            if len(parts) < 1 or len(parts) > 3:
                raise RuntimeError(
                    'The local build "{0}" couldn\'t be be parsed into NAME[:STREAM[:VERSION]]'.format(build_id))

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
                    'The local build "{0}" couldn\'t be found in "{1}"'.format(build_id, self.conf.mock_resultsdir))

            local_build = LocalBuild(os.path.join(self.conf.mock_resultsdir, found_build[3], 'results'))

            if found_build[0] != local_build.name or \
               found_build[1] != local_build.stream or \
               found_build[2] != local_build.version:
                raise RuntimeError(
                    'Parsed metadata results for "{0}" don\'t match the directory name'.format(found_build[3]))
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

        info("Querying PDC for information on %s:%s" % (name, stream))

        query = {
            'name': name,
            'stream': stream,
            'active': True,
        }

        if version is not None:
            query['version'] = version

        retval = self.pdc_client['modules/'](page_size=1,
                                             fields=['name', 'stream', 'version', 'modulemd', 'rpms', 'koji_tag'],
                                             ordering='-version',
                                             **query)
        # Error handling
        if len(retval['results']) == 0:
            raise RuntimeError("Failed to find module in PDC %r" % query)
        if len(retval['results']) != 1:
            raise RuntimeError("Multiple modules in the PDC matched %r" % query)

        module = retval['results'][0]

        path = os.path.join(self.conf.cache_dir, "koji_tags", module['koji_tag'])
        self._cached_remote_builds[key] = KojiBuild(module, path)
        return self._cached_remote_builds[key]

    def ensure_downloaded(self, build):
        if not os.path.exists(build.path):
            info("Downloading %s:%s to %s" % (build.name, build.stream, build.path))
            create_local_repo_from_koji_tag(self.conf, build.koji_tag, build.path)

    def _get_builds_recurse(self, builds, name, stream):
        if name in builds:
            build = builds[name]
            if build.stream != stream:
                raise RuntimeError("Stream conflict for {}, both {} and {} are required",
                                   name, build.stream, stream)
            return build


        build = self.locate(name, stream)
        builds[name] = build

        dependencies = build.mmd.props.dependencies
        # A built module should have its dependencies already expanded
        assert len(dependencies) == 1

        for n, required_streams in dependencies[0].props.requires.items():
            rs = required_streams.get()
            # should already be expanded to a single stream
            assert len(rs) == 1
            self._get_builds_recurse(builds, n, rs[0])

    def get_builds(self, name, stream, version=None):
        builds = OrderedDict()

        self._get_builds_recurse(builds, name, stream)

        return builds
