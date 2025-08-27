# Changelog
Todos los cambios notables de este proyecto.

## [0.1.0] - 2025-08-27
### Added
- Descarga CEM V3 por estado (ZIP de INEGI) con progreso en vivo usando `QgsNetworkAccessManager`.
- Descarga por polígono vía **WCS GetCoverage** y recorte por máscara con `gdal:cliprasterbymasklayer`.
- Carga automática de rásters al proyecto con simbología **Gris monobanda** (Stretch Min/Max).
- Manejo de archivos temporales en la carpeta del sistema y mensajes de estado/progreso en la UI.

