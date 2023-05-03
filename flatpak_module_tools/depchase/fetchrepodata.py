"""_fetchrepodata: Map yum/dnf repo metadata to local lookup caches"""
from collections import defaultdict
import copy
import gzip
import json
import logging
import os
import re
from urllib.parse import urljoin
import yaml

from attr import attrib, attributes
import click
import gi
gi.require_version('Modulemd', '1.0')  # noqa: E402
from gi.repository import Modulemd
from lxml import etree
import requests
from requests_toolbelt.downloadutils.tee import tee_to_file

from .config import config
from .util import display_dataset_name, parse_dataset_name
from .util import yaml_safe_load, yaml_safe_load_all


XDG_CACHE_HOME = (os.environ.get("XDG_CACHE_HOME")
                  or os.path.expanduser("~/.cache"))
CACHEDIR = os.path.join(XDG_CACHE_HOME, "fedmod")

log = logging.getLogger(__name__)


class MissingMetadata(Exception):
    """Reports failure to find the local metadata cache"""


@attributes
class RepoPaths:
    remote_repo_url: str = attrib()
    local_cache_path: str = attrib()

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


@attributes
class RepoPathsPair:
    arch = attrib(type=RepoPaths)
    src = attrib(type=RepoPaths)


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

        localrepodatapath = self.release.get('_localrepodatapath')
        if localrepodatapath:
            # use the real local repo instead of anything we haven't downloaded
            def define_repos(arp, srp, sd, lcn, arch):
                repopaths = RepoPaths(remote_repo_url=f"file://{localrepodatapath}",
                                      local_cache_path="")
                repopaths.local_metadata_path = localrepodatapath
                return RepoPathsPair(arch=repopaths, src=repopaths)
        else:
            define_repos = _define_repos

        self.repo_paths_by_name = {
            repo_name: define_repos(repo_def['arch']['baseurl'],
                                    repo_def['source']['baseurl'],
                                    subst_dict,
                                    release_name + "--" + repo_name, arch)
            for repo_name, repo_def in
            config.releases[release_name]['repositories'].items()
        }


def _get_distro_paths(dataset_name):
    release_name, arch = parse_dataset_name(dataset_name)
    return DistroPaths(release_name, arch)


_MERGED_MODULEMDS_CACHE = 'merged-modulemds'
_MODULE_FORWARD_LOOKUP_CACHE = "module-contents"
_PROFILE_FORWARD_LOOKUP_CACHE = "module-profiles"
_MODULE_DEPENDENCIES_FORWARD_LOOKUP_CACHE = "module-dependencies"
_STREAM_DEFAULT_FORWARD_LOOKUP_CACHE = "stream-defaults"
_PROFILE_DEFAULT_FORWARD_LOOKUP_CACHE = "profile-defaults"
_SRPM_REVERSE_LOOKUP_CACHE = "srpm-to-module"
_RPM_REVERSE_LOOKUP_CACHE = "rpm-to-module"

_JSON_CACHES = [_MODULE_FORWARD_LOOKUP_CACHE,
                _PROFILE_FORWARD_LOOKUP_CACHE,
                _MODULE_DEPENDENCIES_FORWARD_LOOKUP_CACHE,
                _STREAM_DEFAULT_FORWARD_LOOKUP_CACHE,
                _PROFILE_DEFAULT_FORWARD_LOOKUP_CACHE,
                _SRPM_REVERSE_LOOKUP_CACHE,
                _RPM_REVERSE_LOOKUP_CACHE]

_YAML_CACHES = [_MERGED_MODULEMDS_CACHE]

_ALL_CACHES = _JSON_CACHES + _YAML_CACHES

METADATA_SECTIONS = ("filelists", "primary", "modules")

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
        try:
            expected_chunks = int(
                response.headers['content-length']) / chunksize
        except (KeyError, ValueError):
            expected_chunks = None
        downloader = tee_to_file(response, filename=filename,
                                 chunksize=chunksize)
        show_progress = click.progressbar(downloader, length=expected_chunks)
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
    repomd_xml = etree.parse(repomd_filename)

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


def _cache_fname(paths, cache_name):
    if cache_name in _JSON_CACHES:
        ext = 'json'
    elif cache_name in _YAML_CACHES:
        ext = 'yaml'
    else:
        raise RuntimeError(f"Unknown cache extension: {cache_name}")

    return os.path.join(CACHEDIR, f"{paths.release_name}-{cache_name}"
                                  f"-{paths.arch}-cache.{ext}")


