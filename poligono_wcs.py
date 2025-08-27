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

from qgis.PyQt.QtCore import QUrl, QStandardPaths, QEventLoop, QVariant
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.PyQt.QtWidgets import QApplication, QTextEdit, QLabel, QProgressBar
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsVectorFileWriter, QgsCoordinateTransformContext,
    QgsNetworkAccessManager, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsRasterLayer, QgsSingleBandGrayRenderer, QgsContrastEnhancement, QgsRasterBandStats,
    QgsVectorDataProvider, QgsField, QgsFeature, QgsGeometry
)
import processing

# ------------------ Configuration ------------------
WCS_BASE = "https://gaia.inegi.org.mx/geoserver/wcs"
WCS_COVERAGE_ID = "cem30_workespace:cem3_r15"
NODATA_VALUE = -9999
VALID_RES = [15, 30, 60, 90, 120]


# ------------------ Utilities ------------------
def plugin_temp_dir() -> Path:
    """
    Returns a temp directory dedicated to this plugin. The directory is created if missing.
    Uses QStandardPaths to respect the OS temp location used by QGIS.
    """
    base = Path(QStandardPaths.writableLocation(QStandardPaths.TempLocation))
    d = base / "CEM_QGIS_Temp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_append(widget: QTextEdit, msg: str) -> None:
    """
    Appends a message to a QTextEdit and ensures the view follows the cursor.
    """
    widget.append(msg)
    widget.ensureCursorVisible()


def human_size(n: float) -> str:
    """
    Formats a byte count into a human-readable string (IEC-like, simple).
    """
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024.0:
            return f"{f:.1f} {u}"
        f /= 1024.0
    return f"{f:.1f} PB"


def meters_to_deg_step(res_m: int) -> float:
    """
    Maps a meter resolution to an angular pixel size in degrees, using INEGI's arcsecond scheme.
    15→0.5", 30→1", 60→2", 90→3", 120→4".
    """
    table = {15: 0.5, 30: 1.0, 60: 2.0, 90: 3.0, 120: 4.0}
    if res_m not in table:
        raise ValueError("Resolución inválida (usa 15,30,60,90,120).")
    return table[res_m] / 3600.0


def build_wcs_getcoverage_url(bbox4326, res_m: int) -> QUrl:
    """
    Builds a WCS 1.0.0 GetCoverage URL for the given bbox (EPSG:4326) and resolution.
    The server resamples based on resx/resy, format GeoTIFF.
    """
    minx, miny, maxx, maxy = bbox4326
    step = meters_to_deg_step(res_m)
    from qgis.PyQt.QtCore import QUrlQuery
    u = QUrl(WCS_BASE)
    q = QUrlQuery()
    q.addQueryItem("request", "GetCoverage")
    q.addQueryItem("service", "WCS")
    q.addQueryItem("version", "1.0.0")
    q.addQueryItem("coverage", WCS_COVERAGE_ID)
    q.addQueryItem("crs", "EPSG:4326")
    q.addQueryItem("bbox", f"{minx:.8f},{miny:.8f},{maxx:.8f},{maxy:.8f}")
    q.addQueryItem("resx", f"{step:.7f}")
    q.addQueryItem("resy", f"{step:.7f}")
    q.addQueryItem("format", "GeoTIFF")
    u.setQuery(q)
    return u


def http_get_to_file_progress(url: QUrl, out_path: Path, progress_cb=None) -> None:
    """
    Streams a HTTP GET to disk using QgsNetworkAccessManager, honoring QGIS proxy/SSL.
    Optionally reports progress via a callback: progress_cb(bytes_received, bytes_total|0).
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
    Loads a raster and enforces a single-band gray renderer with Stretch to Min/Max.
    Computes band statistics; retries with a sample count if min == max (e.g., 0–0).
    """
    layer = QgsRasterLayer(str(path), path.stem, "gdal")
    if not layer.isValid():
        return False

    provider = layer.dataProvider()
    band = 1
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


def _explode_to_singleparts(layer: QgsVectorLayer) -> QgsVectorLayer:
    """
    Ensures a one-polygon-per-feature layer by converting multipart features to singleparts
    via the native QGIS processing algorithm. Returns an in-memory layer.
    """
    res = processing.run("native:multiparttosingleparts", {"INPUT": layer, "OUTPUT": "memory:"})
    return res["OUTPUT"]


def _write_single_polygon_geojson_4326(geom: QgsGeometry, src_crs, out_path: Path) -> None:
    """
    Builds a one-feature in-memory layer with the given polygon geometry, reprojects it to
    EPSG:4326, and writes it to GeoJSON. Handles writer return arities across QGIS versions.
    """
    crs_dst = QgsCoordinateReferenceSystem("EPSG:4326")
    ct = QgsCoordinateTransform(src_crs, crs_dst, QgsProject.instance().transformContext())
    geom_4326 = QgsGeometry(geom)
    geom_4326.transform(ct)

    mem = QgsVectorLayer("Polygon?crs=EPSG:4326", "mask", "memory")
    pr: QgsVectorDataProvider = mem.dataProvider()
    pr.addAttributes([QgsField("id", QVariant.Int)])
    mem.updateFields()

    feat = QgsFeature(mem.fields())
    feat.setGeometry(geom_4326)
    feat.setAttribute("id", 1)
    pr.addFeatures([feat])
    mem.updateExtents()

    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GeoJSON"
    tr_ctx = QgsCoordinateTransformContext()
    result = QgsVectorFileWriter.writeAsVectorFormatV2(mem, str(out_path), tr_ctx, opts)

    err_code = result[0] if isinstance(result, tuple) else result
    if err_code != QgsVectorFileWriter.NoError:
        err_msg = result[1] if (isinstance(result, tuple) and len(result) > 1) else ""
        raise RuntimeError(f"No se pudo exportar máscara GeoJSON: {err_msg}")


