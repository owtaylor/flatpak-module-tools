#!/bin/bash

set -e

declare -A git_files
while read -r filename ; do
    git_files[$filename]=true
done < <(git ls-tree -r --name-only HEAD)

set -x

pyproject-build -s --no-isolation --outdir .

read -r PROJECT_VERSION < VERSION || :
RPM_VERSION=${PROJECT_VERSION/.post/^}
if [[ $RPM_VERSION =~ ^([0-9.]+)((a|b|rc).*) ]] ; then
    RPM_VERSION=${BASH_REMATCH[1]}~${BASH_REMATCH[2]}
fi

# Check that we aren't distributing uncommitted files
set +x
while read -r filename ; do
    filename=${filename#flatpak-module-tools-"${PROJECT_VERSION}"/}
    if [[ $filename == "" ||
          $filename =~ .*/$ ||
          $filename == "PKG-INFO" ||
          $filename == "VERSION" ||
          $filename =~ flatpak_module_tools.egg-info/* ||
          $filename == setup.cfg ]] ; then
        continue
    fi
    if [[ $filename != "" && ${git_files[$filename]} == "" ]] ; then
        echo "$filename is not in git"
        exit 1
    fi
done < <(tar tf "flatpak-module-tools-$PROJECT_VERSION.tar.gz")
set -x

sed -E \
     -e 's/^(Version:[ 	]*).*/\1'"$RPM_VERSION"/ \
     -e 's/^(%global project_version *).*/\1'"$PROJECT_VERSION"/ \
     -i flatpak-module-tools.spec
