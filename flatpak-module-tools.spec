%global srcname flatpak-module-tools
%global project_version @PROJECT_VERSION@

Name:		%{srcname}
Version:	@RPM_VERSION@
Release:	1
Summary:	Tools for maintaining Flatpak applications and runtimes as Fedora modules

License:	MIT
URL:		https://pagure.io/flatpak-module-tools
Source0:	https://releases.pagure.org/flatpak-module-tools/flatpak-module-tools-%{project_version}.tar.gz

BuildArch:	noarch

BuildRequires: python3-build
BuildRequires: python3-devel
BuildRequires: python3-pip
BuildRequires: python3-setuptools
BuildRequires: python3-setuptools_scm+toml
BuildRequires: python3-wheel

# For tests
BuildRequires: createrepo_c
BuildRequires: flatpak
BuildRequires: libappstream-glib
BuildRequires: libmodulemd
BuildRequires: librsvg2
BuildRequires: ostree
BuildRequires: python3-click
BuildRequires: python3-gobject-base
BuildRequires: python3-pytest-cov
BuildRequires: python3-jinja2
BuildRequires: python3-koji
BuildRequires: python3-networkx
BuildRequires: python3-pytest
BuildRequires: python3-requests
BuildRequires: python3-responses
BuildRequires: python3-rpm
BuildRequires: python3-yaml

Requires: python3-%{srcname} = %{version}-%{release}
Requires: python3-jinja2
Requires: python3-koji
Requires: python3-networkx
Requires: python3-requests-toolbelt
# for pkg_resources
Requires: python3-setuptools
Requires: python3-solv

%description
flatpak-module-tools is a set of command line tools (all accessed via a single
'flatpak-module' executable) for operations related to maintaining Flatpak
applications and runtimes as Fedora modules.

%package -n python3-%{srcname}
Summary: Shared code for building Flatpak applications and runtimes from Fedora modules

# Note - pythonN-flatpak-modules-tools subpackage contains all the Python files from
# the upstream distribution, but some of them are only useful for the CLI, not
# for using this as a library for atomic-reactor. The dependencies here are those
# needed for library usage, the main package has the remainder.

Requires: createrepo_c
Requires: flatpak
# For appstream-compose
Requires: libappstream-glib
# for SVG gdk-pixbuf loader
Requires: librsvg2
Requires: ostree
Requires: python3-click
Requires: python3-requests
Requires: python3-rpm
Requires: python3-yaml

# Output changed from <nvr>.oci.tar.gz to <nvr>.oci.tar
Conflicts: koji-flatpak <= 0.2

%description -n python3-%{srcname}
Python3 library for Flatpak handling

%prep
%autosetup -p1 -n %{srcname}-%{project_version}


%build
export SETUPTOOLS_SCM_PRETEND_VERSION=%{project_version}
%pyproject_wheel


%check
# Tests using RPM don't work well inside %%check
%pytest -k "not test_create_rpm_manifest"


%install
%pyproject_install


%files
%license LICENSE
%doc README.md
%{_bindir}/flatpak-module
%{_bindir}/flatpak-module-depchase


%files -n python3-%{srcname}
%license LICENSE
%{python3_sitelib}/*


%changelog
* Thu Jan 19 2023 Fedora Release Engineering <releng@fedoraproject.org> - 0.13-7
- Rebuilt for https://fedoraproject.org/wiki/Fedora_38_Mass_Rebuild
