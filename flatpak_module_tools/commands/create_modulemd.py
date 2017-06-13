#!/usr/bin/python
from collections import OrderedDict
import os
import sys
import yaml

from flatpak_module_tools.dep_expander import DepExpander
from flatpak_module_tools.package_info import PackageInfo
from flatpak_module_tools.yaml_utils import ordered_load, ordered_dump

def is_modular(reponame):
    return reponame != 'f26' and reponame != 'f26-updates' and reponame != 'f26-updates-testing'

def run(args):
    class Config(object):
        pass

    conf = Config()
    conf.pdc_url = 'http://pdc.fedoraproject.org/rest_api/v1'
    conf.pdc_insecure = False
    conf.pdc_develop = True

    conf.koji_config = '/etc/module-build-service/koji.conf'
    conf.koji_profile = 'koji'

    conf.cache_dir = os.path.expanduser('~/modulebuild/cache')

    pkgs = PackageInfo(conf)

    with open(args.template) as f:
        template = ordered_load(f)

    with open(args.package_list) as f:
        packages = yaml.load(f)

    expander = DepExpander(pkgs)
    bin, source = expander.add_binaries(packages['runtime-roots'])
    #print "All packages to install:"
    #print sorted(source)
    #print

    template['data']['profiles']['runtime']['rpms'] = sorted(bin | set(packages['extra-runtime-packages']))

    if args.dependency_tree:
        expander.dump_dependency_tree(bin, args.dependency_tree)

    api = set(n for n in bin if not is_modular(expander.binary[n].reponame))
    api.update(packages['extra-api'])
    template['data']['api']['rpms'] = sorted(api)

    print >>sys.stderr, "Adding extra dependencies needed at application build-time"
    extra_builddep_bin, extra_builddep_source = expander.add_binaries(packages['builddep-roots'])

    bin.update(extra_builddep_bin)
    source.update(extra_builddep_source)

    binary_to_build = set(n for n in bin if not is_modular(expander.binary[n].reponame))
    to_build = set(expander.binary_to_source(n) for n in binary_to_build)
    orphans = expander.find_orphans(to_build, is_modular)

    def get_builddeps(name):
        exp = DepExpander(pkgs)
        for p in packages['build-order-ignore']:
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

    extra_components = packages['extra-components']

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

    for package, to_override in packages['overrides'].items():
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