def _write_cache(paths, cache_name, data, silent=False):
    """Write the given data to the nominated cache file"""
    cache_fname = _cache_fname(paths, cache_name)

    if cache_fname.endswith('.json'):
        write_fn = json.dump
    elif cache_fname.endswith('.yaml'):
        write_fn = yaml.dump_all
    else:
        raise RuntimeError(f"Unknown cache file extension: {cache_fname}")

    with open(cache_fname, "w") as cache_file:
        write_fn(data, cache_file)
    if not silent:
        print(f"  Added {cache_fname} to cache")


def _read_cache(paths, cache_name):
    """Read the parsed data from the nominated cache file"""
    cache_fname = _cache_fname(paths, cache_name)

    postprocess = None

    if cache_fname.endswith('.json'):
        read_fn = json.load
    elif cache_fname.endswith('.yaml'):
        read_fn = yaml_safe_load_all
        postprocess = list
    else:
        raise RuntimeError(f"Unknown cache file extension: {cache_fname}")

    with open(cache_fname, "r") as cache_file:
        data = read_fn(cache_file)
        if postprocess:
            data = postprocess(data)
        return data


def _read_packages(repo_paths):
    log.debug(f"_read_packages({repo_paths!r})")
    metadata_dir = os.path.join(repo_paths.local_cache_path)
    repomd_fname = os.path.join(metadata_dir, "repodata", "repomd.xml")
    repomd_xml = etree.parse(repomd_fname)
    repo_relative_primary = _read_repomd_location(repomd_xml, "primary")
    repo_primary_fname = os.path.join(metadata_dir, repo_relative_primary)

    package_dicts = []

    with gzip.open(repo_primary_fname, "rb") as primary_xml_gz:
        primary_xml = etree.fromstring(primary_xml_gz.read())

        # the default namespace makes accessing things really annoying
        XMLNS = f"{{{primary_xml.nsmap[None]}}}"

        for pkg in primary_xml.iter(XMLNS + 'package'):
            pkg_dct = {}
            ntag = pkg.find(XMLNS + 'name')
            if ntag is None:
                log.debug(f"Skipping package without name.")
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

def _read_modules(repo_paths):
    metadata_dir = os.path.join(repo_paths.local_cache_path)
    repomd_fname = os.path.join(metadata_dir, "repodata", "repomd.xml")
    repomd_xml = etree.parse(repomd_fname)
    repo_relative_modulemd = _read_repomd_location(repomd_xml, "modules")

    if not repo_relative_modulemd:
        # repository doesn't contain modules
        return

    repo_modulemd_fname = os.path.join(metadata_dir, repo_relative_modulemd)

    with gzip.open(repo_modulemd_fname, "rt") as modules_yaml_gz:
        modules_yaml = modules_yaml_gz.read()

    objects, _ = Modulemd.index_from_string(modules_yaml)
    return objects.values()  # a list of ImprovedModule objects


def merge_modules(module_set):
    """
    Given a list of ModuleStream objects, "merge" them by picking only the
    ModuleStream with highest version
    """
    modules = dict()

    for m in module_set:
        old = modules.get((m.props.name, m.props.stream))
        if not old or m.props.version > old.props.version:
            modules[(m.props.name, m.props.stream)] = m

    return modules.values()


