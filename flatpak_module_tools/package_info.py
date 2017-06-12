import dnf
import os
import sys

from module_build_service.builder.utils import create_local_repo_from_koji_tag
from distutils.version import LooseVersion
from module_build_service import pdc

REPO_F26 = "http://download.devel.redhat.com/pub/fedora/linux/development/26/Everything/x86_64/os/"
REPO_F26_SOURCE = "http://download.devel.redhat.com/pub/fedora/linux/development/26/Everything/source/tree/"

REPO_F26_UPDATES = "http://download.devel.redhat.com/pub/fedora/linux/updates/26/x86_64/"
REPO_F26_UPDATES_SOURCE = "http://download.devel.redhat.com/pub/fedora/linux/updates/26/SRPMS/"

REPO_F26_UPDATES_TESTING = "http://download.devel.redhat.com/pub/fedora/linux/updates/testing/26/x86_64"
REPO_F26_UPDATES_TESTING_SOURCE = "http://download.devel.redhat.com/pub/fedora/linux/updates/testing/26/SRPMS"

class PackageInfo(object):
    def __init__(self, mbs_config):
        self.mbs_config = mbs_config
        self.session = pdc.get_pdc_client_session(self.mbs_config)

        self.base = dnf.Base()

        self._add_repo(self.base, 'f26', REPO_F26)
        self._add_repo(self.base, 'f26-source', REPO_F26_SOURCE)
        self._add_repo(self.base, 'f26-updates', REPO_F26_UPDATES)
        self._add_repo(self.base, 'f26-updates-source', REPO_F26_UPDATES_SOURCE)
        self._add_repo(self.base, 'f26-updates-testing', REPO_F26_UPDATES_TESTING)
        self._add_repo(self.base, 'f26-updates-testing-source', REPO_F26_UPDATES_TESTING_SOURCE)

        self._add_module_repo(self.base, 'base-runtime', 'f26', priority=10)
        self._add_module_repo(self.base, 'shared-userspace', 'f26', priority=10)
        self._add_module_repo(self.base, 'perl', 'f26', priority=10)
        self._add_module_repo(self.base, 'common-build-dependencies', 'f26', priority=10)
#        self._add_module_repo(self.base, 'bootstrap', 'f26', priority=20)
        self.base.fill_sack(load_available_repos=True, load_system_repo=False)

#        self.base = dnf.Base()
#        self.base.read_all_repos()
#        dnfpluginscore.lib.enable_source_repos(self.base.repos)

#        self.base.fill_sack(load_system_repo=False)

    def _module_to_tag(self, name, stream):
        return pdc.get_module_tag(self.session, {'variant_id': name, 'variant_stream': stream, 'variant_type': 'module', 'active': True})

    def _download_tag(self, name, stream, tag):
        repo_dir = os.path.join(self.mbs_config.cache_dir, "koji_tags", tag)
        print >>sys.stderr, "Downloading %s:%s to %s" % (name, stream, repo_dir)
        create_local_repo_from_koji_tag(self.mbs_config, tag, repo_dir)

    def _add_module_repo(self, base, name, stream, priority=99):
        tag = self._module_to_tag(name, stream)
        path = os.path.join(self.mbs_config.cache_dir, 'koji_tags', tag)
        if not os.path.exists(path):
            self._download_tag(name, stream, tag)
        self._add_repo(base, name + ':' + stream, 'file://' + path, priority=priority)

    def _add_repo(self, base, reponame, repourl, priority=99):
        print "Loading", reponame
        if LooseVersion(dnf.__version__) < LooseVersion("2.0.0"):
            repo = dnf.repo.Repo(reponame, self.base.conf.cachedir)
        else:
            repo = dnf.repo.Repo(reponame, self.base.conf)
        repo.baseurl = repourl
        repo.priority = priority
        repo.load()
        repo.enable()
        base.repos.add(repo)

    def find_source_package(self, name):
        source_pkg = None
        for p in self.base.sack.query().filter(name=name, arch='src'):
            return p

        return None

