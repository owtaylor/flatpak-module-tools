import ConfigParser
import json
import os
import shutil
import subprocess
import sys

from utils import check_call

# Returns flatpak's name for the current arch
def get_arch():
    return subprocess.check_output(['flatpak', '--default-arch']).strip()

# add_app_prefix('org.gimp', 'gimp, 'gimp.desktop') => org.gimp.desktop
# add_app_prefix('org.gnome', 'eog, 'eog.desktop') => org.gnome.eog.desktop
def add_app_prefix(app_id, root, full):
    prefix = app_id
    if prefix.endswith('.' + root):
        prefix = prefix[0:-(1 + len(root))]
    return prefix + '.' + full

def find_desktop_files(builddir):
    for (dirpath, dirnames, filenames) in os.walk(os.path.join(builddir, 'files/share/applications')):
        for filename in filenames:
            if not filename.endswith('.desktop'):
                continue
            yield os.path.join(dirpath, filename)

def find_icons(builddir, name):
    for (dirpath, dirnames, filenames) in os.walk(os.path.join(builddir, 'files/share/icons/hicolor')):
        for filename in filenames:
            if not filename.startswith(name + '.'):
                continue
            yield os.path.join(dirpath, filename)

def update_desktop_files(app_id, builddir):
    for full_path in find_desktop_files(builddir):
        cp = ConfigParser.RawConfigParser()
        cp.read([full_path])
        try:
            icon = cp.get('Desktop Entry', 'Icon')
        except ConfigParser.NoOptionError:
            icon = None

        # Does it have an icon?
        if icon and not icon.startswith(app_id):
	    found_icon=False

	    # Rename any matching icons
	    for icon_file in find_icons(builddir, icon):
		shutil.copy(icon_file,
                            os.path.join(os.path.dirname(icon_file),
                                         add_app_prefix(app_id, icon, os.path.basename(icon_file))))
		found_icon=True

	    # If we renamed the icon, change the desktop file
	    if found_icon:
                check_call(['desktop-file-edit',
                            '--set-icon',
                            add_app_prefix(app_id, icon, icon), full_path])

        # Is the desktop file not prefixed with the app id, then prefix it
        basename = os.path.basename(full_path)
        if not basename.startswith(app_id):
            shutil.move(full_path,
                        os.path.join(os.path.dirname(full_path),
                                     add_app_prefix(app_id,
                                                    basename[:-len('.desktop')],
                                                    basename)))
class FlatpakBuilder(object):
    def __init__(self, build, jsonfile, workdir, runtime):
        self.build = build
        self.workdir = workdir
        self.runtime = runtime

        with open(jsonfile) as f:
            self.info = json.load(f)

    def _build_runtime(self):
        builddir = os.path.join(self.workdir, "build")
        os.mkdir(builddir)

        repo = os.path.join(self.workdir, "repo")
        check_call(['ostree', 'init', '--mode=archive-z2', '--repo', repo])

        runtime_id = self.info['runtime']
        runtime_version = self.info['runtime-version']

        with open(os.path.join(builddir, 'metadata'), 'w') as f:
            f.write("""[Runtime]
name=%(runtime_id)s
runtime=%(runtime_id)s/%(arch)s/%(runtime_version)s
sdk=%(runtime_id)s/%(arch)s/%(runtime_version)s

[Environment]
LD_LIBRARY_PATH=/app/lib64:/app/lib
GI_TYPELIB_PATH=/app/lib64/girepository-1.0
""" % {
            'runtime_id': runtime_id,
            'arch': get_arch(),
            'runtime_version': runtime_version
        })

        runtime_ref = 'runtime/%(runtime_id)s/%(arch)s/%(runtime_version)s' % {
            'runtime_id': runtime_id,
            'arch': get_arch(),
            'runtime_version': runtime_version
        }

        tarfile = os.path.join(self.workdir, 'filesystem.tar.gz')
        outfile = os.path.join(self.workdir, runtime_id + '.flatpak')

        check_call(['ostree', 'commit', '--generate-sizes', '--repo', repo, '--owner-uid=0', '--owner-gid=0', '--no-xattrs', '--branch', runtime_ref, '-s', 'build of ' + runtime_ref, '--tree=tar=' + tarfile, "--tree=dir=" + builddir])
        check_call(['ostree', 'summary', '-u', '--repo', repo])

#    check_call(['flatpak', 'build-bundle', 'exportrepo', '--oci', '--runtime', outfile, runtime_id, runtime_version])
        check_call(['flatpak', 'build-bundle', repo, '--runtime', outfile, runtime_id, runtime_version])

        print >>sys.stderr, 'Wrote', outfile

    def _build_app(self):
        app_id = self.info['id']
        runtime_id = self.info['runtime']
        runtime_version = self.info['runtime-version']

        builddir = os.path.join(self.workdir, "build")
        os.mkdir(builddir)

        tarfile = os.path.join(self.workdir, 'filesystem.tar.gz')
        repo = os.path.join(self.workdir, "repo")

        check_call(['flatpak', 'build-init', builddir, app_id, runtime_id, runtime_id, runtime_version])
        # check_call(['flatpak', 'build', builddir, 'tar', 'xvCf', '/', tarfile])
        check_call(['tar', 'xCf', builddir, tarfile])

        update_desktop_files(app_id, builddir)

        check_call(['flatpak', 'build-finish'] + self.info['finish-args'] + [builddir])
        check_call(['flatpak', 'build-export', repo, builddir])

        outfile = os.path.join(self.workdir, app_id + '.flatpak')
        check_call(['flatpak', 'build-bundle', repo, outfile, app_id]) # FIXME version?

        print >>sys.stderr, 'Wrote', outfile

    def build_flatpak(self):
        if self.runtime:
            self._build_runtime()
        else:
            self._build_app()

