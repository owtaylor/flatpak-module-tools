# When building a Flatpak, the version label will be based on the RPM version
# of the main RPM installed in the package. *But* we need the VERSION label
# set *before* building the container to support things like the bump_release
# plugin. So, what this code does is download and parse metadata from a
# repourl (which might have multiple repositories in it) to find that
# main version.
#
# Not supported in parsing repository URLs
#
#  mirrorlist
#  metalink
#  subsituting $releasever
#
# mirrorlist and metalink support wouldn't be that hard, but $releasever
# substitution is impossible, since that would depend on the DNF configuration
# inside the container. excludepkgs/includepkgs are supported despite being
# tricky.

from dataclasses import dataclass
from configparser import RawConfigParser
import fnmatch
from functools import cached_property, total_ordering
import gzip
import logging
import re
from typing import Callable, List, Optional, Union
import xml.etree.ElementTree as ET

import requests

from .rpm_utils import VersionInfo
from .utils import Arch

logger = logging.getLogger(__name__)


@total_ordering
class ExtendedVersionInfo(VersionInfo):
    """Represents a particular binary package version"""
    name: str
    arch: str
    priority: int

    def __init__(self, *, name: str,
                 epoch: Union[str, int, None], version: str, release: str,
                 arch: str, priority: int):
        super().__init__(epoch, version, release)
        self.name = name
        self.arch = arch
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


# We need to handle includepkgs= in repository definitions in case someone
# does something like includepkgs=*-*-*.fc38app to make the repository only
# include packages with the .fc38app dist tag.
#
# NEVRA means "NAME-EPOCH:VERSION-RELEASE.ARCH", but matcher is more complicated
# than just matching a glob against that string.
#
# The DNF algorithm is that for each of a number of forms (N-E:V-R.A, N-V-R, ...)
# if the glob matches that form, match component-wise against the elements
# in the form. And then *also* match the entire glob against the NEVRA string.

@dataclass
class NevraForm:
    if_glob_matches: str
    version_info_subset: Callable[[ExtendedVersionInfo], str]

    def get_matchers(self, glob: str):
        m = re.match(self.if_glob_matches + r'$', glob)
        if m:
            # $ is just a character that is unlikely to be in an a NEVRA
            pattern = "$".join(m.groups())
            re_pattern = re.compile(fnmatch.translate(pattern))

            def matcher(evi: ExtendedVersionInfo):
                return re_pattern.match(self.version_info_subset(evi)) is not None
            yield matcher


# Patterns for the different parts of a NEVRA
N, E, V, R, A = r'([^:]+)', r'([^:-]+)', r'([^:-]+)', r'([^:-]+)', r'([^:.-]+)'


NEVRA_FORMS = [
    NevraForm(fr'{N}-{E}:{V}-{R}\.{A}',
              lambda vi: f"{vi.name}${vi.epoch}${vi.version}${vi.release}${vi.arch}"),
    NevraForm(fr'{N}-{V}-{R}\.{A}',
              lambda vi: f"{vi.name}${vi.version}${vi.release}${vi.arch}"),
    NevraForm(fr'{N}\.{A}',
              lambda vi: f"{vi.name}${vi.arch}"),
    NevraForm(fr'{N}',
              lambda vi: f"{vi.name}"),
    NevraForm(fr'{N}-{E}:{V}-{R}',
              lambda vi: f"{vi.name}${vi.epoch}${vi.version}${vi.release}"),
    NevraForm(fr'{N}-{V}-{R}',
              lambda vi: f"{vi.name}${vi.version}${vi.release}"),
    NevraForm(fr'{N}-{E}:{V}',
              lambda vi: f"{vi.name}${vi.epoch}${vi.version}"),
    NevraForm(fr'{N}-{E}',
              lambda vi: f"{vi.name}${vi.version}"),
    NevraForm(r'(.*)',
              lambda vi: f"{vi.name}-{vi.epoch}:{vi.version}-{vi.release}.{vi.arch}"),
]


def compile_nevr_globlist(globlist):
    """Turns a comma separated list of globs into a decision function"""
    matchers = [
        matcher
        for glob in re.split(r' *, *', globlist.strip())
        for form in NEVRA_FORMS
        for matcher in form.get_matchers(glob)
    ]

    def matches(vi: ExtendedVersionInfo):
        return any(m(vi) for m in matchers)

    return matches


