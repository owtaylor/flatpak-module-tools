import dnf
import os
import sys

from module_build_service.builder.utils import create_local_repo_from_koji_tag
from distutils.version import LooseVersion
from module_build_service import pdc

REPO_F26 = "https://mirrors.fedoraproject.org/metalink?repo=fedora-26&arch=x86_64"
REPO_F26_SOURCE = "https://mirrors.fedoraproject.org/metalink?repo=fedora-source-26&arch=x86_64"

REPO_F26_UPDATES = "https://mirrors.fedoraproject.org/metalink?repo=updates-released-f26&arch=x86_64"
REPO_F26_UPDATES_SOURCE = "https://mirrors.fedoraproject.org/metalink?repo=updates-released-source-f26&arch=x86_64"

REPO_F26_UPDATES_TESTING = "https://mirrors.fedoraproject.org/metalink?repo=updates-testing-f26&arch=x86_64"
REPO_F26_UPDATES_TESTING_SOURCE = "https://mirrors.fedoraproject.org/metalink?repo=updates-testing-source-f26&arch=x86_64"

class PackageInfo(object):
    def __init__(self, locator, requires_modules, buildrequires_modules):
        self.locator = locator

        self.base = dnf.Base()

        self._add_repo(self.base, 'f26', metalink=REPO_F26)
        self._add_repo(self.base, 'f26-source', metalink=REPO_F26_SOURCE)
        self._add_repo(self.base, 'f26-updates', metalink=REPO_F26_UPDATES)
        self._add_repo(self.base, 'f26-updates-source', metalink=REPO_F26_UPDATES_SOURCE)
        self._add_repo(self.base, 'f26-updates-testing', metalink=REPO_F26_UPDATES_TESTING)
        self._add_repo(self.base, 'f26-updates-testing-source', metalink=REPO_F26_UPDATES_TESTING_SOURCE)

        requires_builds = self.locator.get_builds(requires_modules)
        for key, build in requires_builds.items():
            name, stream = key
            locator.ensure_downloaded(build)
            self._add_repo(self.base, name + ':' + stream, 'file://' + build.path, priority=10)

        buildrequires_builds = self.locator.get_builds(buildrequires_modules)
        for key, build in buildrequires_builds.items():
            if not key in requires_builds:
                name, stream = key
                locator.ensure_downloaded(build)
                self._add_repo(self.base, name + ':' + stream, 'file://' + build.path, priority=20)

        self.base.fill_sack(load_available_repos=True, load_system_repo=False)

    def _add_repo(self, base, reponame, repourl=None, metalink=None, priority=99):
        print "Loading", reponame
        if LooseVersion(dnf.__version__) < LooseVersion("2.0.0"):
            repo = dnf.repo.Repo(reponame, self.base.conf.cachedir)
        else:
            repo = dnf.repo.Repo(reponame, self.base.conf)
        if repourl is not None:
            repo.baseurl = repourl
        elif metalink is not None:
            repo.metalink = metalink
        else:
            raise RuntimeError("Either baseurl or metalink must be specified")
        repo.priority = priority
        repo.load()
        repo.enable()
        base.repos.add(repo)

    def find_source_package(self, name):
        source_pkg = None
        for p in self.base.sack.query().filter(name=name, arch='src'):
            return p

        return None

