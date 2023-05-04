"""_fetchrepodata: Map yum/dnf repo metadata to local lookup caches"""
import copy
from dataclasses import dataclass
import gzip
import logging
from math import ceil
import os
import re
from urllib.parse import urljoin

import click
from lxml import etree
import requests
from requests_toolbelt.downloadutils.tee import tee_to_file

from .config import config
from .util import display_dataset_name, parse_dataset_name


XDG_CACHE_HOME = (os.environ.get("XDG_CACHE_HOME")
                  or os.path.expanduser("~/.cache"))
CACHEDIR = os.path.join(XDG_CACHE_HOME, "fedmod")

log = logging.getLogger(__name__)


class MissingMetadata(Exception):
    """Reports failure to find the local metadata cache"""


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


@dataclass
class RepoPathsPair:
    arch: RepoPaths
    src: RepoPaths


def _define_repo(remote_repo_template, subst_dict, local_cache_name,
                 arch=None):

    # expand placeholders in URLs
    remote_repo_url = remote_repo_template
    for subst_key, subst_value in subst_dict.items():
        remote_repo_url = re.sub(fr"\${subst_key}", subst_value,
                                 remote_repo_url)

    local_arch_path = arch or 'source'
    local_cache_path = os.path.join(CACHEDIR, "repos", local_cache_name,
                                    local_arch_path)

    return RepoPaths(remote_repo_url, local_cache_path)


def _define_repos(arch_remote_prefix, src_remote_prefix, subst_dict,
                  local_cache_name, arch):
    return RepoPathsPair(arch=_define_repo(arch_remote_prefix, subst_dict,
                                           local_cache_name, arch),
                         src=_define_repo(src_remote_prefix, subst_dict,
                                          local_cache_name))


class DistroPaths(object):
    def __init__(self, release_name, arch):
        self.release_name = release_name
        self.release = config.releases[release_name]
        self.arch = arch

        subst_dict = {
            'basearch': arch or 'source',
        }

        dataset_regex = self.release.get('dataset-regex')
        if dataset_regex:
            match = re.match(dataset_regex, release_name)

            if not match:
                raise ValueError(
                    f"Couldn't parse release name {release_name!r} with"
                    f" dataset-regex {dataset_regex!r}")

            subst_dict.update(match.groupdict())

        self.repo_paths_by_name = {
            repo_name: _define_repos(repo_def['arch']['baseurl'],
                                     repo_def['source']['baseurl'],
                                     subst_dict,
                                     release_name + "--" + repo_name, arch)
            for repo_name, repo_def in
            config.releases[release_name]['repositories'].items()
        }


def _get_distro_paths(dataset_name):
    release_name, arch = parse_dataset_name(dataset_name)
    return DistroPaths(release_name, arch)


METADATA_SECTIONS = ("filelists", "primary")

_REPOMD_XML_NAMESPACE = {"rpm": "http://linux.duke.edu/metadata/repo"}


def _read_repomd_location(repomd_xml, section):
    location = repomd_xml.find(f"rpm:data[@type='{section}']/rpm:location",
                               _REPOMD_XML_NAMESPACE)
    if location is not None:
        return location.attrib["href"]
    return None


def _download_one_file(remote_url, filename):
    if os.path.exists(filename) and not filename.endswith((".xml", ".yaml")):
        print(f"  Skipping download; {filename} already exists")
        return
    response = requests.get(remote_url, stream=True)
    try:
        print(f"  Downloading {remote_url}")
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
    print(f"  Added {filename} to cache")


def _download_metadata_files(repo_paths):
    os.makedirs(repo_paths.local_metadata_path, exist_ok=True)

    repomd_url = urljoin(repo_paths.remote_metadata_url, "repomd.xml")
    print(f"Remote metadata: {repomd_url}")
    response = requests.get(repomd_url)
    if response.history:
        repomd_url = response.history[-1].headers['location']
        # avoid modifying external object
        repo_paths = copy.copy(repo_paths)
        repo_paths.remote_metadata_url = urljoin(repomd_url, ".")
        print(f" -> redirected: {repomd_url}")
    response.raise_for_status()

    repomd_filename = os.path.join(repo_paths.local_metadata_path,
                                   "repomd.xml")
    with open(repomd_filename, "wb") as f:
        f.write(response.content)
    print(f"  Cached metadata in {repomd_filename}")
    repomd_xml = etree.parse(repomd_filename, parser=None)

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
    repomd_xml = etree.parse(repomd_fname, parser=None)
    repo_relative_primary = _read_repomd_location(repomd_xml, "primary")
    assert repo_relative_primary is not None
    repo_primary_fname = os.path.join(metadata_dir, repo_relative_primary)

    package_dicts = []

    with gzip.open(repo_primary_fname, "rb") as primary_xml_gz:
        primary_xml = etree.fromstring(primary_xml_gz.read(), parser=None)

        # the default namespace makes accessing things really annoying
        XMLNS = f"{{{primary_xml.nsmap[None]}}}"

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


def download_repo_metadata(dataset_name):
    """Downloads the latest repo metadata"""

    paths = _get_distro_paths(dataset_name)
    for repo_pair in paths.repo_paths_by_name.values():
        for repo_definition in (repo_pair.arch, repo_pair.src):
            _download_metadata_files(repo_definition)


@dataclass
class LocalMetadataCache:
    dataset_name: str
    cache_dir: str
    repo_cache_paths: dict


def load_cached_repodata(dataset_name):
    paths = _get_distro_paths(dataset_name)

    display_name = display_dataset_name(dataset_name)
    arg = f" --dataset={display_name}" if display_name else ""

    # Check whether or not fetch-metadata has been run at all
    for repo_name, repo_paths in paths.repo_paths_by_name.items():
        metadata_dir = os.path.join(repo_paths.arch.local_cache_path)
        repomd_fname = os.path.join(metadata_dir, "repodata", "repomd.xml")

        if not os.path.exists(repomd_fname):
            raise MissingMetadata(f"{repomd_fname!r} does not exist. Run "
                                  f"`fedmod{arg} fetch-metadata`.")

    # Load the metadata
    return LocalMetadataCache(
        dataset_name=dataset_name,
        cache_dir=CACHEDIR,
        repo_cache_paths={
            n: (c.arch.local_cache_path, c.src.local_cache_path)
            for n, c in paths.repo_paths_by_name.items()
        }
    )
