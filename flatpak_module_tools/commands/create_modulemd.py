#!/usr/bin/python
from collections import OrderedDict
import os
import pkg_resources
import sys
import yaml

from flatpak_module_tools.dep_expander import DepExpander
from flatpak_module_tools.module_locator import ModuleLocator
from flatpak_module_tools.package_info import PackageInfo
from flatpak_module_tools.yaml_utils import ordered_load, ordered_dump
from flatpak_module_tools.utils import die

def is_modular(reponame):
    return reponame != 'f26' and reponame != 'f26-updates' and reponame != 'f26-updates-testing'

def run(args):
    packages = None

    if args.from_package:
        with pkg_resources.resource_stream('flatpak_module_tools', 'app.template.yaml') as f:
            template = ordered_load(f)
        if args.template is not None:
            die("If --from-package is specified, then --template cannot be specified")

        if args.package_list is None:
            packages = {
                'runtime-roots': [args.from_package]
            }

        requires_modules = [('flatpak-runtime', 'f26')]
        buildrequires_modules = [('flatpak-runtime', 'f26'), ('common-build-dependencies', 'f26')]
    else:
        if args.template is None:
            die("Either --from-package or --template must be specified")
        with open(args.template) as f:
            template = ordered_load(f)

        requires_modules = [('base-runtime', 'f26'), ('shared-userspace', 'f26')]
        buildrequires_modules = [('bootstrap', 'f26')]

    if packages is None:
        if args.package_list is None:
            die("Either --from-package or --package-list must be specified")

        with open(args.package_list) as f:
            packages = yaml.safe_load(f)

    locator = ModuleLocator()
    if args.local_build_ids:
        for build_id in args.local_build_ids:
            locator.add_local_build(build_id)

    pkgs = PackageInfo(locator, requires_modules, buildrequires_modules)

    expander = DepExpander(pkgs)
    bin, source = expander.add_binaries(packages['runtime-roots'])
    #print "All packages to install:"
    #print sorted(source)
    #print

    if args.from_package is not None:
        profile = 'default'
    else:
        profile = 'runtime'

    template['data']['profiles'][profile]['rpms'] = sorted(bin | set(packages.get('extra-runtime-packages', ())))

    if args.dependency_tree:
        expander.dump_dependency_tree(bin, args.dependency_tree)

    if 'api' in template['data']:
        api = set(n for n in bin if not is_modular(expander.binary[n].reponame))
        api.update(packages.get('extra-api', ()))
        template['data']['api']['rpms'] = sorted(api)

    print >>sys.stderr, "Adding extra dependencies needed at application build-time"
    extra_builddep_bin, extra_builddep_source = expander.add_binaries(packages.get('builddep-roots', ()))

    bin.update(extra_builddep_bin)
    source.update(extra_builddep_source)

    binary_to_build = set(n for n in bin if not is_modular(expander.binary[n].reponame))
    to_build = set(expander.binary_to_source(n) for n in binary_to_build)
    orphans = expander.find_orphans(to_build, is_modular)

    def get_builddeps(name):
        exp = DepExpander(pkgs)
        for p in packages.get('build-order-ignore', ()):
            exp.binary[p] = None
        _, source = exp.add_builddeps([name], include_source=True)
        return source

    def get_batch_map(names):
        print >>sys.stderr, "Building dependency map"

        depended_on_by = {}
        for name in sorted(names):
            print >>sys.stderr, '   ', name
            builddeps = get_builddeps(name)
            print sorted([n for n in builddeps if n in names])
            if name in builddeps:
                print >>sys.stderr, "Removing circular build dependency of %s on %s" % (name, name)
                builddeps.remove(name)
            for n in builddeps:
                if n in names:
                    if not n in depended_on_by:
                        depended_on_by[n] = set()
                    depended_on_by[n].add(name)

        for name in sorted(depended_on_by):
            print '%s:\ %s' % (name, sorted(depended_on_by[name]))

        batch_map = {}
        batch = 0
        while len(batch_map)  < len(names):
            found_one = False
            old_map = batch_map.copy()
            for name in names:
                if name in old_map:
                    continue

                must_build_earlier = False
                for d in depended_on_by.get(name, ()):
                    if not d in old_map:
                        must_build_earlier = True
                if not must_build_earlier:
                    batch_map[name] = batch
                    found_one = True

            if not found_one:
                print >>sys.stderr, "Can't make progress"
                sys.exit(1)
            batch -= 1

        for name in batch_map:
            batch_map[name] -= batch

        return batch_map

    batch_map = get_batch_map(to_build)

    components = OrderedDict()
    def add_component(name, rationale, ref='f26', repository=None, buildorder=None):
        components[name] = component = OrderedDict()
        component['rationale'] = rationale
        if ref is not None:
            component['ref'] = ref
        if repository is not None:
            component['repository'] = repository
        if buildorder is not None:
            component['buildorder'] = buildorder

    extra_components = packages.get('extra-components', {})

    for name in sorted(to_build | set(extra_components.keys())):
        if name in extra_components:
            component = extra_components[name]
            add_component(name, component['rationale'],
                          ref=component.get('ref', None),
                          repository=component.get('repository', None),
                          buildorder=component.get('buildorder', None))
        else:
            requiring_package = expander.required_by.get(expander.source_to_binary[name], None)
            if name in orphans:
                rationale = 'Runtime requirement of %s (*)' % requiring_package
            elif requiring_package:
                rationale = 'Runtime requirement of %s' % requiring_package
            else:
                rationale = ''
            add_component(name, rationale, ref='f26', buildorder=batch_map[name])

    for package, to_override in packages.get('overrides', {}).items():
        for k, v in to_override.items():
            components[package][k] = v

    template['data']['components']['rpms'] = components

    #print "Packages to build and install:"
    #for name in sorted(to_build):
    #    requiring_package = expander.required_by.get(expander.source_to_binary[name], None)
    #    if name in orphans:
    #        print '*', name, '(required by ' + requiring_package + ')'
    #    else:
    #        print ' ', name

    expander.include_builddeps = True
    builddep_bin, _ = expander.add_builddeps(to_build, include_source=False)

    builddep_needed = set(n for n in builddep_bin if not is_modular(expander.binary[n].reponame))

    needed_sources = set()
    for name in sorted(builddep_needed):
        requiring_package = expander.required_by.get(name, None)
        if requiring_package is not None:
            print ' ', name, '(required by ' + requiring_package + ')'
        else:
            print ' ', name

    with open(args.out, 'w') as f:
        f.write(ordered_dump(template, default_flow_style=False, encoding="utf-8"))
