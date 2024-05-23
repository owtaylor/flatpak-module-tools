from dataclasses import dataclass
import os
from urllib.parse import urljoin

import koji

from ..config import get_profile
from ..utils import Arch


XDG_CACHE_HOME = (os.environ.get("XDG_CACHE_HOME")
                  or os.path.expanduser("~/.cache"))
CACHEDIR = os.path.join(XDG_CACHE_HOME, "flatpak-module-tools")


@dataclass
class RepoDefinition:
    name: str
    url: str
    arch: Arch

    @property
    def is_local(self):
        return self.url.startswith("file:")

    @property
    def local_cache_path(self):
        if self.is_local:
            return self.url[5:]
        else:
            return os.path.join(CACHEDIR, "repos", self.name, self.arch.rpm)

    @property
    def remote_metadata_url(self):
        return urljoin(self.url, 'repodata/')

    @remote_metadata_url.setter
    def remote_metadata_url(self, url):
        if not url.endswith('/repodata/'):
            raise ValueError("'url' must end with '/repodata/'")

        self.url = url[:-9]  # with 'repodata/' stripped

    @property
    def local_metadata_path(self):
        return os.path.join(self.local_cache_path, 'repodata/')

    @staticmethod
    def for_koji_tag(tag: str, arch: Arch):
        profile = get_profile()

        pathinfo = koji.PathInfo(topdir=profile.koji_options['topurl'])
        baseurl = pathinfo.repo("latest", tag) + "/" + arch.rpm + "/"

        return RepoDefinition(name=tag,
                              url=baseurl,
                              arch=arch)
