from textwrap import dedent

import pytest
from flatpak_module_tools.container_spec import ContainerSpec, ValidationError
from flatpak_module_tools.utils import Arch


APP_CONTAINER_YAML = """\
flatpak:
    appdata-license: GPL-3.0-or-later AND CC0-1.0
    appstream-compose: False
    branch: unstable
    cleanup-commands: |
        GDK_PIXBUF_MODULEDIR=/app/lib64/gdk-pixbuf-2.0/2.10.0/loaders/ \
            gdk-pixbuf-query-loaders-64 > /app/lib64/gdk-pixbuf-2.0/2.10.0/loaders.cache
        gdk-pixbuf-query-loaders-64 >> /app/lib64/gdk-pixbuf-2.0/2.10.0/loaders.cache
    command: eog
    component: eog-flatpak-container
    copy-icon: True
    desktop-file-name-prefix: org.gnome.
    desktop-file-name-suffix: .desktop
    end-of-life: "Too old"
    end-of-life-rebase: org.gnome.EyeOfNewt
    finish-args: >-
        --share=ipc
        --socket=fallback-x11
        --socket=wayland
    id: org.gnome.eog
    name: eog
    packages:
    - eog
    -   name: libjpeg-superfast
        platforms:
            only: x86_64
    rename-appdata-file: eog.appdata.xml
    rename-desktop-file: eog.desktop
    rename-icon: eog.png
    runtime-name: flatpak-runtime
    runtime-version: f39
    tags: ["Image Viewer", "Eye of GNOME"]
platforms:
    only: x86_64, aarch64
    not: aarch64
"""

APP_CONTAINER_YAML = """\
flatpak:
    appdata-license: GPL-3.0-or-later AND CC0-1.0
    appstream-compose: False
    branch: unstable
    cleanup-commands: |
        GDK_PIXBUF_MODULEDIR=/app/lib64/gdk-pixbuf-2.0/2.10.0/loaders/ \
            gdk-pixbuf-query-loaders-64 > /app/lib64/gdk-pixbuf-2.0/2.10.0/loaders.cache
        gdk-pixbuf-query-loaders-64 >> /app/lib64/gdk-pixbuf-2.0/2.10.0/loaders.cache
    command: eog
    component: eog-flatpak-container
    copy-icon: True
    desktop-file-name-prefix: org.gnome.
    desktop-file-name-suffix: .desktop
    end-of-life: "Too old"
    end-of-life-rebase: org.gnome.EyeOfNewt
    finish-args: >-
        --share=ipc
        --socket=fallback-x11
        --socket=wayland
    id: org.gnome.eog
    name: eog
    packages:
    - eog
    -   name: libtastypng
    -   name: libjpeg-superfast
        platforms:
            only: x86_64
    rename-appdata-file: eog.appdata.xml
    rename-desktop-file: eog.desktop
    rename-icon: eog.png
    runtime-name: flatpak-runtime
    runtime-version: f39
    tags: ["Image Viewer", "Eye of GNOME"]
platforms:
    only: ["x86_64", "aarch64"]
    not: aarch64
"""

APP_CONTAINER_YAML_MODULES = """\
compose:
    modules:
    - eog:stable
flatpak:
    id: org.gnome.eog
    base_image: f39/flatpak-runtime:latest
    branch: stable
    command: eog
"""

RUNTIME_CONTAINER_YAML = """\
flatpak:
    id: org.fedoraproject.Platform
    build-runtime: true
    name: f39/flatpak-runtime
    component: flatpak-runtime
    branch: f39
    sdk: org.fedoraproject.Sdk
    finish-args: >-
        --env=GI_TYPELIB_PATH=/app/lib64/girepository-1.0
        --env=GST_PLUGIN_SYSTEM_PATH=/app/lib64/gstreamer-1.0:/usr/lib64/gstreamer-1.0
    cleanup-commands: |
        mv -f /usr/bin/flatpak-xdg-email /usr/bin/xdg-email
        mv -f /usr/bin/flatpak-xdg-open /usr/bin/xdg-open
    packages:
    - abattis-cantarell-fonts
    - abattis-cantarell-vf-fonts
"""


def make_spec(tmp_path, container_yaml):
    with open(tmp_path / "container.yaml", "w") as f:
        f.write(container_yaml)

    return ContainerSpec(tmp_path / "container.yaml")