def _write_lookup_caches(paths, silent=False):
    index_sets = [index_read
                  for index_read in (
                      _read_modules(repopaths.arch)
                      for repopaths in paths.repo_paths_by_name.values())
                  if index_read]

    modules = set()
    module_forward_lookup = {}
    srpm_reverse_lookup = defaultdict(list)
    rpm_reverse_lookup = defaultdict(list)
    # {'module-name:stream:version:context': [profiles]}}
    profile_forward_lookup = defaultdict(list)
    # {module-name: stream}
    stream_defaults_forward_lookup = {}
    # {module-name: {stream : [profiles]}}
    profile_defaults_forward_lookup = defaultdict(dict)
    # {'module-name:stream:version:context': ['module-name:stream']}
    module_dependencies_forward_lookup = defaultdict(list)

    for index_set in index_sets:
        for index in index_set:
            module_name = index.get_name()
            for nsvc, module in index.get_streams().items():
                profiles = module.get_profiles().keys()
                profile_forward_lookup[nsvc] = sorted(profiles)

                for dep in module.get_dependencies():
                    dset = set()
                    for m, s in dep.peek_requires().items():
                        dset.add(f"{m}:{','.join(s.get())}"
                                 if len(s.get()) else m)
                    module_dependencies_forward_lookup[nsvc] = sorted(dset)

            # What we think of as module is a ModuleStream for libmodulemd
            # We are ignoring context and using the stream with highest version
            for module in merge_modules(index.get_streams().values()):
                modules.add(module)
                artifacts = module.props.rpm_artifacts.get()
                module_forward_lookup[module_name] = list(set(artifacts))

                # module.props.components_rpm
                components = module.get_rpm_components()

                for srpmname in components:
                    srpm_reverse_lookup[srpmname].append(module_name)
                for rpmname in artifacts:
                    rpmprefix = rpmname.split(":", 1)[0].rsplit("-", 1)[0]
                    rpm_reverse_lookup[rpmprefix].append(module_name)

            defaults = index.get_defaults()
            if not defaults:
                continue

            stream_defaults_forward_lookup[module_name] = (
                defaults.peek_default_stream())
            # Default profiles for each stream in the module
            for s, pset in defaults.peek_profile_defaults().items():
                profile_defaults_forward_lookup[module_name][s] = pset.get()

    # merge modules, choose builds with highest version per stream
    merged_modulemds = [yaml_safe_load(m.dumps())
                        for m in merge_modules(modules)]

    # Cache the lookup tables as local JSON files
    if not silent:
        print("Caching lookup tables")
    _write_cache(paths, _MERGED_MODULEMDS_CACHE, merged_modulemds, silent)
    _write_cache(paths, _MODULE_FORWARD_LOOKUP_CACHE, module_forward_lookup, silent)
    _write_cache(paths, _PROFILE_FORWARD_LOOKUP_CACHE, profile_forward_lookup, silent)
    _write_cache(paths, _MODULE_DEPENDENCIES_FORWARD_LOOKUP_CACHE,
                 module_dependencies_forward_lookup, silent)
    _write_cache(paths, _STREAM_DEFAULT_FORWARD_LOOKUP_CACHE,
                 stream_defaults_forward_lookup, silent)
    _write_cache(paths, _PROFILE_DEFAULT_FORWARD_LOOKUP_CACHE,
                 profile_defaults_forward_lookup, silent)
    _write_cache(paths, _SRPM_REVERSE_LOOKUP_CACHE, srpm_reverse_lookup, silent)
    _write_cache(paths, _RPM_REVERSE_LOOKUP_CACHE, rpm_reverse_lookup, silent)


def download_repo_metadata(dataset_name):
    """Downloads the latest repo metadata"""

    paths = _get_distro_paths(dataset_name)
    for repo_pair in paths.repo_paths_by_name.values():
        for repo_definition in (repo_pair.arch, repo_pair.src):
            _download_metadata_files(repo_definition)
    _write_lookup_caches(paths)


@attributes
class LocalMetadataCache:
    dataset_name = attrib(type=str)
    cache_dir = attrib(type=str)
    merged_modulemds = attrib(type=list)
    srpm_to_modules = attrib(type=dict)
    rpm_to_modules = attrib(type=dict)
    module_to_packages = attrib(type=dict)
    module_to_profiles = attrib(type=dict)
    module_to_deps = attrib(type=dict)
    stream_defaults = attrib(type=dict)
    profile_defaults = attrib(type=dict)
    repo_cache_paths = attrib(type=dict)


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

    # Check whether or not fetch-metadata actually finished
    for cache in _ALL_CACHES:
        cache_fname = _cache_fname(paths, cache)
        if not os.path.exists(cache_fname):
            msg = (f"{cache_fname!r} does not exist. "
                   f"Try running `fedmod{arg} fetch-metadata` again.")
            raise MissingMetadata(msg)

    # Load the metadata
    return LocalMetadataCache(
        dataset_name=dataset_name,
        cache_dir=CACHEDIR,
        merged_modulemds=_read_cache(paths, _MERGED_MODULEMDS_CACHE),
        srpm_to_modules=_read_cache(paths, _SRPM_REVERSE_LOOKUP_CACHE),
        rpm_to_modules=_read_cache(paths, _RPM_REVERSE_LOOKUP_CACHE),
        module_to_packages=_read_cache(paths, _MODULE_FORWARD_LOOKUP_CACHE),
        module_to_profiles=_read_cache(paths, _PROFILE_FORWARD_LOOKUP_CACHE),
        module_to_deps=_read_cache(paths,
                                   _MODULE_DEPENDENCIES_FORWARD_LOOKUP_CACHE),
        stream_defaults=_read_cache(paths,
                                    _STREAM_DEFAULT_FORWARD_LOOKUP_CACHE),
        profile_defaults=_read_cache(paths,
                                     _PROFILE_DEFAULT_FORWARD_LOOKUP_CACHE),
        repo_cache_paths={
            n: (c.arch.local_cache_path, c.src.local_cache_path)
            for n, c in paths.repo_paths_by_name.items()
        }
    )
