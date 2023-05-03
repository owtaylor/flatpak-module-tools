# -*- coding: utf-8 -*-
#
# util.arch - helper functionality for handling architectures
#
# Copyright © 2019 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Author:
# Nils Philippsen <nils@redhat.com>

from typing import Optional

from .dataset import parse_dataset_name


config = None


def __score(arch, basearch, __seen):
    if arch == basearch:
        return 1

    compat_arches = config.get('arch-compat', {}).get(basearch, [])

    # breadth-first
    if arch in compat_arches:
        return 2

    # recurse
    for compat_arch in compat_arches:
        if compat_arch in __seen:
            continue

        cscore = __score(arch, compat_arch, __seen=__seen)
        if cscore:
            return cscore + 1


def score(arch: str, basearch: Optional[str] = None) -> Optional[int]:
    """Compute a score how well an architecure fits on a base architecture.

    :param arch:        name of the architecture
    :param basearch:    name of the base architecture or None (defaults to
                        architecture of the dataset)

    :returns:           a score, the lower, the better, or None if it doesn't
                        match at all
    """
    global config
    if not config:
        from ..config import config

    if not basearch:
        release_name, basearch = parse_dataset_name()

    return __score(arch, basearch, {basearch})
