# -*- coding: utf-8 -*-
# CEMDownloaderPlugin — QGIS plugin
# Copyright (C) 2025 Eduardo Lemus
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file is part of CEMDownloaderPlugin.
# It is distributed under the terms of the GNU General Public License,
# version 3 or later. THIS PROGRAM IS PROVIDED "AS IS", WITHOUT
# WARRANTY; see the LICENSE file for more details.

"""
INEGI CEM Downloader (QGIS Plugin)
- Tab 1: state-based downloads (ZIP) with progress.
- Tab 2: polygon-based WCS (single resolution) producing one GeoTIFF per polygon,
         clipped and symbolized as Single-band Gray with Min/Max stretch.
"""

import json
import os
from pathlib import Path
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAction, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QTabWidget, QTextEdit, QProgressBar
)
from qgis.core import QgsProject, QgsVectorLayer, QgsWkbTypes

from .estado_descarga import download_estado_with_progress
from .poligono_wcs import download_poligono_wcs_split_per_polygon

PLUGIN_DIR = os.path.dirname(__file__)
ICON_PATH = os.path.join(PLUGIN_DIR, "icon.png")
RES_LIST = [15, 30, 60, 90, 120]


def estados_json_path() -> Path:
    """
    Returns the path to the bundled 'estados.json'.
    """
    return Path(__file__).parent / "data" / "estados.json"


def ensure_polygon_layer(layer: QgsVectorLayer) -> bool:
    """
    Validates a QgsVectorLayer is a polygon layer (including multipart).
    """
    if layer is None:
        return False
    if layer.type() != layer.VectorLayer:
        return False
    gtype = layer.wkbType()
    return QgsWkbTypes.geometryType(gtype) == QgsWkbTypes.PolygonGeometry


def log_append(widget: QTextEdit, msg: str) -> None:
    """
    Appends a message to a QTextEdit and autoscrolls to latest.
    """
    widget.append(msg)
    widget.ensureCursorVisible()


