import json
import os
import re
import sys
from textwrap import dedent

import click
import requests
import yaml


# Some PyYAML magic to get the output we want for container.yaml

class LiteralScalar(str):
    """String subclass that gets dumped into yaml as a scalar"""
    pass


def _represent_literal_scalar(dumper, s):
    return dumper.represent_scalar(tag=u'tag:yaml.org,2002:str',
                                   value=s,
                                   style='|')


yaml.add_representer(LiteralScalar, _represent_literal_scalar)


class NoSortMapping(dict):
    """dict subclass that dumped into yaml as a scalar without sorting keys"""
    pass


def _represent_no_sort_mapping(dumper, d):
    return yaml.MappingNode(tag='tag:yaml.org,2002:map',
                            value=[(dumper.represent_data(k),
                                    dumper.represent_data(v))
                                   for k, v in d.items()],
                            flow_style=False)


yaml.add_representer(NoSortMapping, _represent_no_sort_mapping)


def _load_flathub_manifest(search_term):
    response = requests.get("https://flathub.org/api/v1/apps")
    response.raise_for_status()
    apps = response.json()

    matches = []
    search_lower = search_term.lower()
    for app in apps:
        if (search_lower in app['flatpakAppId'].lower() or
                search_lower in app['name'].lower()):
            matches.append((app['flatpakAppId'], app['name']))

    if len(matches) > 1:
        max_id_len = max([len(app_id) for app_id, _ in matches])
        for app_id, name in matches:
            print(app_id + (' ' * (max_id_len - len(app_id)) + ' ' + name))
        raise click.ClickException("Multiple matches found on flathub.org")
    elif len(matches) == 0:
        raise click.ClickException("No match found on flathub.org")

    app_id = matches[0][0]

    for fname, is_yaml in [
            (f"{app_id}.json", False),
            (f"{app_id}.yaml", True),
            (f"{app_id}.yml", True)]:
        url = f"https://raw.githubusercontent.com/flathub/{app_id}/master/{fname}"
        response = requests.get(url)
        if response.status_code == 404:
            continue
        else:
            break

    response.raise_for_status()

    if is_yaml:
        return yaml.safe_load(response.text)
    else:
        # flatpak-builder supports non-standard comments in the manifest, strip
        # them out. (Ignore the possibility of C comments embedded in strings.)
        #
        # Regex explanation: matches /*<something>*/ (multiline)
        #    <something> DOES NOT contains "/*" substring
        no_comments = re.sub(r'/\*((?!/\*).)*?\*/', '', response.text, flags=re.DOTALL)
        return json.loads(no_comments)


class FlatpakGenerator(str):
    def __init__(self, pkg):
        self.pkg = pkg

    def _flathub_container_yaml(self, manifest, runtime_name, runtime_version):
        app_id = manifest.get('app-id')
        if app_id is None:
            app_id = manifest['id']
        yml = NoSortMapping({
            'flatpak': NoSortMapping({
                'id': app_id,
                'branch': 'stable',
                'runtime-name': runtime_name,
                'runtime-version': 'f' + str(runtime_version),
            })
        })

        yml['flatpak']['packages'] = [self.pkg]

        for key in ['command',
                    'appstream-license',
                    'appstream-compose',
                    'desktop-file-name-prefix',
                    'desktop-file-name-suffix',
                    'rename-appdata-file',
                    'rename-desktop-file',
                    'rename-icon',
                    'copy-icon']:
            if key in manifest:
                yml['flatpak'][key] = manifest[key]

        if 'finish-args' in manifest:
            yml['flatpak']['finish-args'] = LiteralScalar('\n'.join(manifest['finish-args']))

        return yaml.dump(yml, default_flow_style=False, indent=4)

    def _default_container_yaml(self, runtime_name, runtime_version):
        pkg = self.pkg
        command = pkg
        branch = 'f' + str(runtime_version)

        container_yaml = dedent(f'''\
            flatpak:
                # Derived from the project's domain name
                id: org.example.MyApp
                branch: stable
                runtime-name: {runtime_name}
                runtime-version: {branch}
                # RPM package(s) to install, main package first
                packages:
                - {pkg}
                # Binary to execute to run the app
                command: {command}
                # Not sandboxed. See 'man flatpak-build-finish'
                finish-args: |-
                    --device=dri
                    --filesystem=host
                    --share=ipc
                    --socket=x11
                    --socket=wayland
                    --socket=session-bus
            ''')

        return container_yaml

    def _write_container_yaml(self, output_fname, flathub_manifest, runtime_name, runtime_version):
        if flathub_manifest:
            container_yaml = self._flathub_container_yaml(flathub_manifest, runtime_name, runtime_version)
        else:
            container_yaml = self._default_container_yaml(runtime_name, runtime_version)

        with open(output_fname, 'w') as f:
            f.write(container_yaml)

        print(f"Generated container specification: {output_fname!r}."
              f" Please edit appropriately.")

    def run(self, output_containerspec,
            force=False, flathub=None, runtime_name=None, runtime_version=None):
        flathub_manifest = _load_flathub_manifest(flathub) if flathub else None

        if output_containerspec is None:
            output_containerspec = 'container.yaml'

        if not force:
            if os.path.exists(output_containerspec):
                raise click.ClickException(f"{output_containerspec} exists."
                                           f" Pass --force to overwrite.")

        if runtime_name is None:
            runtime_name = 'flatpak-runtime'

        if runtime_version is None:
            response = requests.get("https://bodhi.fedoraproject.org/releases/?state=current")
            response.raise_for_status()

            runtime_version = max(int(
                r["version"])
                    for r in response.json()["releases"]
                    if r["id_prefix"] == "FEDORA-FLATPAK"
            )

        self._write_container_yaml(output_containerspec, flathub_manifest, runtime_name, runtime_version)
