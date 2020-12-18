import base64
import gzip
import os
import re
import shutil
from textwrap import dedent
import tempfile

from flatpak_module_tools.flatpak_builder import FileTreeProcessor

import pytest
import six

APPDATA_CONTENTS = """\
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop">
  <id>org.gnome.eog</id>
  <launchable type="desktop-id">org.gnome.eog.desktop</launchable>
  <metadata_license>CC0-1.0</metadata_license>
  <project_license>GPL-2.0+ and GFDL-1.3</project_license>
  <name>Eye of GNOME</name>
  <summary>Browse and rotate images</summary>
  <description>
    <p>
      The Eye of GNOME is the official image viewer for the GNOME desktop.
      It integrates with the GTK+ look and feel of GNOME, and supports many image
      formats for viewing single images or images in a collection.
    </p>
  </description>
</component>
"""

DESKTOP_CONTENTS = """\
[Desktop Entry]
Name=Image Viewer
Name[de]=Bildbetrachter
Comment=Browse and rotate images
TryExec=eog
Exec=eog %U
Icon[de]=org.gnome.eog
Icon=org.gnome.eog
StartupNotify=true
Terminal=false
Type=Application
Categories=GNOME;GTK;Graphics;2DGraphics;RasterGraphics;Viewer;
"""

# 64x64 blank PNG
ICON_CONTENTS = base64.b64decode("""\
iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAATUlEQVRo3u3P
QQ0AAAgEILV/5zOFDzdoQCepz6aeExAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ
EBAQEBAQEBAQEBAQEBAQEBAQEBAQELi3cqoDfV7ZY54AAAAASUVORK5CYII=
""")

APP_TREE_FILES = {
}


class AppTree(object):
    def __init__(self, root):
        self.root = root

    def path(self, rel):
        return os.path.join(self.root, rel)

    def contents(self, rel, decompress=False):
        if decompress:
            with gzip.open(self.path(rel), 'rt') as f:
                return f.read()
        else:
            with open(self.path(rel), 'rt') as f:
                return f.read()

    def create(self,
               rename_appdata_file=None,
               rename_desktop_file=None,
               rename_icon=None):

        files = {}

        appdata_contents = APPDATA_CONTENTS
        desktop_contents = DESKTOP_CONTENTS

        def assert_sub(pattern, replacement, string):
            new = re.sub(pattern, replacement, string)
            assert new != string

            return new

        if rename_desktop_file:
            appdata_contents = assert_sub(r'<id>org\.gnome\.eog</id>',
                                          r'<id>' + rename_desktop_file + r'</id>',
                                          appdata_contents)
            appdata_contents = assert_sub(r'<launchable type="desktop-id">org\.gnome\.eog\.desktop</launchable>',
                                          r'<launchable type="desktop-id">' + rename_desktop_file + r'</launchable>',
                                          appdata_contents)
        if rename_icon:
            desktop_contents = assert_sub(r'Icon(\[de\])?=org.gnome.eog',
                                          r'Icon\1=' + rename_icon,
                                          desktop_contents)

        if rename_appdata_file:
            if rename_appdata_file.endswith('metainfo.xml'):
                files["files/share/metainfo/" + rename_appdata_file] = appdata_contents
            else:
                files["files/share/appdata/" + rename_appdata_file] = appdata_contents
        else:
            files["files/share/appdata/org.gnome.eog.appdata.xml"] = appdata_contents

        if rename_desktop_file:
            files["files/share/applications/" + rename_desktop_file] = desktop_contents
        else:
            files["files/share/applications/org.gnome.eog.desktop"] = desktop_contents

        if rename_icon:
            files["files/share/icons/hicolor/64x64/apps/" + rename_icon + ".png"] = ICON_CONTENTS
        else:
            files["files/share/icons/hicolor/64x64/apps/org.gnome.eog.png"] = ICON_CONTENTS

        for path, contents in files.items():
            fullpath = os.path.join(self.root, path)
            parentpath = os.path.dirname(fullpath)
            if not os.path.exists(parentpath):
                os.makedirs(parentpath)
            with open(fullpath, "wb") as f:
                if isinstance(contents, six.text_type):
                    contents = contents.encode("UTF-8")
                f.write(contents)


