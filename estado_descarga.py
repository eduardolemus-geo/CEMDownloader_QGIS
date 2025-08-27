# -*- coding: utf-8 -*-
# CEMDownloaderPlugin — QGIS plugin
# Copyright (C) 2025 Eduardo Lemus
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file is part of CEMDownloaderPlugin.
# It is distributed under the terms of the GNU General Public License,
# version 3 or later. THIS PROGRAM IS PROVIDED "AS IS", WITHOUT
# WARRANTY; see the LICENSE file for more details.

from pathlib import Path
import os
import zipfile
from typing import Callable, Optional

from qgis.PyQt.QtCore import QUrl, QEventLoop, QStandardPaths
from qgis.PyQt.QtWidgets import QApplication, QTextEdit, QLabel, QProgressBar
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import (
    QgsProject,
    QgsRasterLayer,
    QgsSingleBandGrayRenderer,
    QgsContrastEnhancement,
    QgsRasterBandStats,
    QgsNetworkAccessManager,
)

# ---------------------------- Configuration ----------------------------

# INEGI "DownloadFile.do" endpoint for state-based CEM downloads (ZIP packages).
BASE_URL = "https://www.inegi.org.mx/app/geo2/elevacionesmex/DownloadFile.do"

# Known BUILD tag for CEM V3 ZIP naming. Update if INEGI changes the server build.
BUILD_TAG = "20170619"

# Resolutions offered by INEGI for state ZIPs (meters).
RES_LIST = [15, 30, 60, 90, 120]


# ------------------------------ Utilities ------------------------------

def plugin_temp_dir() -> Path:
    """
    Returns a plugin-scoped temp directory under the OS/QGIS temp location.
    Directory is created if it does not exist.
    """
    base = Path(QStandardPaths.writableLocation(QStandardPaths.TempLocation))
    d = base / "CEM_QGIS_Temp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_append(widget: QTextEdit, msg: str) -> None:
    """
    Appends a message to the QTextEdit log and keeps the cursor visible.
    """
    widget.append(msg)
    widget.ensureCursorVisible()


def unzip_all(zip_path: Path, out_dir: Path) -> None:
    """
    Extracts the entire ZIP archive into the provided output directory.
    """
    with zipfile.ZipFile(str(zip_path), "r") as z:
        z.extractall(str(out_dir))


def guess_raster_files(root: Path) -> list[Path]:
    """
    Recursively scans 'root' for raster files typically found in CEM packages.
    Returns a list of candidate raster paths.
    """
    exts = {".tif", ".tiff", ".bil", ".img"}
    out: list[Path] = []
    for r, _, fns in os.walk(root):
        for fn in fns:
            if Path(fn).suffix.lower() in exts:
                out.append(Path(r) / fn)
    return out


def human_size(n: float) -> str:
    """
    Formats a byte count into a human-readable string.
    """
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024.0:
            return f"{f:.1f} {u}"
        f /= 1024.0
    return f"{f:.1f} PB"


