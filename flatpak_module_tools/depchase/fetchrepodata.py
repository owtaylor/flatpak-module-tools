"""_fetchrepodata: Map yum/dnf repo metadata to local lookup caches"""
import copy
from enum import Enum
import logging
from math import ceil
import os
import time
from typing import List
from urllib.parse import urljoin

import click
from xml.etree import ElementTree as ET
import requests
from requests_toolbelt.downloadutils.tee import tee_to_file

from .repo_definition import RepoPaths
from ..utils import info, verbose


log = logging.getLogger(__name__)


class Refresh(Enum):
    MISSING = 1
    ALWAYS = 2
    AUTO = 3


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


def _download_metadata_files(repo_paths: RepoPaths, refresh):
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

    written_basenames = set(("repomd.xml",))
    for relative_href in files_to_fetch:
        absolute_href = urljoin(repo_paths.url, relative_href)
        basename = os.path.basename(relative_href)
        filename = os.path.join(repo_paths.local_metadata_path, basename)
        # This could be parallelised with concurrent.futures, but
        # probably not worth it (it makes the progress bars trickier)
        _download_one_file(absolute_href, filename)
        written_basenames.add(basename)

    # Prune any old metadata files automatically
    for f in os.listdir(repo_paths.local_metadata_path):
        if f not in written_basenames:
            os.unlink(os.path.join(repo_paths.local_metadata_path, f))


def download_repo_metadata(repo_definitions: List[RepoPaths], refresh: Refresh):
    """Downloads the latest repo metadata"""

    for repo_definition in repo_definitions:
        if not repo_definition.is_local:
            _download_metadata_files(repo_definition, refresh)
