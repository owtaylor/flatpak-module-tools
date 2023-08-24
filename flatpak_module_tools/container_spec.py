from enum import Enum
from typing import Any, List, Literal, overload, Optional, Union
import yaml

from flatpak_module_tools.utils import Arch


class Option(Enum):
    REQUIRED = 1


class ValidationError(Exception):
    pass


class BaseSpec:
    def __init__(self, path, yaml_dict):
        self.path = path
        self._yaml_dict = yaml_dict

    def _get(self, key: str, type_convert, default: Any = Option.REQUIRED):
        val = self._yaml_dict.get(key)
        if val is None:
            if default == Option.REQUIRED:
                raise ValidationError(f"{self.path}, {key} is missing")
            else:
                return default
        else:
            try:
                return type_convert(val)
            except ValueError as e:
                raise ValidationError(f"{self.path}, {key} {e}")

    @overload
    def _get_str(self, key: str, default: Literal[Option.REQUIRED]) -> str:
        ...

    @overload
    def _get_str(self, key: str) -> str:
        ...

    @overload
    def _get_str(self, key: str, default: str) -> str:
        ...

    @overload
    def _get_str(self, key: str, default: None) -> Optional[str]:
        ...

    def _get_str(
            self, key: str, default: Union[Literal[Option.REQUIRED], str, None] = None
    ) -> Optional[str]:
        def type_convert(val):
            if isinstance(val, (str, int, float)):
                return str(val)
            else:
                raise ValidationError(f"{self.path}, {key} must be a string")

        return self._get(key, type_convert, default)

    @overload
    def _get_bool(self, key: str, default: Literal[Option.REQUIRED]) -> bool:
        ...

    @overload
    def _get_bool(self, key: str, default: bool) -> bool:
        ...

    @overload
    def _get_bool(self, key: str, default: None) -> Optional[bool]:
        ...

    def _get_bool(
            self, key: str, default: Union[Literal[Option.REQUIRED], bool, None]
    ) -> Optional[bool]:
        def type_convert(val):
            if isinstance(val, bool):
                return val
            else:
                raise ValidationError(f"{self.path}, {key} must be a boolean")

        return self._get(key, type_convert, default)

    @overload
    def _get_str_list(self, key: str,
                      default: Literal[Option.REQUIRED], allow_scalar=False) -> List[str]:
        ...

    @overload
    def _get_str_list(self, key: str, default: List[str], allow_scalar=False) -> List[str]:
        ...

    @overload
    def _get_str_list(self, key: str, default: None, allow_scalar=False) -> Optional[List[str]]:
        ...

    def _get_str_list(
            self, key: str, default: Union[Literal[Option.REQUIRED], List[str], None],
            allow_scalar=False
    ) -> Optional[List[str]]:
        def type_convert(val):
            if isinstance(val, List) and all(isinstance(v, (int, float, str)) for v in val):
                return [
                    str(v) for v in val
                ]
            elif allow_scalar and isinstance(val, (int, float, str)):
                return [str(val)]
            else:
                raise ValidationError(f"{self.path}, {key} must be a list of strings")

        return self._get(key, type_convert, default)


class PlatformsSpec(BaseSpec):
    def __init__(self, path, platforms_yaml):
        super().__init__(path, platforms_yaml)
        self.only = self._get_str_list('only', [], allow_scalar=True)
        self.not_ = self._get_str_list('not', [], allow_scalar=True)

    def includes_platform(self, platform: str):
        return (not self.only or platform in self.only) and \
            (not self.not_ or platform not in self.not_)


class PackageSpec(BaseSpec):
    def __init__(self, path, yaml_object):
        if isinstance(yaml_object, str):
            self.name = yaml_object
            self.platforms = None
        else:
            super().__init__(path, yaml_object)

            self.name = self._get_str("name")
            platforms_yaml = yaml_object.get("platforms")
            if platforms_yaml:
                self.platforms = PlatformsSpec(f"{path}/platforms", platforms_yaml)
            else:
                self.platforms = None


