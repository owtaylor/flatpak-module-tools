"""
_depchase: Resolve dependency & build relationships between RPMs and SRPMs
"""

import collections
import functools
import logging
import re

import smartcols
import solv

from . import repodata
from .util import parse_dataset_name

log = logging.getLogger(__name__)


def setup_pool(arch=None, repos=()):
    if arch is None:
        release_name, arch = parse_dataset_name()

    pool = solv.Pool()
    # pool.set_debuglevel(2)
    pool.setarch(arch)
    pool.set_loadcallback(repodata.load_stub)

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


def make_pool(arch=None):
    return setup_pool(arch, repodata.setup_repos())


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


def _get_rpm(pool, pkg):
    sel = pool.select(pkg, solv.Selection.SELECTION_NAME | solv.Selection.SELECTION_DOTARCH)
    if sel.isempty():
        raise RuntimeError(f"Couldn't find package {pkg}")
    found = sel.solvables()

    # Handle x86 32-bit vs 64-bit multilib packages
    have_x86_64 = any(x.arch == "x86_64" for x in found)
    have_i686 = any(x.arch == "i686" for x in found)
    if have_x86_64 and have_i686:
        found = [x for x in found if x.arch == "x86_64"]

    found = sorted(found, key=functools.cmp_to_key(lambda a, b: a.evrcmp(b)))
    return found[0]


def get_srpm_for_rpm(pool, pkg):
    solvable = _get_rpm(pool, pkg)
    return solvable.lookup_sourcepkg()[:-4]
