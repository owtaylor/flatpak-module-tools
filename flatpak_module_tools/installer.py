import koji
import json
import os
import re
import shutil
import subprocess
import tempfile

from six.moves.urllib.parse import urlparse

import click
import requests

from .utils import check_call, header, die

def _download_url(url, outdir):
    parts = urlparse(url)
    basename = os.path.basename(parts.path)
    if basename == '' or basename == 'oci':
        basename = "archive"

    path = os.path.join(outdir, basename)

    try:
        r = requests.get(url, stream=True)
        r.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        die("Download failed for {}: {}".format(url, e))
    except requests.exceptions.HTTPError as e:
        die("Download failed: {}".format(e))

    total_length = int(r.headers.get('content-length'))

    with open(path, 'wb') as f:
        with click.progressbar(length=total_length, label="Downloading image") as pb:
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    pb.update(len(chunk))
                    f.write(chunk)

    return path


def _download_koji_name_stream(profile, koji_name_stream, outdir):
    parts = koji_name_stream.split(':')
    if len(parts) == 1:
        name = parts[0]
        stream = None
    elif len(parts) == 2:
        name = parts[0]
        stream = parts[1]
    else:
        die("Koji download should be NAME or NAME:STREAM, not {}".format(koji_name_stream))

    options = koji.read_config(profile_name=profile.koji_profile, user_config=profile.koji_config)
    session_opts = koji.grab_session_options(options)
    session = koji.ClientSession(options['server'], session_opts)

    package_id = session.getPackageID(name)
    if package_id is None:
        die("Cannot find koji ID for {}".format(koji_name_stream))

    kwargs = {
        'type': 'image',
        'state': koji.BUILD_STATES['COMPLETE'],
        'queryOpts': { 'order': '-completion_ts'}
    }

    if stream is None:
        kwargs['queryOpts']['limit'] = 1

    builds = session.listBuilds(package_id, **kwargs)
    if stream is not None:
        builds = [b for b in builds if b['version'] == stream]

    if len(builds) == 0:
        die("Cannot find any Flatpak builds for {}".format(koji_name_stream))

    build = builds[0]
    archives = session.listArchives(build['build_id'])

    pathinfo = koji.PathInfo(topdir=options['topurl'])
    url = '/'.join((pathinfo.imagebuild(build), archives[0]['filename']))

    return _download_url(url, outdir)


class Installer(object):
    def __init__(self, profile):
        self.profile = profile
        self.source_koji_name_stream = None
        self.source_path = None
        self.source_url = None

        data_home = os.environ.get('XDG_DATA_HOME',
                                   os.path.expanduser('~/.local/share'))
        self.repodir = os.path.join(data_home, 'flatpak-module-tools', 'repo')

    def set_source_path(self, path):
        self.source_path = path

    def set_source_url(self, url):
        self.source_url = url

    def set_source_koji_name_stream(self, koji_name_stream):
        self.source_koji_name_stream = koji_name_stream

    def ensure_remote(self):
        if not os.path.exists(self.repodir):
            parent = os.path.dirname(self.repodir)
            if not os.path.exists(parent):
                os.makedirs(parent)

            check_call(['ostree', 'init', '--mode=archive-z2', '--repo', self.repodir])
            check_call(['flatpak', 'build-update-repo', self.repodir])

        output = subprocess.check_output(['flatpak', 'remotes', '--user'], encoding="UTF-8")
        if not re.search('^flatpak-module-tools\s', output, re.MULTILINE):
            check_call(['flatpak', 'remote-add',
                        '--user', '--no-gpg-verify',
                        'flatpak-module-tools', self.repodir])

    def install(self):
        header('INSTALLING')

        self.ensure_remote()

        try:
            workdir = tempfile.mkdtemp()
            ocidir = os.path.join(workdir, 'oci')
            os.mkdir(ocidir)

            if self.source_koji_name_stream:
                path = _download_koji_name_stream(self.profile, self.source_koji_name_stream, workdir)
            elif self.source_url:
                path = _download_url(self.source_url, workdir)
            elif self.source_path:
                path = os.path.abspath(self.source_path)
            else:
                raise RuntimeError("Installation source not set")

            check_call(['tar', 'xfa', path], cwd=ocidir)

            with open(os.path.join(ocidir, 'index.json')) as f:
                index_json = json.load(f)

            ref = index_json['manifests'][0]['annotations']['org.flatpak.ref']

            check_call(['flatpak', 'build-import-bundle',
                        '--update-appstream', '--oci',
                        self.repodir, ocidir])
        finally:
            shutil.rmtree(workdir)

        parts = ref.split('/')
        shortref = parts[0] + '/' + parts[1]

        try:
            with open(os.devnull, 'w') as devnull:
                old_origin = subprocess.check_output(['flatpak', 'info', '--user', '-o', shortref],
                                                     stderr=devnull, encoding="UTF-8").strip()
        except subprocess.CalledProcessError:
            old_origin = None

        if old_origin == 'flatpak-module-tools':
            check_call(['flatpak', 'update', '-y', '--user', ref])
        else:
            check_call(['flatpak', 'install', '-y', '--user', '--reinstall', 'flatpak-module-tools', ref])
