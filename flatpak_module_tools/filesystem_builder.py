import os
import subprocess
import sys
import tarfile

from utils import check_call

IMAGEBUILDER = os.path.expanduser("~/go/bin/imagebuilder")

class FilesystemBuilder(object):
    def __init__(self, locator, build, workdir, runtime):
        self.locator = locator
        self.build = build
        self.workdir = workdir
        self.runtime = runtime
        self.docker_tag = "flatpak-module:{}-{}-{}".format(build.name, build.stream, build.version)

    def _write_repofile(self):
        repofile = os.path.join(self.workdir, "module.repo")
        config, path_map = self.locator.build_yum_config(self.build.name, self.build.stream)
        with open(repofile, "w") as f:
            f.write(config)
        self.path_map = path_map

    def _write_dockerfile(self):
        dockerfile = os.path.join(self.workdir, "Dockerfile")
        if self.runtime:
            packages = self.build.mmd.profiles['runtime'].rpms
        else:
            packages = self.build.mmd.profiles['default'].rpms
        with open(dockerfile, "w") as f:
            f.write("""FROM registry.fedoraproject.org/fedora:26

COPY module.repo  /var/tmp/flatpak-build/etc/yum.repos.d/
RUN dnf -y --nogpgcheck --installroot=/var/tmp/flatpak-build install {}
""".format(" ".join(sorted(packages))))

    def _build_image(self):
        args = [IMAGEBUILDER]
        for source_path, dest_path in self.path_map.items():
            args += ['-mount', source_path + ":" + dest_path]
        args += ['-t', self.docker_tag]
        args += [self.workdir]

        print >>sys.stderr, "Building container image"
        print >>sys.stderr, args
        check_call(args)

    def _get_target_path(self, export_path):
        if self.runtime:
            if export_path == "var/tmp/flatpak-build":
                return "files"
            elif export_path == "var/tmp/flatpak-build/etc":
                return "files/etc"
            elif export_path == "var/tmp/flatpak-build/usr":
                return None
            elif export_path == "var/tmp/flatpak-build/usr/etc":
                return None
        else:
            if export_path == "var/tmp/flatpak-build/app":
                return "files"

        if not export_path.startswith("var/tmp/flatpak-build/"):
            return None

        short_name = export_path[len("var/tmp/flatpak-build/"):]

        if self.runtime:
            if short_name.startswith("usr/"):
                return "files/" + short_name[4:]
            elif short_name.startswith("usr/etc/"):
                None
            elif short_name.startswith("etc/"):
                return "files/" + short_name
            else:
                return None
        else:
            if short_name.startswith("app/"):
                return "files/" + short_name[4:]
            else:
                return None

    def _export_container(self, container_id):
        print >>sys.stderr, "Writing contents to tarfile"
        outfile = os.path.join(self.workdir, 'filesystem.tar.gz')

        process = subprocess.Popen(['docker', 'export', container_id], stdout=subprocess.PIPE)
        out_fileobj = open(outfile, "w")
        compress_process = subprocess.Popen(['gzip', '-c'], stdin=subprocess.PIPE, stdout=out_fileobj)
        in_tf = tarfile.open(fileobj=process.stdout, mode='r|')
        out_tf = tarfile.open(fileobj=compress_process.stdin, mode='w|')

        for member in in_tf:
            target_name = self._get_target_path(member.name)
            if target_name is None:
                continue

            # flatpak tries to create files/.ref and behaves badly if the root directory
            # isn't writable. It also has trouble upgrading if any directory is not
            # user-writable.
            member.mode |= 0200

            member.name = target_name
#                print >>sys.stderr, member.name
            if member.islnk():
                # Hard links have full paths within the archive (no leading /)
                link_target = self._get_target_path(member.linkname)
                if link_target is None:
                    print >>sys.stderr, "Skipping {}, hard link to {}", target_name, link_target
                    continue
                member.linkname = link_target
                out_tf.addfile(member)
            elif member.issym():
                # Symlinks have the literal link target, which will be
                # relative to the chroot and doesn't need rewriting
                out_tf.addfile(member)
            else:
                f = in_tf.extractfile(member)
                out_tf.addfile(member, fileobj=f)

        in_tf.close()
        out_tf.close()
        if process.wait() != 0:
            raise RuntimeException("docker export command failed")
        compress_process.stdin.close()
        if compress_process.wait() != 0:
            raise RuntimeException("gzip failed")
        out_fileobj.close()

    def _export_image(self):
        print >>sys.stderr, "Creating temporary docker container"
        container_id = subprocess.check_output(['docker', 'create', self.docker_tag]).strip()
        try:
            self._export_container(container_id)
        finally:
            print >>sys.stderr, "Cleaning up docker container"
            subprocess.call(['docker', 'rm', container_id])

    def build_filesystem(self):
        self._write_repofile()
        self._write_dockerfile()
        self._build_image()
        self._export_image()

