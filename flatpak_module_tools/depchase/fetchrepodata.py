"""_fetchrepodata: Map yum/dnf repo metadata to local lookup caches"""
import copy
from dataclasses import dataclass
from enum import Enum
import gzip
import logging
from math import ceil
import os
import time
from typing import Dict
from urllib.parse import urljoin

import click
from xml.etree import ElementTree as ET
import koji
import requests
from requests_toolbelt.downloadutils.tee import tee_to_file

from ..config import get_profile
from ..utils import Arch, info, verbose


XDG_CACHE_HOME = (os.environ.get("XDG_CACHE_HOME")
                  or os.path.expanduser("~/.cache"))
CACHEDIR = os.path.join(XDG_CACHE_HOME, "flatpak-module-tools")

log = logging.getLogger(__name__)


class Refresh(Enum):
    MISSING = 1
    ALWAYS = 2
    AUTO = 3


@dataclass
class RepoPaths:
    remote_repo_url: str
    local_cache_path: str

    @property
    def remote_metadata_url(self):
        return urljoin(self.remote_repo_url, 'repodata/')

    @remote_metadata_url.setter
    def remote_metadata_url(self, url):
        if not url.endswith('/repodata/'):
            raise ValueError("'url' must end with '/repodata/'")

        self.remote_repo_url = url[:-9]  # with 'repodata/' stripped

    @property
    def local_metadata_path(self):
        return os.path.join(self.local_cache_path, 'repodata/')

    @local_metadata_path.setter
    def local_metadata_path(self, path):
        self.local_cache_path = path[:-9]  # with 'repodata/' stripped


def _define_repo(remote_repo_url: str, local_cache_name: str, arch: Arch):
    local_cache_path = os.path.join(CACHEDIR, "repos", local_cache_name, arch.rpm)

    return RepoPaths(remote_repo_url, local_cache_path)


class DistroPaths:
    def __init__(self, tag: str, arch: Arch):
        profile = get_profile()

        pathinfo = koji.PathInfo(topdir=profile.koji_options['topurl'])
        baseurl = pathinfo.repo("latest", tag) + "/" + arch.rpm + "/"

        self.repo_paths_by_name = {
            tag: _define_repo(baseurl, tag, arch)
        }


def _get_distro_paths(release, arch):
    return DistroPaths(release, arch)


METADATA_SECTIONS = ("filelists", "primary")

_REPOMD_XML_NAMESPACE = {"rpm": "http://linux.duke.edu/metadata/repo"}


def _read_repomd_location(repomd_xml: ET.ElementTree, section):
    location = repomd_xml.find(f"rpm:data[@type='{section}']/rpm:location",
                               _REPOMD_XML_NAMESPACE)
    if location is not None:
        return location.attrib["href"]
    return None


def _download_one_file(remote_url, filename):
    if os.path.exists(filename) and not filename.endswith((".xml", ".yaml")):
        verbose(f"  Skipping download; {filename} already exists")
        return
    response = requests.get(remote_url, stream=True)
    try:
        info(f"  Downloading {remote_url}")
        chunksize = 65536
        content_length = response.headers['content-length']
        assert content_length is not None
        expected_chunks = int(content_length) / chunksize
        downloader = tee_to_file(response, filename=filename,
                                 chunksize=chunksize)
        show_progress = click.progressbar(downloader, length=ceil(expected_chunks))
        with show_progress:
            for chunk in show_progress:
                pass
    finally:
        response.close()
    info(f"  Added {filename} to cache")


def _download_metadata_files(repo_paths, refresh):
    os.makedirs(repo_paths.local_metadata_path, exist_ok=True)

    repomd_filename = os.path.join(repo_paths.local_metadata_path,
                                   "repomd.xml")

    need_refresh = True
    try:
        st = os.stat(repomd_filename)
    except FileNotFoundError:
        st = None

    if st is not None:
        if refresh == Refresh.MISSING:
            need_refresh = False
        elif refresh == Refresh.AUTO:
            if time.time() < st.st_mtime + 30 * 60:
                need_refresh = False

    if need_refresh:
        repomd_url = urljoin(repo_paths.remote_metadata_url, "repomd.xml")

        info(f"Remote metadata: {repomd_url}")
        response = requests.get(repomd_url)
        if response.history:
            repomd_url = response.history[-1].headers['location']
            # avoid modifying external object
            repo_paths = copy.copy(repo_paths)
            repo_paths.remote_metadata_url = urljoin(repomd_url, ".")
            info(f" -> redirected: {repomd_url}")
        response.raise_for_status()

        with open(repomd_filename, "wb") as f:
            f.write(response.content)
        info(f"  Cached metadata in {repomd_filename}")

    repomd_xml = ET.parse(repomd_filename, parser=None)

    files_to_fetch = set()
    for section in METADATA_SECTIONS:
        relative_href = _read_repomd_location(repomd_xml, section)
        if relative_href is not None:
            files_to_fetch.add(relative_href)

    predownload = set(os.listdir(repo_paths.local_cache_path))
    for relative_href in files_to_fetch:
        absolute_href = urljoin(repo_paths.remote_repo_url, relative_href)
        filename = os.path.join(repo_paths.local_cache_path, relative_href)
        # This could be parallelised with concurrent.futures, but
        # probably not worth it (it makes the progress bars trickier)
        _download_one_file(absolute_href, filename)
    postdownload = set(os.listdir(repo_paths.local_cache_path))

    # Prune any old metadata files automatically
    if len(postdownload) >= (len(predownload) + len(METADATA_SECTIONS)):
        # TODO: Actually prune old metadata files
        pass


