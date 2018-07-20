import json
import os
import re
import shutil
import subprocess
import tempfile

from six.moves.urllib.parse import urlparse

import click
import requests

from .utils import check_call, header

def _download_url(url, outdir):
    parts = urlparse(url)
    basename = os.path.basename(parts.path)
    if basename == '' or basename == 'oci':
        basename = "archive"

    path = os.path.join(outdir, basename)

    r = requests.get(url, stream=True)
    total_length = int(r.headers.get('content-length'))

    with open(path, 'wb') as f:
        with click.progressbar(length=total_length, label="Downloading {}".format(basename)) as pb:
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    pb.update(len(chunk))
                    f.write(chunk)

    return path

class Installer(object):
    def __init__(self, path):
        self.path = path

        data_home = os.environ.get('XDG_DATA_HOME',
                                   os.path.expanduser('~/.local/share'))
        self.repodir = os.path.join(data_home, 'flatpak-module-tools', 'repo')

    def ensure_remote(self):
        if not os.path.exists(self.repodir):
            parent = os.path.dirname(self.repodir)
            if not os.path.exists(parent):
                os.makedirs(parent)

            check_call(['ostree', 'init', '--mode=archive-z2', '--repo', self.repodir])

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

            if self.path.startswith("http://") or self.path.startswith("https://"):
                path = _download_url(self.path, workdir)
            else:
                path = os.path.abspath(self.path)

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
            check_call(['flatpak', 'update', '--user', ref])
        else:
            if old_origin is not None:
                check_call(['flatpak', 'uninstall', '--user', shortref])
            check_call(['flatpak', 'install', '--user', 'flatpak-module-tools', ref])