def test_app_container_spec(tmp_path):
    spec = make_spec(tmp_path, APP_CONTAINER_YAML)
    assert spec.flatpak.app_id == "org.gnome.eog"
    assert spec.flatpak.appdata_license == "GPL-3.0-or-later AND CC0-1.0"
    assert spec.flatpak.appstream_compose is False
    assert spec.flatpak.branch == "unstable"
    assert spec.flatpak.build_runtime is False
    assert spec.flatpak.cleanup_commands == dedent("""\
        GDK_PIXBUF_MODULEDIR=/app/lib64/gdk-pixbuf-2.0/2.10.0/loaders/ \
            gdk-pixbuf-query-loaders-64 > /app/lib64/gdk-pixbuf-2.0/2.10.0/loaders.cache
        gdk-pixbuf-query-loaders-64 >> /app/lib64/gdk-pixbuf-2.0/2.10.0/loaders.cache
    """)
    assert spec.flatpak.command == "eog"
    assert spec.flatpak.component == "eog-flatpak-container"
    assert spec.flatpak.copy_icon is True
    assert spec.flatpak.desktop_file_name_prefix == "org.gnome."
    assert spec.flatpak.desktop_file_name_suffix == ".desktop"
    assert spec.flatpak.end_of_life == "Too old"
    assert spec.flatpak.end_of_life_rebase == "org.gnome.EyeOfNewt"
    assert spec.flatpak.finish_args == "--share=ipc --socket=fallback-x11 --socket=wayland"
    assert spec.flatpak.name == "eog"
    assert spec.flatpak.packages[0].name == "eog"
    assert spec.flatpak.packages[0].platforms is None
    assert spec.flatpak.packages[1].name == "libtastypng"
    assert spec.flatpak.packages[1].platforms is None
    assert spec.flatpak.packages[2].name == "libjpeg-superfast"
    assert spec.flatpak.packages[2].platforms is not None
    assert spec.flatpak.packages[2].platforms.only == ["x86_64"]
    assert spec.flatpak.rename_appdata_file == "eog.appdata.xml"
    assert spec.flatpak.rename_desktop_file == "eog.desktop"
    assert spec.flatpak.rename_icon == "eog.png"
    assert spec.flatpak.runtime_name == "flatpak-runtime"
    assert spec.flatpak.runtime_version == "f39"
    assert spec.flatpak.sdk is None
    assert spec.flatpak.tags == ["Image Viewer", "Eye of GNOME"]

    assert spec.flatpak.get_packages_for_arch(Arch.X86_64) == [
        "eog", "libtastypng", "libjpeg-superfast"
    ]
    assert spec.flatpak.get_packages_for_arch(Arch.PPC64LE) == ["eog", "libtastypng"]

    assert spec.flatpak.get_component_label("FALLBACK") == "eog-flatpak-container"
    assert spec.flatpak.get_name_label("FALLBACK-flatpak") == "eog"

    assert spec.platforms is not None
    assert spec.platforms.includes_platform("x86_64")
    assert not spec.platforms.includes_platform("aarch64")


def test_runtime_container_spec(tmp_path):
    spec = make_spec(tmp_path, RUNTIME_CONTAINER_YAML)

    assert spec.flatpak.app_id == "org.fedoraproject.Platform"
    assert spec.flatpak.build_runtime is True
    assert spec.flatpak.name == "f39/flatpak-runtime"
    assert spec.flatpak.component == "flatpak-runtime"
    assert spec.flatpak.branch == "f39"
    assert spec.flatpak.sdk == "org.fedoraproject.Sdk"
    assert spec.flatpak.finish_args == (
        "--env=GI_TYPELIB_PATH=/app/lib64/girepository-1.0 "
        "--env=GST_PLUGIN_SYSTEM_PATH=/app/lib64/gstreamer-1.0:/usr/lib64/gstreamer-1.0"
    )
    assert spec.flatpak.cleanup_commands == dedent("""\
        mv -f /usr/bin/flatpak-xdg-email /usr/bin/xdg-email
        mv -f /usr/bin/flatpak-xdg-open /usr/bin/xdg-open
    """)
    assert spec.flatpak.packages[0].name == "abattis-cantarell-fonts"
    assert spec.flatpak.packages[1].name == "abattis-cantarell-vf-fonts"


def test_app_container_spec_modules(tmp_path):
    spec = make_spec(tmp_path, APP_CONTAINER_YAML_MODULES)

    assert spec.compose.modules == ["eog:stable"]

    assert spec.flatpak.app_id == "org.gnome.eog"
    assert spec.flatpak.base_image == "f39/flatpak-runtime:latest"
    assert spec.flatpak.branch == "stable"
    assert spec.flatpak.command == "eog"

    assert spec.flatpak.get_component_label("FALLBACK") == "FALLBACK-flatpak"
    assert spec.flatpak.get_name_label("FALLBACK") == "FALLBACK"
    assert spec.flatpak.get_name_label("FALLBACK-flatpak") == "FALLBACK"


@pytest.mark.parametrize('container_yaml,validation_error', [
    (dedent("""
     flatpak: {packages: [eog]}
     """),
     r"container.yaml:flatpak: id is missing"),
    (dedent("""
     flatpak: {id: [foo]}
     """),
     r"container.yaml:flatpak: id must be a string"),
    (dedent("""
     flatpak: {id: org.gnome.eog, copy-icon: 42}
     """),
     r"container.yaml:flatpak: copy-icon must be a boolean"),
    (dedent("""
     flatpak: {id: org.gnome.eog, tags: 42}
     """),
     r"container.yaml:flatpak: tags must be a list of strings"),
    (dedent("""
     flatpak: {id: org.gnome.eog, packages: 42}
     """),
     r"container.yaml:flatpak: packages must be a list of strings and mappings"),
    (dedent("""
     foo: "
     """),
     r"unexpected end of stream"),
    (dedent("""
     """),
     r"No flatpak section in '"),
    (dedent("""
     flatpak: {id: org.gnome.eog}
     """),
     (r"is new style \(compose:modules is not set\). "
      r"Missing keys:\s*flatpak:packages\s*flatpak:runtime-name\s*flatpak:runtime-version")),
    (dedent("""
     flatpak: {id: org.gnome.eog, build-runtime: True}
     """),
     (r"is new style \(compose:modules is not set\). "
      r"Missing keys:\s*flatpak:packages\s*flatpak:name")),
    (dedent("""
     compose: {modules: ["eog:stable"]}
     flatpak: {id: org.gnome.eog, packages: ["eog"]}
     """),
     r"is old style \(compose:modules is set\). Disallowed keys:\s*flatpak:packages"),
])
def test_invalid_spec(tmp_path, container_yaml, validation_error):
    with pytest.raises(ValidationError, match=validation_error):
        make_spec(tmp_path, container_yaml)
