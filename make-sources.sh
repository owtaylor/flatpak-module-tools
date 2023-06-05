#!/bin/bash

pyproject-build --no-isolation --outdir .

read -r PROJECT_VERSION < VERSION
RPM_VERSION=${PROJECT_VERSION/.post/^}

sed -E \
     -e 's/^(Version:[ 	]*).*/\1'"$RPM_VERSION"/ \
     -e 's/^(%global project_version *).*/\1'"$PROJECT_VERSION"/ \
     -i flatpak-module-tools.spec
