FROM registry.fedoraproject.org/fedora:41

RUN dnf -y install \
    createrepo_c \
    flatpak \
    git-core \
    libappstream-glib \
    ostree \
    python3-build \
    python3-gobject-base \
    python3-koji \
    python3-pip \
    rpm-build

WORKDIR /opt/app-root/src

COPY .git/ /opt/app-root/src/.git
COPY flatpak_module_tools/ /opt/app-root/src/flatpak_module_tools
COPY tests/ /opt/app-root/src/tests
COPY pyproject.toml LICENSE LICENSE.gplv3 MANIFEST.in /opt/app-root/src/

RUN mkdir /tmp/wheel && python3 -m build . --outdir=/tmp/wheel && pip3 install $(echo /tmp/wheel/*.whl)'[cli,tests]'
RUN pytest tests