@pytest.fixture()
def app_tree(request):
    tempdir = tempfile.mkdtemp()
    try:
        tree = AppTree(tempdir)
        yield tree
    finally:
        shutil.rmtree(tempdir)


@pytest.mark.parametrize('rename_appdata_file', (None, 'eog.appdata.xml', 'eog.metainfo.xml'))
@pytest.mark.parametrize('rename_desktop_file', (None, 'eog.desktop'))
@pytest.mark.parametrize('rename_icon', (None, 'eog'))
def test_renames(app_tree,
                 rename_appdata_file,
                 rename_desktop_file,
                 rename_icon):
    app_tree.create(rename_appdata_file,
                    rename_desktop_file,
                    rename_icon)

    processor = FileTreeProcessor(app_tree.root, {
        'id': 'org.gnome.eog',
        'rename-appdata-file': rename_appdata_file,
        'rename-desktop-file': rename_desktop_file,
        'rename-icon': rename_icon,
    })
    processor.process()

    assert os.listdir(app_tree.path('files/share/icons/hicolor/64x64/apps')) == ['org.gnome.eog.png']

    contents = app_tree.contents('files/share/app-info/xmls/org.gnome.eog.xml.gz',
                                 decompress=True)
    assert '<id>org.gnome.eog</id>' in contents
    assert '<launchable type="desktop-id">org.gnome.eog.desktop</launchable>' in contents


def test_copy_icon(app_tree):
    app_tree.create(rename_icon='eog')

    processor = FileTreeProcessor(app_tree.root, {
        'id': 'org.gnome.eog',
        'rename-icon': 'eog',
        'copy-icon': True
    })
    processor.process()

    assert os.path.exists(app_tree.path('files/share/icons/hicolor/64x64/apps/eog.png'))
    assert os.path.exists(app_tree.path('files/share/icons/hicolor/64x64/apps/org.gnome.eog.png'))


@pytest.mark.parametrize('prefix', (None, 'Wonderful '))
@pytest.mark.parametrize('suffix', (None, ' Nightly'))
def test_desktop_file_name_prefix_suffix(app_tree, prefix, suffix):
    app_tree.create()

    processor = FileTreeProcessor(app_tree.root, {
        'id': 'org.gnome.eog',
        'desktop-file-name-prefix': prefix,
        'desktop-file-name-suffix': suffix,
    })
    processor.process()

    expected_en = "Name=" + (prefix or "") + "Image Viewer" + (suffix or "") + "\n"
    expected_de = "Name[de]=" + (prefix or "") + "Bildbetrachter" + (suffix or "") + "\n"
    contents = app_tree.contents('files/share/applications/org.gnome.eog.desktop')

    assert expected_en in contents
    assert expected_de in contents


def test_appdata_license(app_tree):
    app_tree.create()

    processor = FileTreeProcessor(app_tree.root, {
        'id': 'org.gnome.eog',
        'appdata-license': 'GPL-2.0+ and GFDL-1.3 and LGPL-2.1+',
    })
    processor.process()

    contents = app_tree.contents('files/share/app-info/xmls/org.gnome.eog.xml.gz',
                                 decompress=True)
    assert '<project_license>GPL-2.0+ and GFDL-1.3 and LGPL-2.1+</project_license>' in contents


@pytest.mark.parametrize('appstream_compose', (False, True))
def test_appstream_compose(app_tree, appstream_compose):
    app_tree.create()

    processor = FileTreeProcessor(app_tree.root, {
        'id': 'org.gnome.eog',
        'appstream-compose': appstream_compose,
    })
    processor.process()

    composed_path = app_tree.path('files/share/app-info/xmls/org.gnome.eog.xml.gz')
    if appstream_compose:
        assert os.path.exists(composed_path)
    else:
        assert not os.path.exists(composed_path)
