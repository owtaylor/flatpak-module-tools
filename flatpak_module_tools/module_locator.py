import module_build_service as mbs

import os
import re
import sys

import modulemd

from module_build_service.builder.utils import create_local_repo_from_koji_tag
from module_build_service import pdc

from utils import die

class Build(object):
    def yum_config(self):
        modulebuild = os.path.expanduser("~/modulebuild")
        assert self.path.startswith(modulebuild)
        dest_path = "/modulebuild" + self.path[len(modulebuild):]
        cf_item = """[{name}-{stream}]
name={name}-{stream}
baseurl=file://{path}
enabled=1
""".format(name=self.name, stream=self.stream, path=dest_path)

        if self.name == 'base-runtime':
            cf_item += "exclude= gobject-introspection* libpeas*\n"

        return cf_item, self.path, dest_path

class LocalBuild(Build):
    def __init__(self, path):
        mmd_path = os.path.join(path, 'modules.yaml')
        mmds = modulemd.load_all(mmd_path)
        mmd = mmds[0]

        self.path = path
        self.mmd = mmd
        self.name = mmd.name
        self.stream = mmd.stream
        self.version = mmd.version

    def __repr__(self):
        return '<LocalBuild {name}:{stream}:{version}>'.format(**self.__dict__)

class KojiBuild(Build):
    def __init__(self, module, path):
        self.koji_tag = module['koji_tag']
        self.path = path
        self.mmd = pdc._extract_modulemd(module['modulemd'])
        self.name = module['variant_id']
        self.stream = module['variant_version']
        self.version = module['variant_release']

    def __repr__(self):
        return '<KojiBuild {name}:{stream}:{version}>'.format(**self.__dict__)

class ModuleLocator(object):
    class Config(object):
        pass

    def __init__(self):
        self.conf = ModuleLocator.Config()
        self.conf.pdc_url = 'http://pdc.fedoraproject.org/rest_api/v1'
        self.conf.pdc_insecure = False
        self.conf.pdc_develop = True

        self.conf.koji_config = '/etc/module-build-service/koji.conf'
        self.conf.koji_profile = 'koji'

        self.conf.cache_dir = os.path.expanduser('~/modulebuild/cache')
        self.conf.mock_resultsdir = os.path.expanduser('~/modulebuild/builds')

        self.session = pdc.get_pdc_client_session(self.conf)
        self.local_build_ids = []
        self._local_build_info = None

        self._cached_remote_builds = {}

    def download_tag(self, name, stream, tag):
        repo_dir = os.path.join(self.conf.cache_dir, "koji_tags", tag)
        print >>sys.stderr, "Downloading %s:%s to %s" % (name, stream, repo_dir)
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

    def locate(self, name, stream):
        key = (name, stream)
        if key in self.get_local_build_info():
            return self.get_local_build_info()[key]

        if key in self._cached_remote_builds:
            return self._cached_remote_builds[key]

        print >>sys.stderr, "Querying PDC for information on %s:%s" % (name, stream)
        module = pdc.get_module(self.session, {'variant_id': name, 'variant_stream': stream, 'variant_type': 'module', 'active': True})
        if not module:
            die("Can't find module for {}:{}".format(name, stream))

        path = os.path.join(self.conf.cache_dir, "koji_tags", module['koji_tag'])
        self._cached_remote_builds[key] = KojiBuild(module, path)
        return self._cached_remote_builds[key]

    def ensure_downloaded(self, build):
        if not os.path.exists(build.path):
            print >>sys.stderr, "Downloading %s:%s to %s" % (build.name, build.stream, build.path)
            create_local_repo_from_koji_tag(self.conf, build.koji_tag, build.path)

    def _get_yum_config_recurse(self, added, path_map, name, stream):
        key = (name, stream)
        if key in added:
            return ""

        config = ""
        build = self.locate(name, stream)
        added.add(key)

        self.ensure_downloaded(build)
        cf_item, source_path, dest_path = build.yum_config()
        config += cf_item
        path_map[source_path] = dest_path

        for n, s in build.mmd.requires.items():
            config += self._get_yum_config_recurse(added, path_map, n, s)
        return config

    def build_yum_config(self, name, stream):
        added = set()
        path_map = {}
        return self._get_yum_config_recurse(added, path_map, name, stream), path_map
