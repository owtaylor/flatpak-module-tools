# -*- coding: utf-8 -*-
#
# util.yaml - augment PyYAML to not implicitly type-cast scalars
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

from yaml import load, load_all
from yaml.composer import Composer
from yaml.constructor import Constructor, SafeConstructor
from yaml.parser import Parser
from yaml.reader import Reader
from yaml.resolver import Resolver
from yaml.scanner import Scanner


class NoImplicitScalarResolver(Resolver):

    filtered_implicit_tags = tuple(
        'tag:yaml.org,2002:' + tag
        for tag in ('bool', 'float', 'int', 'null', 'timestamp')
    )

    def __init__(self):
        super().__init__()
        self._filter_resolvers()

    @classmethod
    def _filter_resolvers(cls):
        if 'yaml_implicit_resolvers' not in cls.__dict__:
            implicit_resolvers = {}
            for key, resolvers in cls.yaml_implicit_resolvers.items():
                implicit_resolvers[key] = [
                    (tag, regexp)
                    for tag, regexp in resolvers
                    if tag not in cls.filtered_implicit_tags
                ]
            cls.yaml_implicit_resolvers = implicit_resolvers


class NoImplicitScalarSafeLoader(Reader, Scanner, Parser, Composer, SafeConstructor,
                                 NoImplicitScalarResolver):

    def __init__(self, stream):
        Reader.__init__(self, stream)
        Scanner.__init__(self)
        Parser.__init__(self)
        Composer.__init__(self)
        SafeConstructor.__init__(self)
        NoImplicitScalarResolver.__init__(self)


class NoImplicitScalarLoader(Reader, Scanner, Parser, Composer, Constructor,
                             NoImplicitScalarResolver):

    def __init__(self, stream):
        Reader.__init__(self, stream)
        Scanner.__init__(self)
        Parser.__init__(self)
        Composer.__init__(self)
        Constructor.__init__(self)
        NoImplicitScalarResolver.__init__(self)


def yaml_safe_load(stream):
    return load(stream, Loader=NoImplicitScalarLoader)


def yaml_safe_load_all(stream):
    return load_all(stream, Loader=NoImplicitScalarLoader)


def yaml_load(stream, Loader=NoImplicitScalarLoader):
    return load(stream, Loader=Loader)


def yaml_load_all(stream, Loader=NoImplicitScalarLoader):
    return load_all(stream, Loader=Loader)
