"""
_depchase: Resolve dependency & build relationships between RPMs and SRPMs
"""

import collections
import functools
import logging
import os
import re

import smartcols
import solv

from . import _repodata
from .util import arch_score, parse_dataset_name

log = logging.getLogger(__name__)


def setup_pool(arch=None, repos=()):
    if arch is None:
        release_name, arch = parse_dataset_name()

    pool = solv.Pool()
    # pool.set_debuglevel(2)
    pool.setarch(arch)
    pool.set_loadcallback(_repodata.load_stub)

    for repo in repos:
        repo.metadata_path = repo.metadata_path.format(arch=arch)

    for repo in repos:
        assert repo.load(pool)
        if "override" in repo.name:
            repo.handle.priority = 99

    addedprovides = pool.addfileprovides_queue()
    if addedprovides:
        for repo in repos:
            repo.updateaddedprovides(addedprovides)

    pool.createwhatprovides()

    return pool


def fix_deps(pool):
    to_fix = (
        # Weak libcrypt-nss deps due to
        # https://github.com/openSUSE/libsolv/issues/205
        ("glibc", solv.Selection.SELECTION_NAME,
         solv.SOLVABLE_RECOMMENDS,
         lambda s: s.startswith("libcrypt-nss"), solv.SOLVABLE_SUGGESTS),
        # Shim is not buildable
        ("shim",
         solv.Selection.SELECTION_NAME | solv.Selection.SELECTION_WITH_SOURCE,
         solv.SOLVABLE_REQUIRES,
         lambda s: s in ("gnu-efi = 3.0w", "gnu-efi-devel = 3.0w"), None),
    )
    for txt, flags, before, func, after in to_fix:
        for s in pool.select(txt, flags).solvables():
            deps = s.lookup_deparray(before)
            fixing = [dep for dep in deps if func(str(dep))]
            for dep in fixing:
                deps.remove(dep)
                if after is not None:
                    s.add_deparray(after, dep)
            # Use s.set_deparray() once will be available
            s.unset(before)
            for dep in deps:
                s.add_deparray(before, dep)


def get_sourcepkg(p, s=None, only_name=False):
    if s is None:
        s = p.lookup_sourcepkg()[:-4]
    if only_name:
        return s
    # Let's try to find corresponding source
    sel = p.pool.select(s,
                        solv.Selection.SELECTION_CANON
                        | solv.Selection.SELECTION_SOURCE_ONLY)
    sel.filter(p.repo.appdata.srcrepo.handle.Selection())
    assert not sel.isempty(), "Could not find source package for {}".format(s)
    solvables = sel.solvables()
    assert len(solvables) == 1
    return solvables[0]


def _iterate_all_requires(package):
    # pre-requires
    for dep in package.lookup_deparray(solv.SOLVABLE_REQUIRES, 1):
        yield dep
    # requires
    for dep in package.lookup_deparray(solv.SOLVABLE_REQUIRES, -1):
        yield dep


_BOOLEAN_KEYWORDS = re.compile(r" (?:and|or|if|with|without|unless) ")

def _dependency_is_conditional(dependency):
    return _BOOLEAN_KEYWORDS.search(str(dependency)) is not None


def _get_dependency_details(pool, transaction):
    cache = {}

    candq = transaction.newpackages()
    result = {}
    for p in candq:
        pkg_details = {}
        for dep in _iterate_all_requires(p):
            if dep in cache:
                matches = cache[dep]
            else:
                matches = {
                    s
                    for s in candq
                    if s.matchesdep(solv.SOLVABLE_PROVIDES, dep)
                }
                if not matches and str(dep).startswith("/"):
                    # Append provides by files
                    # TODO: use Dataiterator for getting filelist
                    matches = {
                        s
                        for s in pool.select(
                            str(dep), solv.Selection.SELECTION_FILELIST
                        ).solvables()
                        if s in candq
                    }
                # It was possible to resolve set, so something is wrong here
                if not matches:
                    if _dependency_is_conditional(dep):
                        log.debug("Conditional dependency {} doesn't need to be satisfied".format(dep))
                    else:
                        raise RuntimeError("Dependency {} isn't satisfied in resolved packages!".format(dep))
                cache[dep] = matches

            # While multiple packages providing the same thing is rare, it's
            # the kind of duplication we want fedmod to be able to help find.
            # So we always return a list here, even though it will normally
            # only have one entry in it
            pkg_details[str(dep)] = sorted(str(m) for m in matches)
        result[str(p)] = pkg_details

    return result


