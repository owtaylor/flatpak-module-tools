from typing import Iterable

import jinja2

from .utils import Arch


def make_mock_cfg(
        *,
        arch: Arch,
        chroot_setup_cmd: str,
        includepkgs: Iterable[str],
        releasever: str,
        repos: Iterable[str],
        root_cache_enable: bool,
        runtimever: str
):
    env = jinja2.Environment(
        loader=jinja2.PackageLoader('flatpak_module_tools', 'templates'),
        autoescape=False
    )
    template = env.get_template('mock.cfg.j2')
    return template.render(
        arch=arch.rpm,
        chroot_setup_cmd=chroot_setup_cmd,
        includepkgs=includepkgs,
        releasever=releasever,
        repos=repos,
        root_cache_enable=root_cache_enable,
        runtimever=runtimever
    )
