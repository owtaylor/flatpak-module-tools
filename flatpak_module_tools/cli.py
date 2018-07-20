import logging

import click

from .container_builder import ContainerBuilder
from .installer import Installer
from .module_builder import ModuleBuilder
from .utils import die

@click.group()
@click.option('-v', '--verbose', is_flag=True,
              help='Show verbose debugging output')
def cli(verbose):
    if verbose:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.basicConfig(level=logging.WARNING)


@cli.command(name="local-build")
@click.option('--add-local-build', metavar='BUILD_ID', multiple=True,
              help='include a local MBS module build  as a source for the build')
@click.option('--containerspec', metavar='CONTAINER_YAML', default='./container.yaml',
              help='path to container.yaml - defaults to ./container.yaml')
@click.option('--modulemd', metavar='MODULEMD',
              help='path to modulemd file')
@click.option('--stream', metavar='STREAM',
              help='module stream for the build')
@click.option('--install', is_flag=True,
              help='automatically install Flatpak for the current user')
def local_build(add_local_build, containerspec, modulemd, stream, install):
    """Build module locally, then build a container"""

    module_builder = ModuleBuilder(modulemd=modulemd, stream=stream,
                                   local_builds=add_local_build)
    container_builder = ContainerBuilder(containerspec=containerspec,
                                         local_builds=add_local_build,
                                         from_local=True)

    if (container_builder.module_spec.name != module_builder.name or
        container_builder.module_spec.stream != module_builder.stream):
        die("Module will be built as {}:{}, but container.yaml calls for {}"
            .format(module_builder.name, module_builder.stream,
                    container_builder.module_spec.to_str(include_profile=False)))

    module_builder.build()

    tarfile = container_builder.build()

    if install:
        installer = Installer(path=tarfile)
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

    module_builder = ModuleBuilder(modulemd=modulemd, stream=stream,
                                   local_builds=add_local_build)
    module_builder.build()


@cli.command(name="build-container")
@click.option('--add-local-build', metavar='BUILD_ID', multiple=True,
              help='include a local MBS module build  as a source for the build')
@click.option('--from-local', is_flag=True,
              help='Use a local build for the module source listed in container.yaml ')
@click.option('--staging', is_flag=True,
              help='Use builds from Fedora staging infrastructure')
@click.option('--containerspec', metavar='CONTAINER_YAML', default='./container.yaml',
              help='Path to container.yaml - defaults to ./container.yaml')
@click.option('--install', is_flag=True,
              help='automatically install Flatpak for the current user')
def build_container(add_local_build, from_local, staging, containerspec, install):
    """Build a container from local or remote module"""

    container_builder = ContainerBuilder(containerspec=containerspec,
                                         local_builds=add_local_build,
                                         from_local=from_local,
                                         staging=staging)
    tarfile = container_builder.build()

    if install:
        installer = Installer(path=tarfile)
        installer.install()


@cli.command()
@click.argument('path_or_url')
def install(path_or_url):
    """Install a container as a Flatpak"""

    installer = Installer(path=path_or_url)
    installer.install()
