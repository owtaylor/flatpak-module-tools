#!/bin/bash

topdir=$(cd "$(dirname $0)" && pwd)
specfile=$topdir/flatpak-module-tools.spec

cp "$specfile"{,.bak}

cleanup() {
    if cmp -s "$specfile"{,.bak} ; then
        rm "$specfile".bak
    else
        mv "$specfile"{.bak,}
    fi
}

trap cleanup EXIT

. "$topdir/make-sources.sh"
rpmbuild --define "_sourcedir $topdir" -ba flatpak-module-tools.spec
rm -f flatpak-module-tools-$PROJECT_VERSION.tar.gz flatpak_module_tools-$PROJECT_VERSION*.whl
