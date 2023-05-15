"""
depchase: Resolve dependency & build relationships between RPMs and SRPMs
"""

import collections
import functools
import logging
import re
from typing import Iterable, List

import solv

from . import repodata
from .repodata import Repo
from ..utils import Arch

log = logging.getLogger(__name__)


def setup_pool(arch: Arch, repos: Iterable[Repo]):
    pool = solv.Pool()
    # pool.set_debuglevel(2)
    pool.setarch(arch.rpm)
    pool.set_loadcallback(repodata.load_stub)

    for repo in repos:
        assert repo.load(pool)
        if "override" in repo.name:
            repo.handle.priority = 99

    addedprovides = pool.addfileprovides_queue()
    if addedprovides:
        for repo in repos:
            repo.updateaddedprovides(addedprovides)

    pool.createwhatprovides()

    fix_deps(pool)

    return pool


def change_dep(pool, txt, flags, func, before, after):
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
        change_dep(pool, txt, flags, func, before, after)


def remove_requires(pool, pkg, required):
    change_dep(pool, pkg, solv.Selection.SELECTION_NAME,
               lambda s: s == required,
               solv.SOLVABLE_REQUIRES, None)


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


FullInfo = collections.namedtuple('FullInfo',
                                  ['name', 'rpm', 'srpm', 'repo', 'requires'])


def make_pool(release: str, arch: Arch) -> solv.Pool:
    return setup_pool(arch, repodata.setup_repos(release, arch))


_DEFAULT_HINTS = ("glibc-minimal-langpack",)


class Transaction:
    def __init__(self, pool: solv.Pool, recommendations=False):
        self.pool = pool
        self.solver = pool.Solver()
        if not recommendations:
            # Ignore weak deps
            self.solver.set_flag(solv.Solver.SOLVER_FLAG_IGNORE_RECOMMENDED, 1)
        self.jobs = []
        self._dep_to_packages_cache = {}
        self.hints = _DEFAULT_HINTS

    def add_packages(self, pkgnames: Iterable[str]):
        for n in pkgnames:
            search_criteria = (solv.Selection.SELECTION_NAME | solv.Selection.SELECTION_DOTARCH)
            if "." in n:
                search_criteria |= solv.Selection.SELECTION_CANON
            sel = self.pool.select(n, search_criteria)
            if sel.isempty():
                log.warn(f"Could not find package for {n}")
                continue
            self.jobs += sel.jobs(solv.Job.SOLVER_INSTALL)

    def add_provides(self, provides: Iterable[str]):
        for provide in provides:
            search_criteria = solv.Selection.SELECTION_PROVIDES | solv.Selection.SELECTION_REL
            sel = self.pool.select(provide, search_criteria)
            if sel.isempty() and provide.startswith("/"):
                sel = self.pool.select(provide, solv.Selection.SELECTION_FILELIST)
            if sel.isempty():
                log.warn(f"Could not find package providing {provide}")
                continue
            self.jobs += sel.jobs(solv.Job.SOLVER_INSTALL)

    def set_hints(self, hints: Iterable[str]):
        self.hints = list(hints)

    def solve(self):
        for n in self.hints:
            sel = self.pool.select(n, solv.Selection.SELECTION_NAME)
            self.jobs += sel.jobs(solv.Job.SOLVER_FAVOR)

        problems = self.solver.solve(self.jobs)
        if problems:
            for problem in problems:
                log.warn(problem)

        self.transaction = self.solver.transaction()
        self.newpackages = self.transaction.newpackages()

    def _get_packages_providing_dep(self, dep):
        result = self._dep_to_packages_cache.get(dep)
        if result is not None:
            return result

        matches = {
            s
            for s in self.newpackages
            if s.matchesdep(solv.SOLVABLE_PROVIDES, dep)
        }
        if not matches and str(dep).startswith("/"):
            # Append provides by files
            # TODO: use Dataiterator for getting filelist
            matches = {
                s
                for s in self.pool.select(
                    str(dep), solv.Selection.SELECTION_FILELIST
                ).solvables()
                if s in self.newpackages
            }
        # It was possible to resolve set, so something is wrong here
        if not matches:
            if _dependency_is_conditional(dep):
                log.debug(f"Conditional dependency {dep} doesn't need to be satisfied")
            else:
                raise RuntimeError(
                    f"Dependency {dep} isn't satisfied in resolved packages!"
                )

        result = sorted(str(m) for m in matches)

        self._dep_to_packages_cache[dep] = result

        return result

    def get_packages_providing_dep(self, dep):
        m = re.match(r'^(\S+)\s*([<>=]+)\s*(\S+)$', dep)
        if m is not None:
            name = m.group(1)
            rel = m.group(2)
            ver = m.group(3)

            flags = 0
            if "<" in rel:
                flags |= solv.REL_LT
            if ">" in rel:
                flags |= solv.REL_GT
            if "=" in rel:
                flags |= solv.REL_EQ

            base_dep = self.pool.Dep(name, create=True)
            ver_id = self.pool.Dep(ver)
            dep_id = base_dep.Rel(flags, ver_id)
        else:
            dep_id = self.pool.Dep(dep, create=True)

        return self._get_packages_providing_dep(dep_id)

    def get_packages(self):
        return {s.name for s in self.newpackages if s.arch not in ("src", "nosrc")}

    def get_full_infos(self):
        result: List[FullInfo] = []
        for s in self.newpackages:
            if s.arch in ("src", "nosrc"):
                continue
            # Ensure the solvables don't outlive the solver that created them by
            # extracting the information we want but not returning the solvable.
            rpm = str(s)

            pkg_details = {}
            for dep in _iterate_all_requires(s):
                pkg_details[str(dep)] = self._get_packages_providing_dep(dep)

            result.append(FullInfo(
                s.name, rpm, s.lookup_sourcepkg()[:-4], s.repo.name, pkg_details)
            )

        return result


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
