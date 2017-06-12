import hawkey
import re
import sys

def package_cmp(a, b):
    if a.arch == 'i686' and b.arch != 'i686':
        return 1
    if a.arch != 'i686' and b.arch == 'i686':
        return -1
    c = cmp(a.repo.priority, b.repo.priority)
    if c != 0:
        return c
    c = cmp(a.name, b.name)
    if c != 0:
        return c
    # evr_cmp returns a long, so we need to turn it into integer -1/0/1
    c = - a.evr_cmp(b)
    if c < 0:
        return -1
    elif c == 0:
        return 0
    else:
        return 1

class DepExpander(object):
    def __init__(self, pkgs):
        self.pkgs = pkgs
        self.binary = {}
        self.source = {}
        self.source_to_binary = {}
        self.required_by = {}
        self.include_builddeps = False

    def binary_to_source(self, binary_name):
        pkg = self.binary[binary_name]
        return hawkey.split_nevra(pkg.sourcerpm).name

    def _add_binary_package(self, pkg, new_binaries, new_sources, include_source, required_by):
        self.binary[pkg.name] = pkg
        if required_by:
            self.required_by[pkg.name] = required_by
        new_binaries.add(pkg.name)
#        print >>sys.stderr, str(pkg), required_by

        if not include_source:
            return

        source_name = self.binary_to_source(pkg.name)
        source_pkg = self.pkgs.find_source_package(source_name)
        if source_pkg is None:
            print >>sys.stderr, "No source package for " + source_name
            return

        if not source_pkg.name in self.source:
            new_sources.add(source_pkg.name)
            self.source_to_binary[source_pkg.name] = pkg.name
            self.source[source_pkg.name] = source_pkg
#            print >>sys.stderr, str(source_pkg),

    def _resolve_binary_name(self, name):
        if name in self.binary:
            return self.binary[name]
        else:
            for p in self.pkgs.base.sack.query().filter(name=name, arch__eq=['x86_64', 'noarch']):
                if self.include_builddeps or p.repo.priority != 20:
                    return p

        raise Exception("No package named " + name)

    def _add_requires(self, pkg, new_binaries, new_sources, include_source):
        requires = pkg.requires
        for r in requires:
            if not isinstance(r, basestring):
                s = str(r)
                m = re.match('(.*?)\(([^\)]+)\)$', s)
                if m is not None:
                    if m.group(2).startswith('armv7hl'):
                        print >>sys.stderr, "\nRemoving spurious architecture from SRPM: " + str(r)
                        r = m.group(1)
            providing = []
            already_provided = False
            for p in self.pkgs.base.sack.query().filter(provides=r):
                if not self.include_builddeps and p.repo.priority == 20:
                    continue
                #, arch__eq=['x86_64','noarch']
                if p.arch == 'src':
                    continue
                if p.name in self.binary:
                    already_provided = True
                else:
                    providing.append(p)
            if not already_provided:
                if len(providing) == 0:
                    raise Exception("Couldn't find package providing " + str(r) + " for " + pkg.name)
#                print [(p.name, p.version, p.release, p.arch, p.reponame, p.repo.priority) for p in providing]
                providing.sort(package_cmp)
#                print [(p.name, p.version, p.release, p.arch, p.reponame, p.repo.priority) for p in providing]
                self._add_binary_package(providing[0], new_binaries, new_sources, include_source, pkg.name)

    def _complete_dependencies(self, new_binaries, new_sources, include_source):
        print >>sys.stderr, "Adding dependencies of binary packages"
        added_binaries = set()

        while len(new_binaries) > 0:
            print >>sys.stderr, "Binary: %d, Source %d" % (len(self.binary), len(self.source))
            added_binaries.update(new_binaries)
            old = new_binaries
            new_binaries = set()
            for pkgname in old:
                self._add_requires(self.binary[pkgname], new_binaries, new_sources, include_source)

        return added_binaries, new_sources

    def add_binaries(self, names, include_source=True):
        new_binaries = set()
        new_sources = set()
        for name in names:
            if name not in self.binary:
                pkg = self._resolve_binary_name(name)
                if pkg == None:
                    raise Exception("No package provides " + name)
                self._add_binary_package(pkg, new_binaries, new_sources, include_source, None)

        return self._complete_dependencies(new_binaries, new_sources, include_source=include_source)

    def add_builddeps(self, source_names, include_source=True):
        print >>sys.stderr, "Adding build dependencies"

        new_sources = set()
        new_binaries = set()
        for pkgname in source_names:
            source_pkg = self.source.get(pkgname, None)
            if source_pkg is None:
                source_pkg = self.pkgs.find_source_package(pkgname)
            self._add_requires(source_pkg, new_binaries, new_sources, include_source=include_source)

        return self._complete_dependencies(new_binaries, new_sources, include_source=include_source)

    def find_orphans(self, packages, is_external):
        orphans = set()
        found_orphans = True
        while found_orphans:
            found_orphans = False
            for name in packages:
                if name in orphans:
                    continue

                binary = self.source_to_binary[name]
                requiring_package = self.required_by.get(binary, None)
                if requiring_package is not None:
                    if requiring_package in self.binary:
                        requiring_source = self.binary_to_source(requiring_package)
                        requiring_repo = self.binary[requiring_package].reponame
                    else:
                        requiring_source = requiring_package
                        requiring_repo = self.source[requiring_package].reponame

                    if requiring_source in orphans:
                        orphans.add(name)
                        found_orphans = True
                        continue

                    if is_external(requiring_repo):
                        orphans.add(name)
                        found_orphans = True
                        continue

        return orphans

    def dump_dependency_tree(self, packages, outfile):
        tree = {}
        mapping = {}
        for n in packages:
            mapping[n] = {}

        for n in packages:
            required_by = self.required_by.get(n, None)
            if required_by is not None:
                mapping[required_by][n] = mapping[n]
            else:
                tree[n] = mapping[n]

        with open(outfile, 'w') as f:
            def dump_level(d, indent=''):
                for n in sorted(d.keys()):
                    print >>f, indent + n
                    dump_level(d[n], indent + '  ')
            dump_level(tree)
