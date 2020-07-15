import logging

import click

from .config import add_config_file, set_profile_name, get_profile
from .container_builder import ContainerBuilder
from .flatpak_builder import (FLATPAK_METADATA_ANNOTATIONS,
                              FLATPAK_METADATA_BOTH,
                              FLATPAK_METADATA_LABELS)
from .installer import Installer
from .module_builder import ModuleBuilder
from .utils import die

@click.group()
@click.option('-v', '--verbose', is_flag=True,
              help='Show verbose debugging output')
@click.option('-c', '--config', metavar='CONFIG_YAML', multiple=True,
              help='Additional configuration file to read')
@click.option('-p', '--profile', metavar='PROFILE_NAME', default='production',
              help='Alternate configuration profile to use')
def cli(verbose, config, profile):
    for c in reversed(config):
        add_config_file(c)

    set_profile_name(profile)
    try:
        get_profile()
    except KeyError:
        die("Unknown profile '{}'".format(profile))

    if verbose:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.basicConfig(level=logging.WARNING)


@cli.command(name="local-build")
@click.option('--add-local-build', metavar='BUILD_ID', multiple=True,
              help='include a local MBS module build  as a source for the build')
@click.option('--containerspec', metavar='CONTAINER_YAML', default='./container.yaml',
              help='path to container.yaml - defaults to ./container.yaml')
@click.option('--flatpak-metadata',
              type=click.Choice([FLATPAK_METADATA_LABELS,
                                 FLATPAK_METADATA_ANNOTATIONS,
                                 FLATPAK_METADATA_BOTH], case_sensitive=False),
              default=FLATPAK_METADATA_BOTH,
              help='How to store Flatpak metadata in the container')
@click.option('--modulemd', metavar='MODULEMD',
              help='path to modulemd file')
@click.option('--stream', metavar='STREAM',
              help='module stream for the build')
@click.option('--install', is_flag=True,
              help='automatically install Flatpak for the current user')
def local_build(add_local_build, containerspec, flatpak_metadata, modulemd, stream, install):
    """Build module locally, then build a container"""

    module_builder = ModuleBuilder(profile=get_profile(),
                                   modulemd=modulemd, stream=stream,
                                   local_builds=add_local_build)
    container_builder = ContainerBuilder(profile=get_profile(),
                                         containerspec=containerspec,
                                         local_builds=add_local_build,
                                         from_local=True,
                                         flatpak_metadata=flatpak_metadata)

    if (container_builder.module_spec.name != module_builder.name or
        container_builder.module_spec.stream != module_builder.stream):
        die("Module will be built as {}:{}, but container.yaml calls for {}"
            .format(module_builder.name, module_builder.stream,
                    container_builder.module_spec.to_str(include_profile=False)))

    module_builder.build()

    tarfile = container_builder.build()

    if install:
        installer = Installer(profile=get_profile())
        installer.set_source_path(tarfile)
        installer.install()


@cli.command(name="build-module")
@click.option('--add-local-build', metavar='BUILD_ID', multiple=True,
              help='include a local MBS module build  as a source for the build')
@click.option('--modulemd', metavar='MODULEMD',
              help='Path to modulemd file')
@click.option('--stream', metavar='STREAM',
              help='module stream for the build')
def build_module(add_local_build, modulemd, stream):
    """Build module locally"""

    module_builder = ModuleBuilder(profile=get_profile(),
                                   modulemd=modulemd, stream=stream,
                                   local_builds=add_local_build)
    module_builder.build()


@cli.command(name="build-container")
@click.option('--add-local-build', metavar='BUILD_ID', multiple=True,
              help='include a local MBS module build  as a source for the build')
@click.option('--from-local', is_flag=True,
              help='Use a local build for the module source listed in container.yaml ')
@click.option('--flatpak-metadata',
              type=click.Choice([FLATPAK_METADATA_LABELS,
                                 FLATPAK_METADATA_ANNOTATIONS,
                                 FLATPAK_METADATA_BOTH], case_sensitive=False),
              default=FLATPAK_METADATA_BOTH,
              help='How to store Flatpak metadata in the container')
@click.option('--containerspec', metavar='CONTAINER_YAML', default='./container.yaml',
              help='Path to container.yaml - defaults to ./container.yaml')
@click.option('--install', is_flag=True,
              help='automatically install Flatpak for the current user')
def build_container(add_local_build, from_local, flatpak_metadata, containerspec, install):
    """Build a container from local or remote module"""

    container_builder = ContainerBuilder(profile=get_profile(),
                                         containerspec=containerspec,
                                         local_builds=add_local_build,
                                         from_local=from_local,
                                         flatpak_metadata=flatpak_metadata)
    tarfile = container_builder.build()

    if install:
        installer = Installer(profile=get_profile())
        installer.set_source_path(tarfile)
        installer.install()


@cli.command()
@click.option('--koji', is_flag=True,
              help='Look up argument as NAME[:STREAM] in Koji')
@click.argument('path_or_url')
def install(koji, path_or_url):
    """Install a container as a Flatpak"""

    installer = Installer(profile=get_profile())
    if koji:
        installer.set_source_koji_name_stream(path_or_url)
    elif path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        installer.set_source_url(path_or_url)
    else:
        installer.set_source_path(path_or_url)

    installer.install()
