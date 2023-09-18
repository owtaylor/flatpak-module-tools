# When building a Flatpak, the version label will be based on the RPM version
# of the main RPM installed in the package. *But* we need the VERSION label
# set *before* building the container to support things like the bump_release
# plugin. So, what this code does is download and parse metadata from a
# repourl (which might have multiple repositories in it) to find that
# main version.
#
# Not supported in parsing repository URLs
#
#  includepkgs and excludepkgs
#  mirrorlist
#  metalink
#  subsituting $releasever
#
# For includepkgs and excludepkgs, see version history. (Removed only for simplicity.)
# mirrorlist and metalink support wouldn't be that hard. $releasever would
# require it being known and passed in.

from dataclasses import dataclass
from configparser import RawConfigParser
from functools import total_ordering
import gzip
import logging
from pathlib import Path
from typing import List, Optional, Union
import xml.etree.ElementTree as ET

import requests

from .rpm_utils import VersionInfo
from .utils import Arch

logger = logging.getLogger(__name__)


@total_ordering
class ExtendedVersionInfo(VersionInfo):
    """Represents a particular binary package version"""
    priority: int

    def __init__(self, *,
                 epoch: Union[str, int, None], version: str, release: str,
                 priority: int):
        super().__init__(epoch, version, release)
        self.priority = priority

    def __lt__(self, other):
        return (
            self.priority > other.priority or
            (self.priority == other.priority and super().__lt__(other))
        )

    def __eq__(self, other):
        return (
            self.priority == other.priority and
            super().__eq__(other)
        )

    def __ne__(self, other):
        return (
            self.priority != other.priority or
            super().__ne__(other)
        )

    def __repr__(self):
        return super().__repr__() + f", priority={self.priority}"


@dataclass(frozen=True)
class RepoInfo:
    """Represents the parts of a yum/dnf repository definition we support"""
    baseurl: Union[str, Path]
    proxy: Optional[str] = None
    priority: int = 99

    def get_proxies(self):
        if self.proxy:
            return {
                'http': self.proxy,
                'https': self.proxy,
            }
        else:
            return None


def _extract_repo_info(session: requests.Session, repourl: str):
    """Parses a repository file and extract any repositories found"""
    response = session.get(repourl)
    response.raise_for_status()

    cp = RawConfigParser()
    cp.read_string(response.text)

    for section in cp.sections():
        baseurl = cp.get(section, "baseurl", fallback=None)
        enabled = cp.getboolean(section, "enabled", fallback=True)
        priority = cp.getint(section, "priority", fallback=99)

        if not enabled:
            continue

        if baseurl is None:
            logger.error(
                "%s: Repository [%s] has no baseurl set. metalink and mirrorlist are not supported",
                repourl, section
            )
            raise RuntimeError(f"{repourl}: Repository [{section}] has no baseurl set")

        yield RepoInfo(
            baseurl=baseurl, priority=priority
        )


def _extract_primary_location(repomd_xml: str):
    root = ET.fromstring(repomd_xml)

    ns = {
        'repo': 'http://linux.duke.edu/metadata/repo',
    }
    primary_location = root.find("./repo:data[@type='primary']/repo:location", ns)
    if primary_location is None:
        raise RuntimeError("Cannot find <data type='primary'/> in repomd.xml")

    return primary_location.attrib['href']


def _get_primary_metadata_url(session: requests.Session,
                              repo_info: RepoInfo,
                              baseurl: str):
    """Finds location of primary metadata xml.gz from repodata.xml"""
    repomd_url = baseurl + "repodata/repomd.xml"
    response = session.get(repomd_url, proxies=repo_info.get_proxies())
    response.raise_for_status()

    return baseurl + _extract_primary_location(response.text)


def _get_primary_metadata_path(session: requests.Session,
                               baseurl: Path):
    """Finds location of primary metadata xml.gz from repodata.xml"""
    with open(baseurl / "repodata/repomd.xml", "r") as f:
        repomd_xml = f.read()

    return baseurl / _extract_primary_location(repomd_xml)


class PackageLocator:
    def __init__(self, session: Optional[requests.Session] = None):
        if session is None:
            session = requests.Session()

        self.session = session
        self.repos: List[RepoInfo] = []

    def add_repo(self, baseurl: Union[str, Path], *,
                 proxy: Optional[str] = None,
                 priority: int = 99,
                 includepkgs: Optional[str] = None,
                 excludepkgs: Optional[str] = None):
        self.repos.append(RepoInfo(baseurl=baseurl, proxy=proxy, priority=priority))

    def add_remote_repofile(self, url):
        self.repos.extend(_extract_repo_info(self.session, url))

    def _find_package_from_repo_info(self,
                                     repo_info: RepoInfo,
                                     package: str,
                                     arch: Arch):
        """Parses the primary metadata .xml.gz for the repository to look for a package"""

        baseurl = repo_info.baseurl
        if isinstance(baseurl, str) and (baseurl.startswith("http:") or
                                         baseurl.startswith("https:")):
            baseurl = baseurl.replace("$basearch", arch.rpm)
            if not baseurl.endswith("/"):
                baseurl += "/"

            logger.info("Looking for %s in %s", package, baseurl)
            primary_url = _get_primary_metadata_url(self.session, repo_info, baseurl)
            primary_response = requests.get(
                primary_url, stream=True, proxies=repo_info.get_proxies())
            primary_response.raise_for_status()

            decompressed = gzip.GzipFile(fileobj=primary_response.raw, mode="r")
        else:
            logger.info("Looking for %s in %s", package, baseurl)
            primary_path = _get_primary_metadata_path(self.session, Path(baseurl))
            decompressed = gzip.GzipFile(filename=primary_path, mode="r")

        ns = {
            'common': 'http://linux.duke.edu/metadata/common',
        }

        for event, element in ET.iterparse(decompressed):
            if event == "end" and element.tag == "{http://linux.duke.edu/metadata/common}package":
                name = element.find("common:name", ns).text
                if name == package:
                    version_element = element.find("common:version", ns)
                    extended_version = ExtendedVersionInfo(
                        epoch=version_element.attrib["epoch"],
                        version=version_element.attrib["ver"],
                        release=version_element.attrib["rel"],
                        priority=repo_info.priority,
                    )

                    logger.info("Found %s", extended_version)
                    yield extended_version

                # Save most of the memory by clearing the contents
                element.clear()

    def find_latest_version(self, package: str, *,
                            session: Optional[requests.Session] = None,
                            arch: Arch) -> Optional[VersionInfo]:
        if session is None:
            session = requests.Session()

        candidates = [version
                      for repo in self.repos
                      for version in self._find_package_from_repo_info(repo, package, arch)]

        if candidates:
            best = max(candidates)
            return VersionInfo(best.epoch, best.version, best.release)
        else:
            return None
