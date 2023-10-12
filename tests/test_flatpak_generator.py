"""In-process tests for the flatpak generator"""

import logging
import os
import tempfile

from click.testing import CliRunner
import pytest
import responses
import yaml

from flatpak_module_tools.cli import cli


log = logging.getLogger(__name__)

testfiles_dir = os.path.join(os.path.dirname(__file__), 'files', 'generator')

with open(os.path.join(testfiles_dir, 'apps.json')) as f:
    APPS_JSON = f.read()

with open(os.path.join(testfiles_dir, 'eog.yaml')) as f:
    EOG_YAML = f.read()

with open(os.path.join(testfiles_dir, 'eog.json')) as f:
    EOG_JSON = f.read()

with open(os.path.join(testfiles_dir, 'releases.json')) as f:
    RELEASES_JSON = f.read()


def _generate_flatpak(rpm, flathub=None, runtime_name=None, runtime_version=None, expected_error_output=None):
    cmd = ['init']
    cmd.append(rpm)
    if flathub:
        cmd += ['--flathub', flathub]

    prevdir = os.getcwd()
    with tempfile.TemporaryDirectory() as workdir:
        try:
            os.chdir(workdir)
            runner = CliRunner()
            result = runner.invoke(cli, cmd, catch_exceptions=False)
            if expected_error_output is not None:
                assert result.exit_code != 0
                assert expected_error_output in result.output
                return
            else:
                assert result.exit_code == 0

            with open('container.yaml') as f:
                contents = f.read()

            log.info('container.yaml:\n%s\n', contents)
            container_yaml = yaml.safe_load(contents)
        finally:
            os.chdir(prevdir)

    return container_yaml


class TestFlatpak(object):
    @pytest.mark.filterwarnings('ignore::DeprecationWarning:koji')
    @pytest.mark.filterwarnings('ignore::PendingDeprecationWarning:koji')
    @pytest.mark.needs_metadata
    def test_generated_flatpak_files(self):
        container_yaml = _generate_flatpak('eog')

    @responses.activate
    @pytest.mark.needs_metadata
    @pytest.mark.parametrize(('search_term', 'extension', 'expected_error'),
                             [
                                 ('org.gnome.eog', 'yaml', None),
                                 ('org.gnome.eog', 'yml', None),
                                 ('org.gnome.eog', 'json', None),
                                 ('eYe of gNome', 'yaml', None),
                                 ('org.gnome', 'yaml',
                                  'Multiple matches found on flathub.org'),
                                 ('notexist', 'yaml',
                                  'No match found on flathub.org'),
                             ])
    def test_flatpak_from_flathub(self, search_term, extension,
                                  expected_error):
        responses.add(responses.GET, 'https://flathub.org/api/v1/apps',
                      body=APPS_JSON, content_type='application/json')
        responses.add(responses.GET, 'https://bodhi.fedoraproject.org/releases/?state=current',
                      body=RELEASES_JSON, content_type='application/json')

        app_id = 'org.gnome.eog'
        base = 'https://raw.githubusercontent.com/flathub'

        for ext, content_type, body in [
                ('yml', 'application/x-yaml', EOG_YAML),
                ('yaml', 'application/x-yaml', EOG_YAML),
                ('json', 'application/json', EOG_JSON)]:
            if extension == ext:
                responses.add(responses.GET,
                              f"{base}/{app_id}/master/{app_id}.{ext}",
                              body=body, content_type=content_type)
            else:
                responses.add(responses.GET,
                              f"{base}/{app_id}/master/{app_id}.{ext}",
                              body='Not found', status=404)

        if expected_error is None:
            container_yaml = \
                _generate_flatpak('eog',
                                  flathub=search_term,
                                  expected_error_output=expected_error)

            f = container_yaml['flatpak']

            assert f['id'] == 'org.gnome.eog'
            assert f['command'] == 'eog'
            assert f['runtime-name'] == 'flatpak-runtime'
            assert f['runtime-version'] == 'f38'
            assert f['rename-desktop-file'] == 'eog.desktop'
            assert f['rename-appdata-file'] == 'eog.appdata.xml'
            assert f['rename-icon'] == 'eog'
            assert f['copy-icon'] is True
            assert f['finish-args'] == '--share=ipc\n--socket=x11'
            assert f['packages'][0] == 'eog'
        else:
            _generate_flatpak('eog', flathub=search_term,
                                  expected_error_output=expected_error)
