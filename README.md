About
=====
flatpak-module-tools is a set of command line tools (all accessed via a single
'flatpak-module' executable) for operations related to maintaining Flatpak
applications and runtimes as Fedora modules.

flatpak-module local-build
==========================
The `flatpak-module local-build` builds the module locally, then creates a flatpak of it.
It is equivalent to running `flatpak-module build-module; flatpak-module build-container --source=local`

Usage:
    flatpak-module [global options] local-build
	     [--add-local-build=NAME:STREAM[:VERSION]]
		 [--modulemd=mymodule.yaml]
		 [--containerspec=somedir/container.yaml]
		 [--stream=STREAM]
	     [--install]

**--add-local-build**
include a local MBS module build as a source for the build

**--modulemd**
modulemd file to build. If in a git repository, defaults to <reponame>.yaml

**--containerspec**
path to container.yaml - defaults to `./container.yaml`

**--stream**
Module stream for the build. If in a git repository, defaults to `<branchname>`

**--install**
automatically install the resulting Flatpak or runtime for the current user

flatpak-module build-module
===========================
A wrapper around `mbs-manager build_module_locally`.

Usage:
    flatpak-module [global options] build-module
	     [--add-local-build=NAME:STREAM[:VERSION]]
		 [--modulemd=mymodule.yaml]
		 [--stream=STREAM]

**--add-local-build**
include a local MBS module build  as a source for the build

**--modulemd**
modulemd file to build. If in a git repository, defaults to `<reponame>.yaml`

**--stream**
Module stream for the build. If in a git repository, defaults to `<branchname>`

flatpak-module build-container
==============================
Creates a OCI container of an Flatpak application or runtime from a module build.

Output file is:

 NAME-STREAM-VERSION-oci.tar.gz

For example:

 org.example.MyApp-stable-20180205192824.oci.tar.gz

Usage:
    flatpak-module [global options] build-container
	     [--add-local-build=NAME:STREAM[:VERSION]]
	     [--from-local]
	     [--install/--install-user]
		 [--containerspec=somedir/container.yaml]

**--add-local-build**
include a local MBS module build  as a source for the build

**--from-local**
Specifies to build the container from a local module build. Shorthand for '--add-local-build=NAME:STREAM' with the name and stream from container.yaml.

**--install**
automatically install the resulting Flatpak or runtime systemwide

**--containerspec**
path to container.yaml - defaults to `./container.yaml`

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
        base_repo_url: http://kojipkgs.fedoraproject.org/repos/f{platform}/latest/$basearch
        koji_config: /etc/module-build-service/koji.conf
        koji_profile: koji
        mbs_config_file: /etc/module-build-service/config.py
        mbs_config_section: DevConfiguration
        platform_stream_pattern: '^f(\d+)$'
```

(normally, it won't be necessary to set all these values.) The profile name `__default__` provides defaults that
are used if a particular profile doesn't have a key.

Development
===========

You can use pipenv to install a local copy for development:

``` sh
# Create a virtual environment
$ pipenv --site-packages --three
# Enter it
$ pipenv shell
# Install flatpak-module-tools so that edits are picked up immediately
$ pip install -e .
```

Subsequently, you just need `pipenv shell`.

Release process
===============
* Update the version in `setup.py`
* Make a release tarball (`setup.py sdist`)
* Commit the change (`git commit -a -m "Version 0.9.3"`)
* Create a signed tag (`git tag -s -m "Version 0.9.3" v0.9.3`)
* Push commit and tag (`git push origin --tags master`)
* Upload to PyPI (`twine upload flatpak-module-tools-0.9.3.tar.gz`)
* Create updated packages for current Fedora releases and EPEL
* File Bodhi updates as necessary

LICENSE
=======
flatpak-module-tools is licensed under the MIT license. See the LICENSE file for details.