def http_get_to_file_progress(
    url: QUrl,
    out_path: Path,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> None:
    """
    Streams an HTTP GET request to disk using QgsNetworkAccessManager to honor QGIS
    proxy/SSL settings. A progress callback receives (bytes_received, bytes_total|0).
    """
    nam = QgsNetworkAccessManager.instance()
    req = QNetworkRequest(url)
    req.setRawHeader(b"User-Agent", b"CEM-QGIS-Downloader/0.1")
    reply = nam.get(req)

    received = 0
    f = open(out_path, "wb")

    def on_ready_read():
        nonlocal received
        chunk = bytes(reply.readAll())
        received += len(chunk)
        f.write(chunk)

    def on_progress(br, bt):
        if progress_cb:
            progress_cb(int(br), int(bt))

    loop = QEventLoop()
    reply.readyRead.connect(on_ready_read)
    reply.downloadProgress.connect(on_progress)
    reply.finished.connect(loop.quit)
    loop.exec_()
    f.close()

    if reply.error():
        err = reply.errorString()
        reply.deleteLater()
        raise RuntimeError(f"Error de red: {err}")

    reply.deleteLater()


def add_raster_gray_with_stats(path: Path) -> bool:
    """
    Loads a raster file and enforces a Single-band Gray renderer with Stretch to Min/Max.
    If initial statistics collapse to a constant range (e.g., 0–0), retries with sampling.
    Returns True if the layer was added to the project successfully.
    """
    layer = QgsRasterLayer(str(path), path.stem, "gdal")
    if not layer.isValid():
        return False

    provider = layer.dataProvider()
    band = 1

    # Full-extent statistics (no sampling). If degenerate, retry with sampling.
    stats = provider.bandStatistics(band, QgsRasterBandStats.All, layer.extent(), 0)
    minv, maxv = stats.minimumValue, stats.maximumValue
    if minv == maxv:
        stats = provider.bandStatistics(band, QgsRasterBandStats.All, layer.extent(), 10000)
        minv, maxv = stats.minimumValue, stats.maximumValue

    renderer = QgsSingleBandGrayRenderer(provider, band)
    ce = QgsContrastEnhancement(provider.dataType(band))
    ce.setContrastEnhancementAlgorithm(QgsContrastEnhancement.StretchToMinimumMaximum, True)

    if (minv is not None) and (maxv is not None) and (minv < maxv):
        ce.setMinimumValue(minv)
        ce.setMaximumValue(maxv)

    renderer.setContrastEnhancement(ce)
    layer.setRenderer(renderer)

    QgsProject.instance().addMapLayer(layer)
    layer.triggerRepaint()
    return True


def build_estado_url(entidad: str, cve_edo: str, res_m: int) -> QUrl:
    """
    Builds the INEGI 'DownloadFile.do' URL for the given state and resolution.
    Naming differs for R15 (TIF ZIP) vs. other resolutions (BIL ZIPs).
    """
    if res_m == 15:
        fname = f"CEM_V3_{BUILD_TAG}_R{res_m}_E{cve_edo}_TIF.zip"
    else:
        fname = f"CEM_V3_{BUILD_TAG}_R{res_m}_E{cve_edo}.zip"

    from qgis.PyQt.QtCore import QUrl, QUrlQuery
    u = QUrl(BASE_URL)
    q = QUrlQuery()
    q.addQueryItem("file", fname)
    q.addQueryItem("res", str(res_m))
    q.addQueryItem("entidad", entidad)
    u.setQuery(q)
    return u


# ------------------------------ Public API ------------------------------

def download_estado_with_progress(
    entidad: str,
    cve: str,
    res_m: int,
    log_widget: QTextEdit,
    status_label: QLabel,
    progressbar: QProgressBar,
) -> None:
    """
    State-based CEM downloader with live progress feedback.
    Pipeline:
      1) Build DownloadFile.do URL for (entidad, cve, res_m).
      2) Stream ZIP to a plugin temp folder with byte-level progress reporting.
      3) Unzip and discover raster files.
      4) Add each raster to the project with Single-band Gray (Min/Max stretch).
    """
    try:
        url = build_estado_url(entidad, cve, res_m)
        log_append(log_widget, f"URL: {url.toString()}")

        tmp_dir = plugin_temp_dir() / f"estado_{cve}_{res_m}m"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        zip_path = tmp_dir / "cem_estado.zip"

        # Initial UI state
        status_label.setText("Conectando a INEGI…")
        progressbar.setRange(0, 0)
        progressbar.setValue(0)
        QApplication.processEvents()

        # Download with progress
        log_append(log_widget, f"Descargando ZIP a: {zip_path}")

        def _cb(br: int, bt: int) -> None:
            if bt <= 0:
                progressbar.setRange(0, 0)
                status_label.setText(f"Descargando… {human_size(br)}")
            else:
                progressbar.setRange(0, 100)
                pct = int((br * 100.0) / max(1, bt))
                progressbar.setValue(pct)
                status_label.setText(f"Descargando… {pct}%  ({human_size(br)}/{human_size(bt)})")
            QApplication.processEvents()

        http_get_to_file_progress(url, zip_path, progress_cb=_cb)

        # Post-download
        progressbar.setRange(0, 100)
        progressbar.setValue(100)
        status_label.setText("Descarga completa. Descomprimiendo…")
        QApplication.processEvents()

        # Unzip and locate rasters
        log_append(log_widget, "Descomprimiendo…")
        unzip_all(zip_path, tmp_dir)

        status_label.setText("Buscando rásters en el ZIP…")
        QApplication.processEvents()
        rasters = guess_raster_files(tmp_dir)
        if not rasters:
            status_label.setText("No se encontraron rásters en el ZIP.")
            log_append(log_widget, "No se encontraron rásters dentro del ZIP.")
            return

        # Add rasters to project with single-band gray styling
        status_label.setText("Agregando rásters al proyecto…")
        progressbar.setRange(0, len(rasters))
        progressbar.setValue(0)
        QApplication.processEvents()

        for i, rpath in enumerate(rasters, start=1):
            ok = add_raster_gray_with_stats(rpath)
            if ok:
                log_append(log_widget, f"Agregado al proyecto (Gris monobanda): {rpath}")
            else:
                log_append(log_widget, f"Archivo inválido (no cargado): {rpath}")
            progressbar.setValue(i)
            QApplication.processEvents()

        status_label.setText("Listo. Recuerda: los archivos están en carpeta TEMP.")
        log_append(log_widget, "Listo. Recuerda: los archivos están en carpeta TEMP.")

    except Exception as e:
        progressbar.setRange(0, 100)
        progressbar.setValue(0)
        status_label.setText("Error.")
        log_append(log_widget, f"ERROR: {e}")
