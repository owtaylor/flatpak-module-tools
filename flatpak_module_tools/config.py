import os

import pkg_resources
import yaml


_extra_config_files = []
def add_config_file(self, config_file):
    _extra_config_files.append(config_file)


_config = None
def get_config():
    global _config
    if _config is None:
        _config = Config()
        _config.read()

    return _config


_profile_name = None
def set_profile_name(profile_name):
    global _profile_name
    _profile_name = profile_name


def get_profile():
    return get_config().profiles[_profile_name]


class ProfileConfig(object):
    config_keys = [
        'base_repo_url',
        'koji_config',
        'koji_profile',
        'mbs_config_file',
        'mbs_config_section',
        'platform_stream_pattern',
    ]

    def __init__(self):
        for k in self.config_keys:
            setattr(self, k, None)

    def merge(self, yml):
        for k in self.config_keys:
            v = yml.get(k)
            if v is not None:
                setattr(self, k, v)

    def setdefaults(self, other):
        for k in self.config_keys:
            if getattr(self, k) is None:
                setattr(self, k, getattr(other, k))


class Config(object):
    def __init__(self):
        self.profiles = {}

    def _read_config_file(self, config_file):
        if isinstance(config_file, str):
            try:
                with open(config_file) as f:
                    yml = yaml.safe_load(f)
            except OSError:
                return
        else:
            yml = yaml.safe_load(config_file)

        for profile, profile_yml in yml['profiles'].items():
            if profile not in self.profiles:
                self.profiles[profile] = ProfileConfig()
            self.profiles[profile].merge(profile_yml)

    def read(self):
        config_files = []

        try:
            config_files += [
                pkg_resources.resource_stream('flatpak_module_tools', 'config.yaml')
            ]
        except:
            pass

        user_config = os.environ.get('XDG_CONFIG_HOME',
                                     os.path.expanduser('~/.config'))
        config_files += [
            '/etc/flatpak-module/config.yaml',
            '/etc/flatpak-module/config.d/',
            os.path.join(user_config, 'flatpak-module/config.yaml'),
            os.path.join(user_config, 'flatpak-module/config.d/'),
        ]

        config_files += _extra_config_files

        for config_file in config_files:
            if isinstance(config_file, str) and config_file.endswith('/'):
                try:
                    files = os.listdir(config_file)
                except OSError:
                    continue
                for f in sorted(files, reverse=True):
                    if f.endswith('yaml'):
                        self._read_config_file(os.path.join(config_file, f))
            else:
                self._read_config_file(config_file)

        defaults = self.profiles.get('__default__')
        if defaults is not None:
            for t in self.profiles.values():
                if t is not defaults:
                    t.setdefaults(defaults)
