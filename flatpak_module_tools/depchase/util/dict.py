# -*- coding: utf-8 -*-
#
# util.dict - dict helper functions and classes
#
# Copyright © 2018 Red Hat, Inc.
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

from collections import defaultdict
import copy


def dict_merge_deep(d1, d2):
    d1_keys = set(d1)
    d2_keys = set(d2)

    merged = {}

    for k in d1_keys - d2_keys:
        merged[k] = copy.copy(d1[k])

    for k in d2_keys - d1_keys:
        merged[k] = copy.copy(d2[k])

    for k in d1_keys & d2_keys:
        if isinstance(d1[k], dict) and isinstance(d2[k], dict):
            merged[k] = dict_merge_deep(d1[k], d2[k])
        else:
            merged[k] = copy.copy(d2[k])

    return merged


class DefaultDictWithKey(defaultdict):

    def __missing__(self, key):
        if self.default_factory is None:
            raise KeyError(key)

        ret = self[key] = self.default_factory(key)  # type: ignore
        return ret
