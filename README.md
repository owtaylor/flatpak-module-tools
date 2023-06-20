About
=====
flatpak-module-tools is a set of command line tools (all accessed via a single
'flatpak-module' executable) for operations related to maintaining Flatpak
applications and runtimes as Fedora modules.

flatpak-module build-container
==============================
Creates a OCI container of an Flatpak application or runtime from packages downloaded
from Koji or in a local repository.

Usage:
    flatpak-module [global options] build-container
         [--install]
         [--containerspec=somedir/container.yaml]
         [--flatpak-metadata=labels/annotations/both]
         [--target=some-koji-target]

**--install**
automatically install the resulting Flatpak or runtime systemwide

**--containerspec**
path to container.yaml - defaults to `./container.yaml`

**--flatpak-metadata**
how flatpak metadata should be stored. Defaults to `both`. Using
only labels require Flatpak >= 1.6.

**--target**
Koji target to build against. Determined from runtime_version if missing.

flatpak-module install
======================

Installs a Flatpak or Runtime built as an OCI bundle for the current user. If it doesn't
already exist, a `flatpak-module-tools` remote is added to the Flatpak's user configuration.

Usage:
    flatpak-module [global options] install [PATH-or-URL]


**--koji**
Look up argument as NAME[:STREAM] in Koji, instead of a path or an URL, and install the latest
Flatpak build that matches.

global options
==============

**--verbose/v**
Show verbose debugging output

**--config/c**
Additional configuration file to read

**--profile/p**
Alternate configuration profile to use. Default is `production`. The standard config file
for flatpak-module-tools defines `production` and `staging`, which result in using the
Fedora production and staging environments, respectively.

Configuration
=============
Configuration is read from the following sources, in descending order of priority:

* Any config file specified on the comand line, first has highest priority
* `~/.config/flatpak-module/config.d/*.yaml`, sorted alphabetically
* `~/.config/flatpak-module/config.yaml`
* `/etc/flatpak-module/config.d/*.yaml`, sorted alphabetically
* `/etc/flatpak-module/config.yaml`
* `config.yaml` in the Python installation directory of flatpak-module-tools

A config file looks like:

``` yaml
profiles:
    profile_name:
		# Koji config file; leave blank to use standard files in /etc and ~/.koji
        koji_config: null
        koji_profile: koji
		# Different koji instance used for sources when rebuilding RPMs; if both
		# are unset, koji_config/koji_profile is used.
		source_koji_config: null
		source_koji_profile: null
		# Target used to build Flatpak application RPMs (prefix=/app rebuilds)
        rpm_koji_target: f$release-flatpak-app
		# Target used to build Flatpak containers
        flatpak_koji_target: f$release-flatpak-candidate
		# Tag used to get source for rebuilding RPMs locally;; refers to
		# source_koji_config/source_koji_profile if those are set.
        source_koji_tag: f$release-build
```

(normally, it won't be necessary to set all these values.) The profile name `__default__` provides defaults that
are used if a particular profile doesn't have a key.

Development
===========

You can use tox to setup a local environent for testing

``` sh
tox -e dev
. .venv/bin/activate
```

Subsequently, you just need `. .venv/bin/activate` unless your dependencies change.

Release process
===============
* Create a signed tag (`git tag -s -m "Version 0.9.3" v0.9.3`)
* Make a source tarball (`pyproject-build -s`)
* Push tag and any commits (`git push origin --tags main`)
* Upload source to PyPI (`twine upload dist/flatpak-module-tools-0.9.3.tar.gz`)
* Create updated packages for current Fedora releases and EPEL
* File Bodhi updates as necessary

LICENSE
=======
flatpak-module-tools-depchase is licensed under the terms of the GNU General Public License,
version 3 or later. See the LICENSE.gplv3 file for details.

The rest of flatpak-module-tools is licensed under the MIT license.
See the LICENSE file for details.