def print_transaction(details, pool):
    tb = smartcols.Table()
    tb.title = "DEPENDENCY INFORMATION"
    cl = tb.new_column("INFO")
    cl.tree = True
    cl_match = tb.new_column("MATCH")
    cl_srpm = tb.new_column("SRPM")
    cl_repo = tb.new_column("REPO")
    for p in sorted(details):
        ln = tb.new_line()
        ln[cl] = p
        deps = details[p]
        for dep in sorted(deps):
            matches = deps[dep]
            lns = tb.new_line(ln)
            lns[cl] = dep
            first = True
            for m in matches:
                if first:
                    lnc = lns
                else:
                    lnss = tb.new_line(lns)
                    lnc = lnss
                    first = False
                lnc[cl_match] = m
                lnc[cl_srpm] = get_srpm_for_rpm(pool, m)
            sel = pool.select(m, solv.Selection.SELECTION_CANON)
            if sel.isempty():
                lnc[cl_repo] = "Unknown repo"
            else:
                s = sel.solvables()
                assert len(s) == 1
                lnc[cl_repo] = str(s[0].repo)
    log.info(tb)


FullInfo = collections.namedtuple('FullInfo',
                                  ['name', 'rpm', 'srpm', 'requires'])


def _solve(solver, pkgnames, full_info=False):
    """Given a set of package names, returns a list of solvables to install"""
    pool = solver.pool

    # We have to =(
    fix_deps(pool)

    jobs = []
    # Initial jobs, no conflicting packages
    for n in pkgnames:
        search_criteria = (solv.Selection.SELECTION_NAME
                        | solv.Selection.SELECTION_DOTARCH)
        if "." in n:
            search_criteria |= solv.Selection.SELECTION_CANON
        sel = pool.select(n, search_criteria)
        if sel.isempty():
            log.warn("Could not find package for {}".format(n))
            continue
        jobs += sel.jobs(solv.Job.SOLVER_INSTALL)
    problems = solver.solve(jobs)
    if problems:
        for problem in problems:
            log.warn(problem)

    if log.getEffectiveLevel() <= logging.INFO or full_info:
        dep_details = _get_dependency_details(pool, solver.transaction())
        if log.getEffectiveLevel() <= logging.INFO:
            print_transaction(dep_details, pool)

    if full_info:
        result = []
    else:
        result = set()
    for s in solver.transaction().newpackages():
        if s.arch in ("src", "nosrc"):
            continue
        # Ensure the solvables don't outlive the solver that created them by
        # extracting the information we want but not returning the solvable.
        if full_info:
            rpm = str(s)
            result.append(FullInfo(s.name, rpm, s.lookup_sourcepkg()[:-4],
                                   dep_details[rpm]))
        else:
            result.add(s.name)
    return result


def ensure_buildable(pool, pkgnames, full_info=False):
    """Given a set of solvables, returns a set of source packages & build
    deps"""
    # The given package set may not be installable on its own
    # That's OK, since other modules will provide those packages
    # The goal of *this* method is to report the SRPMs that need to be
    # built, and their build dependencies
    sources = set(get_srpm_for_rpm(pool, n) for n in pkgnames)
    builddeps = ensure_installable(pool, sources)
    return sources, builddeps


def make_pool(arch=None):
    return setup_pool(arch, _repodata.setup_repos())


_DEFAULT_HINTS = ("glibc-minimal-langpack",)