class CEMDialog(QDialog):
    """
    Main floating dialog containing two tabs:
      - By State (INEGI ZIP downloads)
      - By Polygon (WCS GetCoverage → per-polygon clip)
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CEM in QGIS")
        self.setWindowModality(Qt.NonModal)
        self.setWindowIcon(QIcon(ICON_PATH))
        self.resize(700, 560)

        self.tabs = QTabWidget()

        # ---------------- Tab 1: By State ----------------
        self.cboEstado = QComboBox()
        self.cboResEstado = QComboBox()
        for r in RES_LIST:
            self.cboResEstado.addItem(f"{r} m", r)
        self.btnDescEstado = QPushButton("Descargar por Estado")
        self.log1 = QTextEdit(); self.log1.setReadOnly(True)

        self.lblStatus1 = QLabel("Listo.")
        self.progress1 = QProgressBar(); self.progress1.setTextVisible(True); self.progress1.setRange(0, 100); self.progress1.setValue(0)

        w1 = QVBoxLayout()
        line1 = QHBoxLayout(); line1.addWidget(QLabel("Estado:")); line1.addWidget(self.cboEstado)
        line2 = QHBoxLayout(); line2.addWidget(QLabel("Resolución:")); line2.addWidget(self.cboResEstado); line2.addStretch(); line2.addWidget(self.btnDescEstado)
        w1.addLayout(line1); w1.addLayout(line2)
        w1.addWidget(QLabel("Nota: los archivos se guardan en carpeta TEMP. Guarda una copia si deseas conservarlos."))
        w1.addWidget(self.lblStatus1)
        w1.addWidget(self.progress1)
        w1.addWidget(self.log1)
        page1 = QDialog(); page1.setLayout(w1)

        # ------------- Tab 2: By Polygon (WCS + clip) -------------
        self.cboLayer = QComboBox()
        self.btnRefresh = QPushButton("Actualizar capas")
        self.cboResPoly = QComboBox()
        for r in RES_LIST:
            self.cboResPoly.addItem(f"{r} m", r)
        self.btnDescPoly = QPushButton("Descargar por Polígono")
        self.log2 = QTextEdit(); self.log2.setReadOnly(True)

        self.lblStatus2 = QLabel("Listo.")
        self.progress2 = QProgressBar(); self.progress2.setTextVisible(True); self.progress2.setRange(0, 100); self.progress2.setValue(0)

        w2 = QVBoxLayout()
        l0 = QHBoxLayout(); l0.addWidget(QLabel("Capa de polígonos:")); l0.addWidget(self.cboLayer); l0.addWidget(self.btnRefresh)
        l1 = QHBoxLayout(); l1.addWidget(QLabel("Resolución:")); l1.addWidget(self.cboResPoly); l1.addStretch(); l1.addWidget(self.btnDescPoly)
        w2.addLayout(l0); w2.addLayout(l1)
        w2.addWidget(self.lblStatus2); w2.addWidget(self.progress2)
        w2.addWidget(QLabel("Nota: el GeoTIFF descargado y el recorte se guardan en TEMP. Guarda una copia para conservarlos."))
        w2.addWidget(self.log2)
        page2 = QDialog(); page2.setLayout(w2)

        self.tabs.addTab(page1, "Por Estado")
        self.tabs.addTab(page2, "Por Polígono")

        lay = QVBoxLayout(); lay.addWidget(self.tabs); self.setLayout(lay)

        # Populate states (filtering out any 'Nacional' entry if present)
        self._estados = self._load_estados()
        for item in self._estados:
            self.cboEstado.addItem(item["entidad"], item)

        # Wire handlers
        self.btnDescEstado.clicked.connect(self.on_download_estado)
        self.btnRefresh.clicked.connect(self.populate_layers)
        self.btnDescPoly.clicked.connect(self.on_download_poly)

        # Initial layer population
        self.populate_layers()

    def _load_estados(self):
        """
        Loads 'estados.json' and returns items excluding 'Nacional'.
        """
        p = estados_json_path()
        with open(p, "r", encoding="utf-8") as f:
            all_items = json.load(f)
        out = []
        for it in all_items:
            name = (it.get("entidad") or "").strip()
            if name and name.lower() != "nacional":
                out.append(it)
        return out

    # ---------------- Tab 1: By State ----------------
    def on_download_estado(self):
        """
        Triggers ZIP download pipeline for the selected state and resolution.
        """
        item = self.cboEstado.currentData()
        res_m = self.cboResEstado.currentData()
        if not item:
            log_append(self.log1, "Selecciona un estado válido.")
            return

        download_estado_with_progress(
            entidad=item["entidad"],
            cve=item["cve"],
            res_m=res_m,
            log_widget=self.log1,
            status_label=self.lblStatus1,
            progressbar=self.progress1
        )

    # --------------- Tab 2: By Polygon ----------------
    def populate_layers(self):
        """
        Populates the vector layer combo with polygon layers present in the project.
        """
        self.cboLayer.clear()
        prj = QgsProject.instance()
        for lyr in prj.mapLayers().values():
            if isinstance(lyr, QgsVectorLayer) and ensure_polygon_layer(lyr):
                self.cboLayer.addItem(lyr.name(), lyr)
        if self.cboLayer.count() == 0:
            self.cboLayer.addItem("— No hay capas de polígonos —", None)

    def on_download_poly(self):
        """
        Triggers the per-polygon WCS download + clip + load pipeline for the selected layer.
        Produces one GeoTIFF per polygon (singleparts extracted from multiparts).
        """
        layer = self.cboLayer.currentData()
        if not layer or not isinstance(layer, QgsVectorLayer) or not ensure_polygon_layer(layer):
            log_append(self.log2, "Selecciona una capa de polígonos válida.")
            return

        res_m = self.cboResPoly.currentData()

        self.lblStatus2.setText("Preparando…")
        self.progress2.setRange(0, 1)
        self.progress2.setValue(0)

        download_poligono_wcs_split_per_polygon(
            layer=layer,
            res_m=res_m,
            log_widget=self.log2,
            status_label=self.lblStatus2,
            progressbar=self.progress2
        )


class CEMDownloaderPlugin:
    """
    QGIS plugin entrypoint that registers a toolbar/menu action and opens the dialog.
    """
    def __init__(self, iface):
        self.iface = iface
        self._action = None
        self._dlg = None

    def initGui(self):
        """
        Creates the QAction with icon and registers it in menu and toolbar.
        """
        self._action = QAction(QIcon(ICON_PATH), "CEM in QGIS", self.iface.mainWindow())
        self._action.triggered.connect(self.run)
        self.iface.addPluginToMenu("&CEM in QGIS", self._action)
        self.iface.addToolBarIcon(self._action)

    def unload(self):
        """
        Cleans up action from menus/toolbars when the plugin is unloaded.
        """
        if self._action:
            self.iface.removePluginMenu("&CEM in QGIS", self._action)
            self.iface.removeToolBarIcon(self._action)
        self._action = None

    def run(self):
        """
        Shows/raises the main dialog.
        """
        if self._dlg is None:
            self._dlg = CEMDialog(self.iface.mainWindow())
        self._dlg.show()
        self._dlg.raise_()
        self._dlg.activateWindow()
