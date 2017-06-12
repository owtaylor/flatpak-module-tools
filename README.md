About
=====
flatpak-module-tools is a set of command line tools (all accessed via a single
'flatpak-module' binary) for operations related to creating and maintaining
Fedora modules that target Flatpak applications and runtimes

Obtaining module information and packages
=========================================
The tools can access two types of modules: official module builds from Koji
and local builds on the current system done with `mbs-build local`

official builds
---------------
Information about official builds is queried from the Fedora PDC. Because
yum repositories are not yet exported for Fedora modules, when repository
information or packages for a module are needed, the module is downloaded
from Koji into `~/modulebuild/cache/koji_tags`. This is done using code
from the module-build-service so that the cache is shared between these
tools and mbs-build local.

local builds
------------
Local builds from `~/modulebuild/cache` are not automatically used, but can
be added with the '--add-local-build/-l' command line option. For example

    flatpak-module create-modulemd -l flatpak-runtime:f26 <other args>

will use the most recent local build of the f26 stream of the flatpak-runtime
module instead of querying the PDC for a build.

flatpak-module create-modulemd
==============================
The `flatpak-module create-modulemd` command creates a modulemd file for an
application or runtime by using repository information for a non-modular
build of Fedora as a source of dependency information.

Example:

    flatpak-module create-modulemd --template flatpak-runtime.in.yaml --package-list flatpak-runtime-packages.yaml -o flatpak-runtime.yaml

flatpak-module create-flatpak
=============================
The `flatpak-module create-flatpak` builds a flatpak application or runtime
out of a previously built module.

Example:

    flatpak-module create-flatpak --add-local-build flatpak-runtime:f26 --add-local-build eog:f26 --module eog:f26 --info flatpak.json

LICENSE
=======
flatpak-module-tools is licensed under the MIT license. See the LICENSE file for details.
