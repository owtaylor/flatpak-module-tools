#!/bin/bash

set -ex

pyproject-build -s --no-isolation --outdir .

read -r PROJECT_VERSION < VERSION || :
RPM_VERSION=${PROJECT_VERSION/.post/^}
if [[ $RPM_VERSION =~ ^([0-9.]+)((a|b|rc).*) ]] ; then
    RPM_VERSION=${BASH_REMATCH[1]}~${BASH_REMATCH[2]}
fi

sed -E \
     -e 's/^(Version:[ 	]*).*/\1'"$RPM_VERSION"/ \
     -e 's/^(%global project_version *).*/\1'"$PROJECT_VERSION"/ \
     -i flatpak-module-tools.spec
