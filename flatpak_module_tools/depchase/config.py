import fnmatch
import logging
import os
import re

import yaml

from .util import dict_merge_deep, DefaultDictWithKey


log = logging.getLogger(__name__)


class ConfigParseError(Exception):

    def __init__(self, errors):
        self.errors = errors
        super().__init__()


class Config(dict):

    yaml_scalar_types = (str, int, float, bool)

    # standard configuration paths: /etc/fedmod, ~/.config/fedmod,
    # ../config relative to this file (for development)
    config_paths = (
        os.path.join(os.path.sep, 'etc', 'fedmod'),
        os.path.abspath(os.path.join(os.path.expanduser('~'),
                                     '.config', 'fedmod')),
        os.path.abspath(os.path.join(os.path.dirname(__file__),
                                     os.path.pardir, 'config')),
    )

    def __init__(self, config_paths=None):
        self.templates = DefaultDictWithKey(self._template_merge_bases)  # type: ignore
        self.releases = DefaultDictWithKey(self._release_expand)  # type: ignore

        if not config_paths:
            config_paths = self.config_paths

        conf_files = []
        for config_path in config_paths:
            conf_files_partial = []

            try:
                dir_files = os.listdir(config_path)
            except FileNotFoundError:
                # directory doesn't exist
                continue

            for f in sorted(dir_files):
                if fnmatch.fnmatch(f, '*.yaml'):
                    fpath = os.path.join(config_path, f)
                    # Use 'fedmod.yaml' as the base configuration to build
                    # upon, every other file in the same directory gets
                    # processed in alphabetical order.
                    if f != 'fedmod.yaml':
                        conf_files_partial.append(fpath)
                    else:
                        conf_files_partial.insert(0, fpath)
            conf_files.extend(conf_files_partial)

        parsed_configuration = {}
        for conf_file in conf_files:
            with open(conf_file, 'r') as f:
                try:
                    partial_conf = yaml.safe_load(f)
                except yaml.scanner.ScannerError:  # type: ignore
                    log.warn(f"Can't parse configuration file '{conf_file}' as"
                             " YAML, skipping.")
                    continue

                try:
                    self.verify_convert_conf_dict(partial_conf)
                except ConfigParseError as e:
                    log.warn(f"Problems in configuration file '{conf_file}':")
                    for error in e.errors:
                        log.warn(f"- {error}")
                    log.warn("--> skipping configuration file.")
                    continue

                parsed_configuration = dict_merge_deep(
                    parsed_configuration, partial_conf.get('data', {}))

        super().__init__(**parsed_configuration)

    def clear(self):
        super().clear()
        self.templates.clear()
        self.releases.clear()
        if hasattr(self, '_datasets'):
            del self._datasets

    def _template_merge_bases(self, name):
        templates = self.get('datasets', {}).get('templates', {})

        tmpl_merged = templates[name].copy()

        extends = tmpl_merged.get('extends')
        if extends:
            tmpl_merged = dict_merge_deep(self.templates[extends], tmpl_merged)

        return tmpl_merged

    def _release_expand(self, name):
        releases = self.get('datasets', {}).get('releases', {})

        rel_expanded = releases[name].copy()

        template = rel_expanded.get('template')
        if template:
            rel_expanded = dict_merge_deep(self.templates[template],
                                           rel_expanded)

        platform_module = rel_expanded.get('platform-module', {})
        platform_stream_tmpl = platform_module.get('stream-template')
        dataset_regex = rel_expanded.get('dataset-regex')

        if platform_stream_tmpl and dataset_regex:
            match = re.match(dataset_regex, name)
            if not match:
                raise ValueError(
                    f"Couldn't parse release name {name!r} with"
                    f" dataset-regex {dataset_regex!r}.")

            platform_module['stream-name'] = platform_stream_tmpl.format(
                **match.groupdict()
            )

        # The 'extends' key is only interesting concerning templates
        # themselves.
        try:
            del rel_expanded['extends']
        except KeyError:
            pass

        return rel_expanded

    def verify_convert_conf_dict(self, conf_dict):
        assert isinstance(conf_dict, dict)

        errors = []

        document = conf_dict.get('document')
        if document != 'fedmod-configuration':
            errors.append(f"Unknown or missing 'document' key: {document!r}")

        version = conf_dict.get('version')
        if version != 1:
            errors.append(f"Unknown or missing 'version' key: {version!r}")

        data = conf_dict.get('data')
        if not isinstance(data, dict):
            errors.append("Missing or invalid 'data' element, must be dict.")

        errors.extend(self._verify_convert_data_dict(data, 'data'))

        if errors:
            raise ConfigParseError(errors)

    @classmethod
    def _verify_convert_data_dict(cls, data, root):
        errors = []
        options = data.get('options')
        if options:
            if not isinstance(options, dict):
                errors.append(f"'{root}': 'options' must be a dict.")
            else:
                verbose = options.get('verbose')
                if verbose not in (None, True, False):
                    errors.append(f"'{root}/options': 'verbose' must be"
                                  " boolean.")

                dataset = options.get('dataset')
                if dataset and not isinstance(dataset, cls.yaml_scalar_types):
                    errors.append(f"'{root}/options': 'dataset' must be a"
                                  " scalar value.")

        datasets = data.get('datasets')
        if datasets:
            templates = datasets.get('templates')
            if templates:
                if not isinstance(templates, dict):
                    errors.append(f"'{root}': 'templates' must be a dict.")
                else:
                    errors.extend(cls._verify_convert_templates_dict(
                        templates, f'{root}/templates'))

            releases = datasets.get('releases')
            if releases:
                if not isinstance(releases, dict):
                    errors.append(f"'{root}': 'releases' must be a dict.")
                else:
                    for relname, reldata in releases.items():
                        repositories = reldata.get('repositories', {})
                        errors.extend(cls._verify_convert_repositories_dict(
                            repositories,
                            f'{root}/releases/{relname}/repositories'))

        return errors

    @classmethod
    def _verify_convert_templates_dict(cls, templates, root):
        errors = []

        for tname, tmpl in templates.items():
            tpath = f"{root}/{tname}"

            architectures = tmpl.get('architectures', [])
            if not (isinstance(architectures, list)
                    and all(isinstance(x, cls.yaml_scalar_types)
                            for x in architectures)):
                errors.append(f"'{tpath}': 'architectures' must be list of"
                              " scalars.")

            for key in ('dataset-regex', 'default-architecture', 'extends'):
                if key in tmpl and not isinstance(tmpl[key],
                                                  cls.yaml_scalar_types):
                    errors.append(f"'{tpath}': '{key}' must be a scalar"
                                  " value.")

            repositories = tmpl.get('repositories', {})
            if not isinstance(repositories, dict):
                errors.append(f"'{tpath}': 'repositories' must be a dict.")
            else:
                errors.extend(cls._verify_convert_repositories_dict(
                    repositories, f"{tpath}/repositories"))

        return errors

    @classmethod
    def _verify_convert_repositories_dict(cls, repositories, root):
        errors = []

        for rname, rtree in repositories.items():

            if not isinstance(rtree, dict):
                errors.append(f"'{root}/{rname}' must be a dict.")
                continue

            for rtype, rvals in rtree.items():
                rpath = f"{root}/{rname}/{rtype}"

                if isinstance(rvals, cls.yaml_scalar_types):
                    # interpret scalar repository value as baseurl
                    repositories[rname][rtype] = {'baseurl': rvals}
                elif not isinstance(rvals, dict):
                    errors.append(
                        f"'{rpath}': must be a scalar value or a dict.")
                else:
                    unknown_keys = set(rvals) - {'baseurl', 'metalink'}

                    if unknown_keys:
                        unknown_keys = (str(x) for x in unknown_keys)
                        errors.append(
                            f"'{rpath}': unknown key(s): {', '.join(unknown_keys)}"
                        )

        return errors

    @property
    def datasets(self):
        if not hasattr(self, '_datasets'):
            release_names = sorted(
                self.get('datasets', {}).get('releases', {}))

            self._datasets = []
            for name in release_names:
                self._datasets.append(name)
                self._datasets.extend(
                    f"{name}-{a}"
                    for a in sorted(
                        self.releases[name].get('architectures', ())))

        return self._datasets


config = Config()
runtime_config = {}