def ensure_installable(pool, pkgnames, hints=_DEFAULT_HINTS,
                       recommendations=False, full_info=False):
    """Iterate over the resolved dependency set for the given packages

    *hints*:  Packages that have higher priority when more than one package
              could satisfy a dependency.
    *recommendations*: Whether or not to report recommended dependencies as
                       well as required dependencies (Default: required deps
                       only)
    """
    if pool is None:
        pool = make_pool()
    # Set up initial hints
    favorq = []
    for n in hints:
        sel = pool.select(n, solv.Selection.SELECTION_NAME)
        favorq += sel.jobs(solv.Job.SOLVER_FAVOR)
    pool.setpooljobs(favorq)

    solver = pool.Solver()
    if not recommendations:
        # Ignore weak deps
        solver.set_flag(solv.Solver.SOLVER_FLAG_IGNORE_RECOMMENDED, 1)

    return _solve(solver, pkgnames, full_info=full_info)


def print_reldeps(pool, pkg):
    sel = pool.select(pkg,
                      solv.Selection.SELECTION_NAME
                      | solv.Selection.SELECTION_DOTARCH)
    assert not sel.isempty(), "Package can't be found"
    found = sel.solvables()
    assert len(found) == 1, "More matching solvables were found, {}".format(
        found)
    s = found[0]

    reldep2str = {solv.SOLVABLE_REQUIRES: "requires",
                  solv.SOLVABLE_RECOMMENDS: "recommends",
                  solv.SOLVABLE_SUGGESTS: "suggests",
                  solv.SOLVABLE_SUPPLEMENTS: "supplements",
                  solv.SOLVABLE_ENHANCES: "enhances"}
    for reltype, relstr in reldep2str.items():
        for dep in s.lookup_deparray(reltype):
            print("{}: {}".format(relstr, dep))


def _get_rpm(pool, pkg):
    search_criteria = (solv.Selection.SELECTION_NAME
                    | solv.Selection.SELECTION_DOTARCH)
    if "." in pkg:
        search_criteria  |= solv.Selection.SELECTION_CANON
    sel = pool.select(pkg, search_criteria)
    if sel.isempty():
        raise RuntimeError("Couldn't find package {}".format(pkg))
    found = sel.solvables()

    # determine latest EVR in the found packages
    found = sorted(found, key=functools.cmp_to_key(lambda a, b: b.evrcmp(a)))
    latest_evr = found[0].evr

    # filter for latest and arch-compatible
    found = [f for f in found if f.evr == latest_evr and arch_score(f.arch)]

    # return the one package with the best matching architecture for multilib
    return functools.reduce(lambda x, y:
                            x if arch_score(x.arch) < arch_score(y.arch) else y,
                            found)


def get_srpm_for_rpm(pool, pkg):
    s = _get_rpm(pool, pkg)

    return get_sourcepkg(s, only_name=True)


def get_rpms_for_srpms(pool, pkgnames):
    sources = set()
    for n in pkgnames:
        sel = pool.select(n,
                          solv.Selection.SELECTION_NAME
                          | solv.Selection.SELECTION_DOTARCH
                          | solv.Selection.SELECTION_SOURCE_ONLY)
        if not sel.isempty():
            srcrpm = sel.solvables()[0]
            # lookup_location() here will be something like
            #   Packages/n/nginx-1.12.1-1.fc27.src.rpm
            # Which is closer to lookup_sourcepkg() which we match with below
            sources.add(os.path.basename(srcrpm.lookup_location()[0]))

    # This search is O(N) where N = the number of packages in Fedora
    # so it would be nice to find a more algorithmically efficient approach
    # OTOH, we only run this when *fetching* metadata, so it isn't too bad
    result = set()
    for p in (s for s in pool.solvables if s.arch not in ("src", "nosrc")):
        if p.lookup_sourcepkg() in sources:
            result.add(p.name)
    return result


def get_rpm_metadata(pool, pkg):
    s = _get_rpm(pool, pkg)

    return {
        'summary': s.lookup_str(solv.SOLVABLE_SUMMARY),
        'description': s.lookup_str(solv.SOLVABLE_DESCRIPTION),
    }