def _bbox_4326_from_geom(geom: QgsGeometry, src_crs):
    """
    Returns the geometry bounding box transformed to EPSG:4326 as (minx, miny, maxx, maxy).
    """
    crs_dst = QgsCoordinateReferenceSystem("EPSG:4326")
    ct = QgsCoordinateTransform(src_crs, crs_dst, QgsProject.instance().transformContext())
    g = QgsGeometry(geom)
    g.transform(ct)
    r = g.boundingBox()
    return (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())


# ------------------ Public action: one GeoTIFF per polygon ------------------
def download_poligono_wcs_split_per_polygon(
    layer: QgsVectorLayer,
    res_m: int,
    log_widget: QTextEdit,
    status_label: QLabel = None,
    progressbar: QProgressBar = None
) -> None:
    """
    Downloads CEM via WCS and generates one clipped GeoTIFF per polygon in the input layer.
    If the input contains multipart geometries, they are split to singleparts first.

    Per-polygon pipeline:
      1) Compute EPSG:4326 bbox padded by one pixel step (to avoid edge cuts).
      2) Request GeoTIFF via WCS GetCoverage for that bbox.
      3) Clip the GeoTIFF to the polygon mask (GeoJSON) using gdal:cliprasterbymasklayer.
      4) Load the result in QGIS with Single-band Gray and stretched Min/Max.
    """
    if res_m not in VALID_RES:
        raise ValueError("Resolución inválida. Usa 15,30,60,90 o 120 m.")

    single = _explode_to_singleparts(layer)
    feats = list(single.getFeatures())
    total = len(feats)
    if total == 0:
        log_append(log_widget, "La capa no contiene polígonos.")
        return

    if status_label and progressbar:
        status_label.setText(f"Preparando {total} polígonos…")
        progressbar.setRange(0, total)
        progressbar.setValue(0)
        QApplication.processEvents()

    tmp_root = plugin_temp_dir() / f"wcs_{layer.name()}_{res_m}m"
    tmp_root.mkdir(parents=True, exist_ok=True)

    for i, f in enumerate(feats, start=1):
        try:
            geom = f.geometry()

            minx, miny, maxx, maxy = _bbox_4326_from_geom(geom, single.crs())
            step_deg = meters_to_deg_step(res_m)
            pad = step_deg
            bbox = (minx - pad, miny - pad, maxx + pad, maxy + pad)

            url = build_wcs_getcoverage_url(bbox, res_m)
            log_append(log_widget, f"[{i}/{total}] WCS GetCoverage: {url.toString()}")

            tif_raw = tmp_root / f"wcs_raw_poly_{i:04d}.tif"
            mask_geojson = tmp_root / f"mask_poly_{i:04d}.geojson"
            out_tif = tmp_root / f"{layer.name()}__poly{i:04d}__cem_{res_m}m_clip.tif"

            _write_single_polygon_geojson_4326(geom, single.crs(), mask_geojson)

            if status_label:
                status_label.setText(f"[{i}/{total}] Descargando WCS {res_m} m…")
                QApplication.processEvents()

            def _cb(br, bt):
                if status_label:
                    if bt <= 0:
                        status_label.setText(f"[{i}/{total}] Descargando… {human_size(br)}")
                    else:
                        pct = int(br * 100.0 / max(1, bt))
                        status_label.setText(f"[{i}/{total}] Descargando… {pct}% ({human_size(br)}/{human_size(bt)})")
                    QApplication.processEvents()

            http_get_to_file_progress(url, tif_raw, progress_cb=_cb)

            if status_label:
                status_label.setText(f"[{i}/{total}] Recortando (gdal:cliprasterbymasklayer)…")
                QApplication.processEvents()

            params = {
                "INPUT": str(tif_raw),
                "MASK": str(mask_geojson),
                "CROP_TO_CUTLINE": True,
                "KEEP_RESOLUTION": True,
                "NODATA": NODATA_VALUE,
                "ALPHA_BAND": True,
                "OUTPUT": str(out_tif),
            }
            processing.run("gdal:cliprasterbymasklayer", params)

            ok = add_raster_gray_with_stats(out_tif)
            if ok:
                log_append(log_widget, f"[{i}/{total}] OK → {out_tif}")
            else:
                log_append(log_widget, f"[{i}/{total}] Archivo inválido (no cargado): {out_tif}")

        except Exception as e:
            log_append(log_widget, f"[{i}/{total}] ERROR: {e}")

        finally:
            if progressbar:
                progressbar.setValue(i)
                QApplication.processEvents()

    if status_label and progressbar:
        status_label.setText("Listo. Se generó un TIFF por polígono en TEMP. Guarda copias para conservarlos.")
        QApplication.processEvents()
    log_append(log_widget, "Listo. Resultado en TEMP (un TIFF por polígono).")
