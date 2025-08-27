# -*- coding: utf-8 -*-
# CEMDownloaderPlugin — QGIS plugin
# Copyright (C) 2025 Eduardo Lemus
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file is part of CEMDownloaderPlugin.
# It is distributed under the terms of the GNU General Public License,
# version 3 or later. THIS PROGRAM IS PROVIDED "AS IS", WITHOUT
# WARRANTY; see the LICENSE file for more details.

def classFactory(iface):
    from .plugin import CEMDownloaderPlugin
    return CEMDownloaderPlugin(iface)