class FlatpakSpec(BaseSpec):
    def _get_package_list(self, key, default) -> List["PackageSpec"]:
        def type_convert(val):
            if isinstance(val, List) and all(isinstance(v, (str, dict)) for v in val):
                return [
                    PackageSpec(f"{self.path}/{i}", v) for i, v in enumerate(val)
                ]
            else:
                raise ValidationError(f"{self.path}, {key} must be a list of strings and mappings")

        return self._get(key, type_convert, default)

    def __init__(self, path, flatpak_yaml):
        super().__init__(path, flatpak_yaml)

        self.app_id = self._get_str("id")
        self.appdata_license = self._get_str('appdata-license', None)
        self.appstream_compose = self._get_bool('appstream-compose', True)
        self.base_image = self._get_str('base_image', None)
        self.branch = self._get_str('branch', 'stable')
        self.build_runtime = self._get_bool('build-runtime', False)
        self.cleanup_commands = self._get_str('cleanup-commands', None)
        self.command = self._get_str('command', None)
        self.component = self._get_str('component', None)
        self.copy_icon = self._get_bool('copy-icon', False)
        self.desktop_file_name_prefix = self._get_str('desktop-file-name-prefix', None)
        self.desktop_file_name_suffix = self._get_str('desktop-file-name-suffix', None)
        self.end_of_life = self._get_str('end-of-life', None)
        self.end_of_life_rebase = self._get_str('end-of-life-rebase', None)
        self.finish_args = self._get_str('finish-args', None)
        self.name = self._get_str('name', None)
        self.packages = self._get_package_list('packages', [])
        self.rename_appdata_file = self._get_str('rename-appdata-file', None)
        self.rename_desktop_file = self._get_str('rename-desktop-file', None)
        self.rename_icon = self._get_str('rename-icon', None)
        self.runtime = self._get_str('runtime', None)
        self.runtime_name = self._get_str('runtime-name', None)
        self.runtime_version = self._get_str('runtime-version', None)
        self.sdk = self._get_str('sdk', None)
        self.tags = self._get_str_list('tags', [])

    def get_packages_for_arch(self, arch: Arch):
        return [
            package.name for package in self.packages
            if not package.platforms or package.platforms.includes_platform(arch.rpm)
        ]

    def get_component_label(self, fallback_name: str):
        # Return the com.redhat.component label - which is the "name" of the
        # NVR
        if self.component:
            return self.component
        else:
            return (self.name or fallback_name) + "-flatpak"

    def get_name_label(self, fallback_component: str):
        # Return the name label - this is used for which container repository
        # to push to. This basically reverses get_component() and is useful
        # when we want to go from NVR to name label.
        if self.name:
            return self.name
        else:
            if fallback_component.endswith("-flatpak"):
                return fallback_component[:-8]
            else:
                return fallback_component


class ComposeSpec(BaseSpec):
    def __init__(self, path, compose_yaml):
        super().__init__(path, compose_yaml)
        self.modules = self._get_str_list('modules', [])


class ContainerSpec(BaseSpec):
    def __init__(self, path):
        with open(path) as f:
            container_yaml = yaml.safe_load(f)

        super().__init__(path, container_yaml)

        flatpak_yaml = container_yaml.get('flatpak', None)
        if not flatpak_yaml:
            raise ValidationError(f"No flatpak section in '{path}'")

        self.flatpak = FlatpakSpec(f"{path}:flatpak", flatpak_yaml)

        compose_yaml = container_yaml.get('compose', {})
        self.compose = ComposeSpec(f"{path}:compose", compose_yaml)

        platforms_yaml = container_yaml.get('compose', {})
        self.platforms = PlatformsSpec(f"{path}:platforms", platforms_yaml)

        NEW_STYLE_ATTRS = ["packages", "runtime_name", "runtime_version"]
        NEW_STYLE_ATTRS_RUNTME = ["packages", "name", "branch"]

        if self.compose.modules:
            set_attrs = [a for a in NEW_STYLE_ATTRS if getattr(self.flatpak, a)]
            if set_attrs:
                raise ValidationError(
                    f"{path} is old style (compose:modules is set). Disallowed keys:\n" +
                    "\n".join(
                        f"    flatpak:{a.replace('_', '-')}" for a in set_attrs
                    )
                )
        else:
            if self.flatpak.build_runtime:
                required_attrs = NEW_STYLE_ATTRS_RUNTME
            else:
                required_attrs = NEW_STYLE_ATTRS

            unset_attrs = [a for a in required_attrs if not getattr(self.flatpak, a)]
            if unset_attrs:
                raise ValidationError(
                    f"{path} is new style (compose:modules is not set). Missing keys:\n" +
                    "\n".join(
                        f"    flatpak:{a.replace('_', '-')}" for a in unset_attrs
                    )
                )
