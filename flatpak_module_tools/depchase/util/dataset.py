# dataset: utility functions for dataset names

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config


# can't import config here because it would introduce a circular dependency
config: "Config" = None  # type: ignore


def _setup_module():
    global config
    if not config:
        from ..config import config


def get_default_dataset() -> str:
    _setup_module()
    dataset = config.get('options', {}).get('dataset')
    assert isinstance(dataset, str)
    return dataset


def parse_dataset_name(dataset_name=None):
    """Parse dataset_name as distro_release[-arch] and validate the result"""
    _setup_module()

    if dataset_name is None:
        dataset_name = get_default_dataset()

    if dataset_name in config.releases:
        release_name = dataset_name
        arch = None
    else:
        parts = dataset_name.rsplit('-', 1)
        if len(parts) == 1:
            release_name, arch = parts[0], None
        else:
            release_name, arch = parts[0], parts[1]

    try:
        dataset_config = config.releases[release_name]
    except KeyError:
        # Use dataset_name because release_name contains only part of the
        # string at this point.
        raise ValueError("Unknown dataset/release name: {}. Known releases: {}"
                         .format(dataset_name, ", ".join(
                             sorted(config['datasets']['releases']))))
    if not arch:
        arch = dataset_config.get('default-architecture', 'x86_64')

    architectures = dataset_config.get('architectures')
    if architectures and arch not in architectures:
        raise ValueError("Unknown architecture: {arch}. Known architectures: {architectures}")

    return release_name, arch


def display_dataset_name(dataset_name):
    _setup_module()

    release_name, arch = parse_dataset_name(dataset_name)
    if (release_name, arch) == parse_dataset_name(get_default_dataset()):
        return None
    elif arch == config.releases[release_name]['default-architecture']:
        return release_name
    else:
        return dataset_name
