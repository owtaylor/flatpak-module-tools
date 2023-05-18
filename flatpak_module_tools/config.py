from functools import cached_property
import os
from typing import Optional

import koji
import pkg_resources
import re
import yaml

from .utils import get_arch


_extra_config_files = []


def add_config_file(config_file):
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


def get_profile() -> "ProfileConfig":
    return get_config().profiles[_profile_name]


class ProfileConfig:
    koji_config: Optional[str]
    koji_profile: Optional[str]

    config_keys = [
        'base_repo_url',
        'release_name',
        'koji_config',
        'koji_profile',
        'rpm_koji_target',
        'flatpak_koji_target',
    ]

    def __init__(self, name):
        self.name = name
        for k in self.config_keys:
            setattr(self, k, None)

        self._koji_session: koji.ClientSession | None = None

    def merge(self, yml):
        for k in self.config_keys:
            v = yml.get(k)
            if v is not None:
                setattr(self, k, v)

    def setdefaults(self, other):
        for k in self.config_keys:
            if getattr(self, k) is None:
                setattr(self, k, getattr(other, k))

    def get_base_repo_url(self, release, arch=None):
        if arch is None:
            arch = get_arch()

        result = getattr(self, "base_repo_url") \
            .replace("$release", release) \
            .replace("$basearch", get_arch().rpm)

        if not result.endswith("/"):
            result += "/"

        return result

    def get_release_name(self, release):
        return getattr(self, "release_name").replace("$release", release)

    def release_from_runtime_version(self, runtime_version: str):
        return re.sub(r'^[^\d]+', '', runtime_version)

    def get_rpm_koji_target(self, release):
        return getattr(self, "rpm_koji_target").replace("$release", release)

    def get_flatpak_koji_target(self, release):
        return getattr(self, "flatpak_koji_target").replace("$release", release)

    @cached_property
    def koji_options(self):
        assert self.koji_profile is not None
        return koji.read_config(
            profile_name=self.koji_profile, user_config=self.koji_config
        )

    @cached_property
    def koji_session(self):
        session_opts = koji.grab_session_options(self.koji_options)
        return koji.ClientSession(self.koji_options['server'], session_opts)


class Config:
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
                self.profiles[profile] = ProfileConfig(profile)
            self.profiles[profile].merge(profile_yml)

    def read(self):
        config_files = []

        try:
            config_files += [
                pkg_resources.resource_stream('flatpak_module_tools', 'config.yaml')
            ]
        except Exception:
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