@dataclass(frozen=True)
class RepoInfo:
    """Represents the parts of a yum/dnf repository definition we support"""
    baseurl: str
    proxy: Optional[str] = None
    priority: int = 99

    includepkgs: Optional[str] = None
    excludepkgs: Optional[str] = None

    @cached_property
    def includepkgs_fn(self) -> Callable[[ExtendedVersionInfo], bool]:
        if self.includepkgs:
            return compile_nevr_globlist(self.includepkgs)
        else:
            return lambda evi: True

    @cached_property
    def excludepkgs_fn(self) -> Callable[[ExtendedVersionInfo], bool]:
        if self.excludepkgs:
            return compile_nevr_globlist(self.excludepkgs)
        else:
            return lambda evi: False

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
        includepkgs = cp.get(section, "includepkgs", fallback=None)
        excludepkgs = cp.get(section, "excludepkgs", fallback=None)

        if not enabled:
            continue

        if baseurl is None:
            logger.error(
                "%s: Repository [%s] has no baseurl set. metalink and mirrorlist are not supported",
                repourl, section
            )
            raise RuntimeError(f"{repourl}: Repository [{section}] has no baseurl set")

        yield RepoInfo(
            baseurl=baseurl, priority=priority, includepkgs=includepkgs, excludepkgs=excludepkgs
        )


def _get_primary_metadata_url(session: requests.Session,
                              repo_info: RepoInfo,
                              baseurl: str):
    """Finds location of primary metadata xml.gz from repodata.xml"""
    repomd_url = baseurl + "repodata/repomd.xml"
    response = session.get(repomd_url, proxies=repo_info.get_proxies())
    response.raise_for_status()

    root = ET.fromstring(response.text)

    ns = {
        'repo': 'http://linux.duke.edu/metadata/repo',
    }
    primary_location = root.find("./repo:data[@type='primary']/repo:location", ns)
    if primary_location is None:
        raise RuntimeError("Cannot find <data type='primary'/> in repomd.xml")

    return baseurl + primary_location.attrib['href']


class PackageLocator:
    def __init__(self, session: Optional[requests.Session] = None):
        if session is None:
            session = requests.Session()

        self.session = session
        self.repos: List[RepoInfo] = []

    def add_repo(self, baseurl: str, *,
                 proxy: Optional[str] = None,
                 priority: int = 99,
                 includepkgs: Optional[str] = None,
                 excludepkgs: Optional[str] = None):
        self.repos.append(RepoInfo(baseurl=baseurl, proxy=proxy, priority=priority,
                                   includepkgs=includepkgs, excludepkgs=excludepkgs))

    def add_remote_repofile(self, url):
        self.repos.extend(_extract_repo_info(self.session, url))

    def _find_package_from_repo_info(self,
                                     repo_info: RepoInfo,
                                     package: str,
                                     arch: Arch):
        """Parses the primary metadata .xml.gz for the repository to look for a package"""
        baseurl = repo_info.baseurl.replace("$basearch", arch.rpm)
        if not baseurl.endswith("/"):
            baseurl += "/"

        logger.info("Looking for %s in %s", package, baseurl)

        primary_url = _get_primary_metadata_url(self.session, repo_info, baseurl)
        primary_response = requests.get(primary_url, stream=True, proxies=repo_info.get_proxies())
        primary_response.raise_for_status()

        ns = {
            'common': 'http://linux.duke.edu/metadata/common',
        }

        decompressed = gzip.GzipFile(fileobj=primary_response.raw, mode="r")
        for event, element in ET.iterparse(decompressed):
            if event == "end" and element.tag == "{http://linux.duke.edu/metadata/common}package":
                name = element.find("common:name", ns).text
                if name == package:
                    package_arch = element.find("common:arch", ns).text
                    version_element = element.find("common:version", ns)
                    extended_version = ExtendedVersionInfo(
                        name=name,
                        epoch=version_element.attrib["epoch"],
                        version=version_element.attrib["ver"],
                        release=version_element.attrib["rel"],
                        arch=package_arch,
                        priority=repo_info.priority,
                    )

                    if not repo_info.includepkgs_fn(extended_version):
                        logger.info("Ignoring %s because not in includepkgs", extended_version)
                        continue
                    if repo_info.excludepkgs_fn(extended_version):
                        logger.info("Ignoring %s because in excludepkgs", extended_version)
                        continue

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
