import json
import os
import re
import sys
import shutil

import modulemd

from flatpak_module_tools.filesystem_builder import FilesystemBuilder
from flatpak_module_tools.flatpak_builder import FlatpakBuilder
from flatpak_module_tools.module_locator import ModuleLocator
from flatpak_module_tools.utils import check_call, die

def run(args):
    locator = ModuleLocator()
    if args.local_build_ids:
        for build_id in args.local_build_ids:
            locator.add_local_build(build_id)

    parts = args.module.split(':')
    if len(parts) != 2:
        die("module to build must be specified as NAME:STREAM")

    module_name = parts[0]
    module_stream = parts[1]

    with open(args.info) as f:
        info = json.load(f)

    build = locator.locate(module_name, module_stream)

    builddir = os.path.expanduser("~/modulebuild/flatpaks")
    workdir = os.path.join(builddir, "{}-{}-{}".format(build.name, build.stream, build.version))
    if os.path.exists(workdir):
        print >>sys.stderr, "Removing old output directory {}".format(workdir)
        shutil.rmtree(workdir)

    os.makedirs(workdir)
    print >>sys.stderr, "Writing results to {}".format(workdir)

    fs_builder = FilesystemBuilder(locator, build, info, workdir, runtime=args.runtime)
    fs_builder.build_filesystem()

    flatpak_builder = FlatpakBuilder(build, info, workdir, runtime=args.runtime)
    flatpak_builder.build_flatpak()


