"""_repodata: Resolve module relationship queries against the local cache"""
import logging
import os.path
import sys
import tempfile

import solv

from .repo_definition import RepoDefinition, CACHEDIR
from ..utils import die


log = logging.getLogger(__name__)


class Repo(object):
    def __init__(self, definition: RepoDefinition):
        # Sanity-check that the repo exists
        repomd_fname = os.path.join(definition.local_cache_path, "repodata", "repomd.xml")
        if not os.path.exists(repomd_fname):
            raise RuntimeError(
                f"Cached repodata for {definition.name} not found at {definition.local_cache_path}"
            )

        self.name = definition.name
        self.metadata_path = definition.local_cache_path
        self.cachedir = CACHEDIR
        self._handle: solv.Repo | None = None
        self.cookie = None
        self.extcookie = None

    @property
    def handle(self) -> solv.Repo:
        assert self._handle is not None
        return self._handle

    @staticmethod
    def calc_cookie_fp(fp):
        chksum = solv.Chksum(solv.REPOKEY_TYPE_SHA256)
        chksum.add("1.1")
        chksum.add_fp(fp)
        return chksum.raw()

    @staticmethod
    def calc_cookie_ext(f, cookie):
        chksum = solv.Chksum(solv.REPOKEY_TYPE_SHA256)
        chksum.add("1.1")
        chksum.add(cookie)
        chksum.add_fstat(f.fileno())
        return chksum.raw()

    def cachepath(self, ext=None):
        path = f"{self.name.replace('.', '_')}-{self.metadata_path}"
        if ext:
            path = f"{path}-{ext}.solvx"
        else:
            path = f"{path}.solv"
        return os.path.join(self.cachedir, path.replace("/", "_"))

    def usecachedrepo(self, ext, mark=False):
        try:
            repopath = self.cachepath(ext)
            f = open(repopath, "rb")
            f.seek(-32, os.SEEK_END)
            fcookie = f.read(32)
            if len(fcookie) != 32:
                return False
            cookie = self.extcookie if ext else self.cookie
            if cookie and fcookie != cookie:
                return False
            fextcookie = None
            if not ext:
                f.seek(-32 * 2, os.SEEK_END)
                fextcookie = f.read(32)
                if len(fextcookie) != 32:
                    return False
            f.seek(0)
            f = solv.xfopen_fd(None, f.fileno())
            flags = 0
            if ext:
                flags = (solv.Repo.REPO_USE_LOADING
                         | solv.Repo.REPO_EXTEND_SOLVABLES)
                if ext != "DL":
                    flags |= solv.Repo.REPO_LOCALPOOL
            if not self.handle.add_solv(f, flags):
                return False
            if not ext:
                assert fextcookie is not None
                self.cookie = fcookie
                self.extcookie = fextcookie
            if mark:
                # no futimes in python?
                try:
                    os.utime(repopath, None)
                except Exception:
                    pass
        except IOError:
            return False
        return True

    def writecachedrepo(self, ext, repodata: solv.XRepodata | None = None):
        tmpname = None
        try:
            fd, tmpname = tempfile.mkstemp(prefix=".newsolv-",
                                           dir=self.cachedir)
            os.fchmod(fd, 0o444)
            f = os.fdopen(fd, "wb+")
            f = solv.xfopen_fd(None, f.fileno())
            if not repodata:
                self.handle.write(f)
            elif ext:
                repodata.write(f)
            else:
                # rewrite_repos case, do not write stubs
                self.handle.write_first_repodata(f)
            f.flush()
            if not ext:
                if not self.extcookie:
                    self.extcookie = self.calc_cookie_ext(f, self.cookie)
                f.write(self.extcookie)
            if not ext:
                f.write(self.cookie)
            else:
                f.write(self.extcookie)
            f.close
            if self.handle.iscontiguous():
                # switch to saved repo to activate paging and save memory
                nf = solv.xfopen(tmpname)
                if not ext:
                    # main repo
                    self.handle.empty()
                    flags = solv.Repo.SOLV_ADD_NO_STUBS
                    if repodata:
                        # rewrite repos case, recreate stubs
                        flags = 0
                    if not self.handle.add_solv(nf, flags):
                        sys.exit("internal error, cannot reload solv file")
                else:
                    assert repodata is not None
                    # extension repodata
                    # need to extend to repo boundaries, as this is how
                    # repodata.write() has written the data
                    repodata.extend_to_repo()
                    flags = solv.Repo.REPO_EXTEND_SOLVABLES
                    if ext != "DL":
                        flags |= solv.Repo.REPO_LOCALPOOL
                    repodata.add_solv(nf, flags)
            os.rename(tmpname, self.cachepath(ext))
        except (OSError, IOError):
            if tmpname:
                os.unlink(tmpname)

    def load(self, pool: solv.Pool):
        assert not self._handle
        handle = pool.add_repo(self.name)
        self._handle = handle
        handle.appdata = self
        f = self.read_repo_metadata("repodata/repomd.xml", False, None)
        if not f:
            handle.free(True)
            self._handle = None
            return False
        self.cookie = self.calc_cookie_fp(f)
        if self.usecachedrepo(None, True):
            return True
        handle.add_repomdxml(f)
        fname, fchksum = self.find("primary")
        if not fname:
            handle.free(True)
            self._handle = None
            return False
        f = self.read_repo_metadata(fname, True, fchksum)
        if not f:
            handle.free(True)
            self._handle = None
            return False
        handle.add_rpmmd(f, None)
        self.add_exts()
        self.writecachedrepo(None)
        # Must be called after writing the repo
        handle.create_stubs()
        return True

    def read_repo_metadata(self, fname, uncompress, chksum):
        f = open(f"{self.metadata_path}/{fname}")
        return solv.xfopen_fd(fname if uncompress else None, f.fileno())

    def find(self, what):
        assert self.handle is not None
        di = self.handle.Dataiterator_meta(solv.REPOSITORY_REPOMD_TYPE, what,
                                           solv.Dataiterator.SEARCH_STRING)
        di.prepend_keyname(solv.REPOSITORY_REPOMD)
        for d in di:
            dp = d.parentpos()
            filename = dp.lookup_str(solv.REPOSITORY_REPOMD_LOCATION)
            chksum = dp.lookup_checksum(solv.REPOSITORY_REPOMD_CHECKSUM)
            if filename:
                if not chksum:
                    die(f"No {filename} file checksum!")
                return filename, chksum
        return None, None

    def add_ext_keys(self, ext, repodata, handle):
        if ext == "FL":
            repodata.add_idarray(handle, solv.REPOSITORY_KEYS,
                                 solv.SOLVABLE_FILELIST)
            repodata.add_idarray(handle, solv.REPOSITORY_KEYS,
                                 solv.REPOKEY_TYPE_DIRSTRARRAY)
        else:
            raise NotImplementedError

    def add_ext(self, repodata, what, ext):
        filename, chksum = self.find(what)
        if not filename:
            return
        handle = repodata.new_handle()
        repodata.set_poolstr(handle, solv.REPOSITORY_REPOMD_TYPE, what)
        repodata.set_str(handle, solv.REPOSITORY_REPOMD_LOCATION, filename)
        repodata.set_checksum(handle, solv.REPOSITORY_REPOMD_CHECKSUM, chksum)
        self.add_ext_keys(ext, repodata, handle)
        repodata.add_flexarray(solv.SOLVID_META, solv.REPOSITORY_EXTERNAL,
                               handle)

    def add_exts(self):
        assert self.handle is not None
        repodata = self.handle.add_repodata()
        self.add_ext(repodata, "filelists", "FL")
        repodata.internalize()

    def load_ext(self, repodata: solv.XRepodata):
        assert self.handle is not None
        repomdtype = repodata.lookup_str(solv.SOLVID_META,
                                         solv.REPOSITORY_REPOMD_TYPE)
        if repomdtype == "filelists":
            ext = "FL"
        else:
            assert False
        if self.usecachedrepo(ext):
            return True
        filename = repodata.lookup_str(solv.SOLVID_META,
                                       solv.REPOSITORY_REPOMD_LOCATION)
        filechksum = repodata.lookup_checksum(solv.SOLVID_META,
                                              solv.REPOSITORY_REPOMD_CHECKSUM)
        f = self.read_repo_metadata(filename, True, filechksum)
        if not f:
            return False
        if ext == "FL":
            self.handle.add_rpmmd(f, "FL",
                                  solv.Repo.REPO_USE_LOADING
                                  | solv.Repo.REPO_EXTEND_SOLVABLES
                                  | solv.Repo.REPO_LOCALPOOL)
        self.writecachedrepo(ext, repodata)
        return True

    def updateaddedprovides(self, addedprovides):
        assert self.handle is not None
        if self.handle.isempty():
            return
        # make sure there's just one real repodata with extensions
        repodata = self.handle.first_repodata()
        if not repodata:
            return
        oldaddedprovides = repodata.lookup_idarray(
            solv.SOLVID_META, solv.REPOSITORY_ADDEDFILEPROVIDES)
        if not set(addedprovides) <= set(oldaddedprovides):
            for id in addedprovides:
                repodata.add_idarray(solv.SOLVID_META,
                                     solv.REPOSITORY_ADDEDFILEPROVIDES, id)
            repodata.internalize()
            self.writecachedrepo(None, repodata)


def load_stub(repodata):
    repo = repodata.repo.appdata
    if repo:
        return repo.load_ext(repodata)
    return False
