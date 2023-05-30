from enum import Enum
from typing import Any, List, Literal, overload
import yaml

from .utils import die


class Option(Enum):
    REQUIRED = 1


class BaseSpec:
    def __init__(self, path, yaml_dict):
        self.path = path
        self._yaml_dict = yaml_dict

    def _get(self, key: str, type_convert, default: Any = Option.REQUIRED):
        val = self._yaml_dict.get(key)
        if val is None:
            if default == Option.REQUIRED:
                die(f"{self.path}, {key} is missing")
            else:
                return default
        else:
            try:
                return type_convert(val)
            except ValueError as e:
                die(f"{self.path}, {key} {e}")

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
    def _get_str(self, key: str, default: None) -> str | None:
        ...

    def _get_str(
            self, key: str, default: Literal[Option.REQUIRED] | str | None = None
    ) -> str | None:
        def type_convert(val):
            if isinstance(val, (str, int, float)):
                return str(val)
            else:
                die(f"{self.path}, {key} must be a string")

        return self._get(key, type_convert, default)

    @overload
    def _get_bool(self, key: str, default: Literal[Option.REQUIRED]) -> bool:
        ...

    @overload
    def _get_bool(self, key: str, default: bool) -> bool:
        ...

    @overload
    def _get_bool(self, key: str, default: None) -> bool | None:
        ...

    def _get_bool(self, key: str, default: Literal[Option.REQUIRED] | bool | None) -> bool | None:
        def type_convert(val):
            if isinstance(val, bool):
                return val
            else:
                die(f"{self.path}, {key} must be a boolean")

        return self._get(key, type_convert, default)

    @overload
    def _get_str_list(self, key: str,
                      default: Literal[Option.REQUIRED], allow_scalar=False) -> List[str]:
        ...

    @overload
    def _get_str_list(self, key: str, default: List[str], allow_scalar=False) -> List[str]:
        ...

    @overload
    def _get_str_list(self, key: str, default: None, allow_scalar=False) -> List[str] | None:
        ...

    def _get_str_list(
            self, key: str, default: Literal[Option.REQUIRED] | List[str] | None,
            allow_scalar=False
    ) -> List[str] | None:
        def type_convert(val):
            if isinstance(val, List) and all(isinstance(v, (int, float, str)) for v in val):
                return [
                    str(v) for v in val
                ]
            elif allow_scalar and isinstance(val, (int, float, str)):
                return [str(val)]
            else:
                die(f"{self.path}, {key} must be a list of strings")

        return self._get(key, type_convert, default)


class FlatpakSpec(BaseSpec):
    def __init__(self, path, flatpak_yaml):
        super().__init__(path, flatpak_yaml)

        self.app_id = self._get_str("id")
        self.appdata_license = self._get_str('appdata-license', None)
        self.appstream_compose = self._get_bool('appstream-compose', True)
        self.branch = self._get_str('branch', 'stable')
        self.build_runtime = self._get_bool('build-runtime', False)
        self.cleanup_commands = self._get_str('cleanup_commands', None)
        self.command = self._get_str('command', None)
        self.component = self._get_str('component', None)
        self.copy_icon = self._get_bool('copy-icon', False)
        self.desktop_file_name_prefix = self._get_str('desktop-file-name-prefix', None)
        self.desktop_file_name_suffix = self._get_str('desktop-file-name-suffix', None)
        self.end_of_life = self._get_str('end-of-life', None)
        self.end_of_life_rebase = self._get_str('end-of-life-rebase', None)
        self.finish_args = self._get_str('finish-args', None)
        self.name = self._get_str('name', None)
        self.packages = self._get_str_list('packages', [])
        self.rename_appdata_file = self._get_str('rename-appdata-file', None)
        self.rename_desktop_file = self._get_str('rename-desktop-file', None)
        self.rename_icon = self._get_str('rename-icon', None)
        self.runtime = self._get_str('runtime', None)
        self.runtime_name = self._get_str('runtime-name', None)
        self.runtime_version = self._get_str('runtime-version', None)
        self.sdk = self._get_str('sdk', None)
        self.tags = self._get_str_list('tags', [])


class ComposeSpec(BaseSpec):
    def __init__(self, path, compose_yaml):
        super().__init__(path, compose_yaml)
        self.modules = self._get_str_list('modules', [])


class PlatformsSpec(BaseSpec):
    def __init__(self, path, platforms_yaml):
        super().__init__(path, platforms_yaml)
        self.only = self._get_str_list('only', [], allow_scalar=True)
        self.not_ = self._get_str_list('not', [], allow_scalar=True)


class ContainerSpec(BaseSpec):
    def __init__(self, path):
        with open(path) as f:
            container_yaml = yaml.safe_load(f)

        super().__init__(path, container_yaml)

        flatpak_yaml = container_yaml.get('flatpak', None)
        if not flatpak_yaml:
            die(f"No flatpak section in '{path}'")

        self.flatpak = FlatpakSpec(f"{path}:flatpak", flatpak_yaml)

        compose_yaml = container_yaml.get('compose', {})
        self.compose = ComposeSpec(f"{path}:compose", compose_yaml)

        platforms_yaml = container_yaml.get('compose', {})
        self.platforms = PlatformsSpec(f"{path}:platforms", platforms_yaml)

        NEW_STYLE_ATTRS = ["packages", "runtime_name", "runtime_version"]

        if self.compose.modules:
            set_attrs = [a for a in NEW_STYLE_ATTRS if getattr(self.flatpak, a) is not None]
            if set_attrs:
                die(
                    f"{path} is old style (compose:modules is set). Disallowed keys:\n" +
                    "\n".join(
                        f"    flatpak:{a.replace('_', '-')}" for a in set_attrs
                    )
                )
        else:
            unset_attrs = [a for a in NEW_STYLE_ATTRS if getattr(self.flatpak, a) is None]
            if unset_attrs:
                die(
                    f"{path} is old style (compose:modules is not set). Missing keys:\n" +
                    "\n".join(
                        f"    flatpak:{a.replace('_', '-')}" for a in unset_attrs
                    )
                )

        if self.compose.modules and self.flatpak.packages:
            die("{path}: Both compose:modules (deprecated) and flatpak:packages are set")