def _read_packages(repo_paths):
    log.debug(f"_read_packages({repo_paths!r})")
    metadata_dir = os.path.join(repo_paths.local_cache_path)
    repomd_fname = os.path.join(metadata_dir, "repodata", "repomd.xml")
    repomd_xml = ET.parse(repomd_fname, parser=None)
    repo_relative_primary = _read_repomd_location(repomd_xml, "primary")
    assert repo_relative_primary is not None
    repo_primary_fname = os.path.join(metadata_dir, repo_relative_primary)

    package_dicts = []

    with gzip.open(repo_primary_fname, "rb") as primary_xml_gz:
        primary_xml = ET.fromstring(primary_xml_gz.read(), parser=None)

        # the default namespace makes accessing things annoying
        XMLNS = "{http://linux.duke.edu/metadata/common}"

        for pkg in primary_xml.iter(XMLNS + 'package'):
            pkg_dct = {}
            ntag = pkg.find(XMLNS + 'name')
            if ntag is None:
                log.debug("Skipping package without name.")
                continue
            pkg_dct['name'] = name = ntag.text

            if pkg.attrib['type'] != 'rpm':
                # skip non-RPM content
                log.debug(f"Skipping non-RPM package {name!r}.")
                continue

            vtag = pkg.find(XMLNS + 'version')
            if vtag is None:
                log.debug(f"Skipping package without version tag {name!r}.")
                continue

            pkg_dct['epoch'] = epoch = vtag.attrib.get('epoch', '0')
            pkg_dct['ver'] = ver = vtag.attrib.get('ver')
            pkg_dct['rel'] = rel = vtag.attrib.get('rel')

            if not ver or not rel:
                log.debug(f"Skipping package without proper version info {name!r}.")
                continue

            atag = pkg.find(XMLNS + 'arch')
            if atag is None:
                log.debug(f"Skipping package without architecture {name!r}.")
                continue
            pkg_dct['arch'] = arch = atag.text

            pkg_dct['nevra'] = nevra = f'{name}-{epoch}:{ver}-{rel}.{arch}'

            stag = pkg.find(XMLNS + 'summary')
            if stag is not None:
                pkg_dct['summary'] = stag.text

            dtag = pkg.find(XMLNS + 'description')
            if dtag is not None:
                pkg_dct['description'] = dtag.text

            log.debug(f"Found {nevra}.")

            package_dicts.append(pkg_dct)

    return package_dicts


def download_repo_metadata(tag, arch, refresh: Refresh):
    """Downloads the latest repo metadata"""

    paths = _get_distro_paths(tag, arch)
    for repo_definition in paths.repo_paths_by_name.values():
        _download_metadata_files(repo_definition, refresh)


def get_metadata_location(tag, arch):
    paths = _get_distro_paths(tag, arch)
    return paths.repo_paths_by_name[tag].local_metadata_path


@dataclass
class LocalMetadataCache:
    cache_dir: str
    repo_cache_paths: Dict[str, str]


def load_cached_repodata(tag: str, arch: Arch):
    paths = _get_distro_paths(tag, arch)

    # Sanity-check that all the repos we expect exist
    for repo_name, repo_path in paths.repo_paths_by_name.items():
        metadata_dir = os.path.join(repo_path.local_cache_path)
        repomd_fname = os.path.join(metadata_dir, "repodata", "repomd.xml")

        if not os.path.exists(repomd_fname):
            raise RuntimeError(f"Cached repodata for {repo_name} not found at {repo_path}")

    # Load the metadata
    return LocalMetadataCache(
        cache_dir=CACHEDIR,
        repo_cache_paths={
            n: c.local_cache_path
            for n, c in paths.repo_paths_by_name.items()
        }
    )
