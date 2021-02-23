import click
import os
import re
import subprocess
import sys

from .utils import die, error, header, important, info

# ModuleBuilder is a straightforward, simple wrapper around:
#
#   mbs-manager build_module_locally
#
# Except that it extensively filters the logging from that command
# to produce something that intended for a packager creating a module
# rather than a maintainer of an MBS installation.

class ModuleBuilder(object):
    def __init__(self, profile, modulemd, stream, local_builds=[]):
        self.profile = profile
        self.git_repo = self.find_git_repo()
        if self.git_repo:
            self.top_dir = self.git_repo
        else:
            self.top_dir = os.getcwd()

        if modulemd is not None:
            self.modulemd = modulemd
        else:
            self.modulemd = self.find_modulemd()

        if not self.modulemd.endswith(".yaml"):
            die("ModuleMD file '{}' does not have .yaml suffix")

        self.name = os.path.basename(self.modulemd)[:-5]
        if stream is not None:
            self.stream = stream
        else:
            self.stream = self.find_stream()

        self.local_builds = local_builds

    def find_git_repo(self):
        try:
            with open(os.devnull, 'w') as devnull:
                return subprocess.check_output(['git', 'rev-parse', '--show-toplevel'],
                                               stderr=devnull, encoding="UTF-8").strip()
        except subprocess.CalledProcessError:
            return None

    def find_modulemd(self):
        basename = os.path.basename(self.top_dir)
        modulemd = os.path.join(self.top_dir, basename + '.yaml')
        if not os.path.exists(modulemd):
            die("'{}' does not exist".format(modulemd))

        return modulemd

    def find_stream(self):
        if self.git_repo is None:
            return 'master'

        try:
            with open(os.devnull, 'w') as devnull:
                branch = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                                                  stderr=devnull, encoding="UTF-8").strip()
        except subprocess.CalledProcessError:
            branch = None

        if branch is None or branch == 'HEAD':
            die("Cannot determine current git branch")

        return branch

    def build(self):
        header('BUILDING MODULE')

        log_line_re = re.compile(r'.*?-\s*(\S+)\s-\s*(DEBUG|INFO|WARNING|ERROR)\s*-\s*(.*)')
        command_re = re.compile(r'Executing command: \[([^]]+)\](?:, stdout log: (\S+?))?(?:, stderr log: (\S+?))?$')
        build_id_re = re.compile(r'The user "[^"]+" submitted the build "([^"]+)"')

        # These define lines in the log output to ignore, in addition to all
        # messages at level DEBUG
        ignore_res = [re.compile(r) for r in [
            r'Calling complete',
            r'Calling done',
            r'Calling init',
            r'Calling wait',
            r'Cannot re-use',
            r'Connecting to koji',
            r'Found build',
            r'Greenwave is not configured or configured improperly',
            r'Hub not initialized',
            r'Machine arch setting',
            r'MockModuleBuilder initialized',
            r'<ModuleBuild ',
            r'No MBSConsumer found.  Shutting down\?',
            r'Note that retrieved module state 1 doesn\'t match message module state \'wait\'',
            r'Saw relevant component build ',
            r'Scheduling faked event ',
            r'The necessary conflicts could not be generated due to RHBZ#1693683',
            r'u"document: modulemd',
        ]]

        # These are lines that if we see in the log, we consider the build to have failed
        # (Possible we want to treat level=ERROR as fatal always)
        fail_res = [re.compile(r) for r in [
            r'Error while building artifact',
            r'Could not process message handler',
        ]]

        cmd = ['mbs-manager', 'build_module_locally',
               '--file', self.modulemd,
               '--stream', self.stream]
        for build_id in self.local_builds:
            cmd += ['--add-local-build', build_id]

        env = os.environ.copy()
        if self.profile.mbs_config_file is not None:
            env['MBS_CONFIG_FILE'] = self.profile.mbs_config_file
        if self.profile.mbs_config_section is not None:
            env['MBS_CONFIG_SECTION'] = self.profile.mbs_config_section
        process = subprocess.Popen(cmd,
                                   env=env,
                                   bufsize=1,
                                   universal_newlines=True,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)

        output_dir = None
        log_path = None
        log_file = None
        log_lines = []
        failed = False

        while True:
            line = process.stdout.readline()
            if len(line) == 0:
                break

            if output_dir is None or not os.path.exists(output_dir):
                log_lines.append(line)
            else:
                if log_file is None:
                    log_file = open(log_path, 'w')
                    for l in log_lines:
                        log_file.write(l)
                log_file.write(line)
                log_file.flush()

            line = line.rstrip()
            m = log_line_re.match(line)
            if m is None:
                if line == "The configuration file at /etc/module-build-service/config.py was not present":
                    continue
                click.secho(line)
            else:
                logname = m.group(1)
                level = m.group(2)
                message = m.group(3)

                # Since https://pagure.io/fm-orchestrator/pull-request/1161
                # module-build-service messages are categorized as MBS.<subcategory>
                # But then that broke and they were logged as "MBS "
                # https://pagure.io/fm-orchestrator/pull-request/1679
                if logname != 'module_build_service' and logname != 'MBS' and not logname.startswith('MBS.'):
                    continue

                if level == 'DEBUG':
                    continue

                ignored = False
                for p in ignore_res:
                    if p.match(message) is not None:
                        ignored = True
                        break
                if ignored:
                    continue

                for p in fail_res:
                    if p.match(message) is not None:
                        failed = True
                        break

                if level == 'INFO':
                    m = build_id_re.match(message)
                    if m is not None:
                        nsvc = m.group(1)
                        parts = nsvc.split(":")
                        name = parts[0]
                        assert name == self.name
                        stream = parts[1]
                        assert stream == self.stream
                        version = parts[2]

                        output_dir = os.path.join(os.path.expanduser('~/modulebuild'),
                                                  'builds',
                                                  'module-{}-{}-{}'.format(name,
                                                                           stream,
                                                                           version))
                        log_path = os.path.join(output_dir, 'mbs.log')
                        important("name: {}".format(name))
                        important("stream: {}".format(stream))
                        important("version: {}".format(version))
                        important("modulemd: {}".format(self.modulemd))
                        important("log: {}".format(log_path))
                        click.echo()
                    else:
                        m = command_re.match(message)
                        if m is not None:
                            click.secho('running: ', fg='blue', bold=True, err=True, nl=False)
                            pieces = m.group(1).split(', ')
                            click.echo(' '.join(p[1:-1] for p in pieces))
                            if m.group(2) is not None and pieces[0] != "'mock'" and m.group(2) != '/dev/null':
                                click.secho('    stdout: ', fg='blue', err=True, nl=False)
                                click.echo(m.group(2))
                            if m.group(3) is not None:
                                click.secho('    stderr: ', fg='blue', err=True, nl=False)
                                click.echo(m.group(3))
                        else:
                            click.secho('info: ', fg='blue', bold=True, err=True, nl=False)
                            click.echo(message)

                elif level == 'WARNING':
                    click.secho('warning: ', fg='yellow', bold=True, err=True, nl=False)
                    click.echo(message)
                elif level == 'ERROR':
                    click.secho('error: ', fg='red', bold=True, err=True, nl=False)
                    click.echo(message)

        if log_file is not None:
            log_file.close()

        rv = process.wait()
        click.echo()

        if not failed and rv == 0:
            important('{}:{}:{} successfully built'.format(name, stream, version))
            info("log: {}".format(log_path))
        else:
            error("mbs-manager build_module_locally failed")
            error("log: {}".format(log_path))
            sys.exit(1)

        self.version = version

