import hawkey
import json
import os
import re
import sys
import shutil
import subprocess

import modulemd

from flatpak_module_tools.filesystem_builder import FilesystemBuilder
from flatpak_module_tools.flatpak_builder import FlatpakBuilder
from flatpak_module_tools.module_locator import ModuleLocator
from flatpak_module_tools.utils import check_call, die

import logging

def run(args):
    logging.getLogger().setLevel(logging.DEBUG)
    requests_log = logging.getLogger("requests.packages.urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True

    locator = ModuleLocator()
    if args.local_build_ids:
        for build_id in args.local_build_ids:
            locator.add_local_build(build_id)

    parts = args.module.split(':')
    if len(parts) != 2:
        die("module to build must be specified as NAME:STREAM")

    module_name = parts[0]
    module_stream = parts[1]

    build = locator.locate(module_name, module_stream)

    composedir = os.path.expanduser("~/modulebuild/composes")
    workdir = os.path.join(composedir, "{}-{}-{}".format(build.name, build.stream, build.version))
    if os.path.exists(workdir):
        print >>sys.stderr, "Removing old output directory {}".format(workdir)
        shutil.rmtree(workdir)

    os.makedirs(workdir)
    print >>sys.stderr, "Writing results to {}".format(workdir)

    found_packages = set()

    builds = locator.get_builds([(module_name, module_stream)])
    for build in builds.values():
        print build.name
        locator.ensure_downloaded(build)
        filters = set(build.mmd.filter.rpms)

        for f in os.listdir(build.path):
            print f
            if not f.endswith(".rpm"):
                continue
            nevra = hawkey.split_nevra(f[:-4])
            if nevra.arch == 'src':
                continue
            if nevra.name in found_packages:
                continue
            if nevra.name in filters:
                continue

            found_packages.add(nevra.name)
            os.link(os.path.join(build.path, f), os.path.join(workdir, f))

    subprocess.check_call(['createrepo_c', workdir])
