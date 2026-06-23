# -*- coding: utf-8 -*-
"""
QGIS Processing script: Normal map da heightmap/DEM
Versione metrica + RGB visuale, con bande allineate semanticamente a Metashape

Crea una normal map a 3 bande, georeferenziata, a partire da un raster DEM/heightmap.
L'output mantiene dimensioni in pixel, CRS, geotrasformazione, estensione e pixel size
esatti del raster sorgente.

MODALITA' OUTPUT
----------------
1) Float32 metrico (-1/+1), consigliato per confronto con Metashape:
   Banda 1 = n.u, componente della normale lungo l'asse U/X del raster
   Banda 2 = n.v, componente della normale lungo l'asse V/Y georeferenziato del raster
   Banda 3 = n.w, componente frontale/normale al piano heightmap
   Valori reali: -1.0 ... +1.0
   NoData: NaN

2) RGB Byte visuale (0/255):
   Banda 1 / R = n.u codificato da [-1,+1] a [0,255]
   Banda 2 / G = n.v codificato da [-1,+1] a [0,255]
   Banda 3 / B = n.w codificato da [-1,+1] a [0,255]
   Codifica: RGB = round((n + 1) * 127.5)
   NoData/background: 0,0,0

3) Entrambi:
   Il percorso scelto dall'utente viene usato per il GeoTIFF float32 metrico.
   Accanto a questo viene creato automaticamente un secondo file con suffisso _rgb.tif.

CALCOLO
-------
La normale e' calcolata dai gradienti della heightmap/DEM:
  n = (-dz/dx, +dz/dy_immagine, 1)
poi normalizzata.

SIGNIFICATO DELLE BANDE
-----------------------
Per analogia con lo script Metashape:
  u = asse orizzontale del raster, cioe' direzione positiva delle colonne / X georeferenziata;
  v = asse verticale/georeferenziato del raster, cioe' direzione positiva della Y del geotransform;
  w = asse frontale, cioe' direzione positiva della heightmap.

Nota sul segno di V/Y: nei GeoTIFF north-up il pixel size Y e' negativo, quindi le righe
aumentano verso il basso ma la Y georeferenziata positiva va verso l'alto. La formula
usa +dz/dy_immagine proprio per ottenere una banda 2 equivalente a n.v, non alla
semplice direzione riga-verso-il-basso.

Uso:
  QGIS > Processing > Toolbox > Scripts > Create New Script
  Incolla/salva questo file nella cartella degli script Processing e avvia lo script.
"""

import math
import os

import numpy as np
from osgeo import gdal

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterRasterLayer,
)


class NormalMapFromDem(QgsProcessingAlgorithm):
    INPUT = 'INPUT'
    BAND = 'BAND'
    HEIGHT_SCALE = 'HEIGHT_SCALE'
    ALIGN_DOMINANT = 'ALIGN_DOMINANT'
    RANSAC_ITERATIONS = 'RANSAC_ITERATIONS'
    RANSAC_ANGLE = 'RANSAC_ANGLE'
    OUTPUT_FORMAT = 'OUTPUT_FORMAT'
    OUTPUT = 'OUTPUT'

    # Output format enum indices
    FORMAT_FLOAT32 = 0
    FORMAT_RGB_BYTE = 1
    FORMAT_BOTH = 2

    def tr(self, string):
        return QCoreApplication.translate('NormalMapFromDem', string)

    def createInstance(self):
        return NormalMapFromDem()

    def name(self):
        return 'normal_map_from_dem'

    def displayName(self):
        return self.tr('Normal map from DEM')

    def group(self):
        return self.tr('Raster')

    def groupId(self):
        return 'raster'

    def shortHelpString(self):
        return self.tr(
            'Crea una normal map a 3 bande da una heightmap/DEM mantenendo CRS, estensione, '
            'geotrasformazione, numero di righe/colonne e dimensione dei pixel del raster sorgente.\n\n'
            'Output consigliato per confronto con lo script Metashape:\n'
            '  Float32 metrico: bande n.u, n.v, n.w con valori reali compresi tra -1 e +1.\n\n'
            'Output visuale classico:\n'
            '  RGB Byte: bande R, G, B codificate in 0-255 tramite round((n + 1) * 127.5).\n\n'
            'La normale e calcolata dai gradienti della heightmap/DEM:\n'
            '  n = (-dz/dx, +dz/dy_immagine, 1)\n'
            'poi normalizzata.\n\n'
            'Il parametro Scala verticale moltiplica le quote prima del calcolo dei gradienti. '
            'Usa 1.0 quando le unita verticali e orizzontali coincidono. Valori maggiori accentuano '
            'le pendenze; valori minori le attenuano.\n\n'
            'La correzione della normale dominante stima con RANSAC la direzione media della superficie '
            'e ruota le normali affinche tale direzione coincida con (0,0,1). E utile per pareti '
            'o superfici leggermente inclinate, perche rimuove il trend globale e conserva le variazioni locali.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT,
                self.tr('Raster heightmap/DEM sorgente')
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.BAND,
                self.tr('Banda da usare come quota/heightmap'),
                type=QgsProcessingParameterNumber.Integer,
                minValue=1,
                defaultValue=1
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.HEIGHT_SCALE,
                self.tr('Scala verticale delle quote'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.000001,
                defaultValue=1.0
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ALIGN_DOMINANT,
                self.tr('Correggi/allinea la normale dominante con RANSAC'),
                defaultValue=True
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.RANSAC_ITERATIONS,
                self.tr('Iterazioni RANSAC'),
                type=QgsProcessingParameterNumber.Integer,
                minValue=10,
                maxValue=5000,
                defaultValue=300
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.RANSAC_ANGLE,
                self.tr('Soglia angolare RANSAC in gradi'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.1,
                maxValue=90.0,
                defaultValue=15.0
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.OUTPUT_FORMAT,
                self.tr('Tipo di output'),
                options=[
                    self.tr('Float32 metrico: n.u, n.v, n.w reali -1/+1'),
                    self.tr('RGB Byte visuale: R=n.u, G=n.v, B=n.w 0-255'),
                    self.tr('Entrambi: float32 + file _rgb.tif'),
                ],
                defaultValue=self.FORMAT_BOTH
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT,
                self.tr('Output normal map GeoTIFF')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        raster_layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if raster_layer is None:
            raise QgsProcessingException(self.tr('Raster sorgente non valido.'))

        input_uri = raster_layer.dataProvider().dataSourceUri()
        input_path = input_uri.split('|')[0]
        output_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        band_index = self.parameterAsInt(parameters, self.BAND, context)
        height_scale = self.parameterAsDouble(parameters, self.HEIGHT_SCALE, context)
        align_dominant = self.parameterAsBool(parameters, self.ALIGN_DOMINANT, context)
        ransac_iterations = self.parameterAsInt(parameters, self.RANSAC_ITERATIONS, context)
        ransac_angle = self.parameterAsDouble(parameters, self.RANSAC_ANGLE, context)
        output_format = self.parameterAsEnum(parameters, self.OUTPUT_FORMAT, context)

        feedback.pushInfo(self.tr('Apertura raster sorgente...'))
        src_ds = gdal.Open(input_path, gdal.GA_ReadOnly)
        if src_ds is None:
            raise QgsProcessingException(self.tr('Impossibile aprire il raster sorgente con GDAL.'))

        width = src_ds.RasterXSize
        height = src_ds.RasterYSize
        if width < 2 or height < 2:
            raise QgsProcessingException(self.tr('Il raster deve avere almeno 2 righe e 2 colonne.'))

        if band_index < 1 or band_index > src_ds.RasterCount:
            raise QgsProcessingException(
                self.tr('Banda richiesta non valida. Il raster contiene {} bande.').format(src_ds.RasterCount)
            )

        gt = src_ds.GetGeoTransform(can_return_null=True)
        if gt is None:
            raise QgsProcessingException(self.tr('Il raster sorgente non ha una geotrasformazione valida.'))
        projection = src_ds.GetProjection()

        # Distanza reale tra centri-pixel lungo asse colonna e asse riga.
        # Funziona anche con raster ruotati, pur mantenendo la stessa geotrasformazione in output.
        x_spacing = math.hypot(float(gt[1]), float(gt[4]))
        y_spacing = math.hypot(float(gt[2]), float(gt[5]))
        if x_spacing <= 0 or y_spacing <= 0:
            raise QgsProcessingException(self.tr('Dimensione pixel non valida.'))

        feedback.pushInfo(self.tr('Dimensioni: {} colonne x {} righe').format(width, height))
        feedback.pushInfo(self.tr('Pixel size reale: X={} ; Y={}').format(x_spacing, y_spacing))
        feedback.pushInfo(self.tr('Convenzione bande: banda 1 = n.u / asse X raster; banda 2 = n.v / asse Y georeferenziato; banda 3 = n.w / frontale heightmap.'))

        src_band = src_ds.GetRasterBand(band_index)
        nodata = src_band.GetNoDataValue()

        feedback.setProgressText(self.tr('Lettura heightmap/DEM...'))
        arr = src_band.ReadAsArray()
        if arr is None:
            raise QgsProcessingException(self.tr('Impossibile leggere la banda richiesta.'))
        arr = arr.astype(np.float32)

        valid = np.isfinite(arr)
        if nodata is not None:
            valid &= (arr != nodata)

        valid_count = int(np.count_nonzero(valid))
        if valid_count == 0:
            raise QgsProcessingException(self.tr('Nessun pixel valido nella heightmap/DEM.'))

        z = arr * np.float32(height_scale)
        feedback.setProgress(20)

        feedback.setProgressText(self.tr('Calcolo gradienti dz/dx e dz/dy...'))
        dzdx, dzdy = self._safe_gradients(z, valid, x_spacing, y_spacing)
        feedback.setProgress(45)

        feedback.setProgressText(self.tr('Calcolo e normalizzazione delle normali...'))
        nx = -dzdx
        ny = dzdy
        nz = np.ones_like(z, dtype=np.float32)

        norm = np.sqrt(nx * nx + ny * ny + nz * nz)
        normal_valid = valid & np.isfinite(norm) & (norm > 0)

        nx_out = np.full_like(z, np.nan, dtype=np.float32)
        ny_out = np.full_like(z, np.nan, dtype=np.float32)
        nz_out = np.full_like(z, np.nan, dtype=np.float32)

        nx_out[normal_valid] = nx[normal_valid] / norm[normal_valid]
        ny_out[normal_valid] = ny[normal_valid] / norm[normal_valid]
        nz_out[normal_valid] = nz[normal_valid] / norm[normal_valid]
        feedback.setProgress(60)

        if align_dominant:
            feedback.setProgressText(self.tr('Stima RANSAC della normale dominante...'))
            dominant = self._dominant_normal_ransac(
                nx_out,
                ny_out,
                nz_out,
                normal_valid,
                iterations=ransac_iterations,
                angle_deg=ransac_angle,
                feedback=feedback
            )
            feedback.pushInfo(
                self.tr('Normale dominante stimata: [{:.6f}, {:.6f}, {:.6f}]').format(
                    dominant[0], dominant[1], dominant[2]
                )
            )

            rotation = self._rotation_matrix_from_vectors(dominant, np.array([0.0, 0.0, 1.0], dtype=np.float64))
            nx_out, ny_out, nz_out = self._apply_rotation(nx_out, ny_out, nz_out, normal_valid, rotation)
            normal_valid = np.isfinite(nx_out) & np.isfinite(ny_out) & np.isfinite(nz_out)
            feedback.setProgress(78)

        outputs = {}

        if output_format in (self.FORMAT_FLOAT32, self.FORMAT_BOTH):
            feedback.setProgressText(self.tr('Scrittura GeoTIFF float32 metrico n.u/n.v/n.w -1/+1...'))
            metric_path = output_path
            self._write_geotiff_float32(metric_path, nx_out, ny_out, nz_out, normal_valid, gt, projection)
            outputs[self.OUTPUT] = metric_path
            feedback.pushInfo(self.tr('Output metrico float32 salvato: {}').format(metric_path))

        if output_format in (self.FORMAT_RGB_BYTE, self.FORMAT_BOTH):
            feedback.setProgressText(self.tr('Codifica e scrittura GeoTIFF RGB Byte 0-255...'))
            rgb_path = output_path if output_format == self.FORMAT_RGB_BYTE else self._rgb_sibling_path(output_path)
            rgb = self._encode_rgb_byte(nx_out, ny_out, nz_out, normal_valid)
            self._write_geotiff_rgb(rgb_path, rgb, gt, projection)
            if output_format == self.FORMAT_RGB_BYTE:
                outputs[self.OUTPUT] = rgb_path
            else:
                outputs['OUTPUT_RGB'] = rgb_path
            feedback.pushInfo(self.tr('Output RGB Byte salvato: {}').format(rgb_path))

        feedback.setProgress(100)
        src_ds = None
        feedback.pushInfo(self.tr('Normal map completata.'))
        return outputs

    def _safe_gradients(self, z, valid, x_spacing, y_spacing):
        """Calcola gradienti evitando che NoData/NaN contaminino i pixel validi."""
        dzdx = np.full(z.shape, np.nan, dtype=np.float32)
        dzdy = np.full(z.shape, np.nan, dtype=np.float32)

        # Gradiente X: differenze centrali; bordi con differenze in avanti/indietro.
        if z.shape[1] > 2:
            mask = valid[:, 1:-1] & valid[:, :-2] & valid[:, 2:]
            values = (z[:, 2:] - z[:, :-2]) / np.float32(2.0 * x_spacing)
            sub = dzdx[:, 1:-1]
            sub[mask] = values[mask]

        mask_left = valid[:, 0] & valid[:, 1]
        left = dzdx[:, 0]
        left[mask_left] = (z[:, 1][mask_left] - z[:, 0][mask_left]) / np.float32(x_spacing)

        mask_right = valid[:, -1] & valid[:, -2]
        right = dzdx[:, -1]
        right[mask_right] = (z[:, -1][mask_right] - z[:, -2][mask_right]) / np.float32(x_spacing)

        # Se il raster ha solo 2 colonne, completa anche la seconda con la stessa logica.
        if z.shape[1] == 2:
            mask_col1 = valid[:, 1] & valid[:, 0]
            col1 = dzdx[:, 1]
            col1[mask_col1] = (z[:, 1][mask_col1] - z[:, 0][mask_col1]) / np.float32(x_spacing)

        # Gradiente Y: differenze centrali; bordi con differenze in avanti/indietro.
        if z.shape[0] > 2:
            mask = valid[1:-1, :] & valid[:-2, :] & valid[2:, :]
            values = (z[2:, :] - z[:-2, :]) / np.float32(2.0 * y_spacing)
            sub = dzdy[1:-1, :]
            sub[mask] = values[mask]

        mask_top = valid[0, :] & valid[1, :]
        top = dzdy[0, :]
        top[mask_top] = (z[1, :][mask_top] - z[0, :][mask_top]) / np.float32(y_spacing)

        mask_bottom = valid[-1, :] & valid[-2, :]
        bottom = dzdy[-1, :]
        bottom[mask_bottom] = (z[-1, :][mask_bottom] - z[-2, :][mask_bottom]) / np.float32(y_spacing)

        # Se il raster ha solo 2 righe, completa anche la seconda con la stessa logica.
        if z.shape[0] == 2:
            mask_row1 = valid[1, :] & valid[0, :]
            row1 = dzdy[1, :]
            row1[mask_row1] = (z[1, :][mask_row1] - z[0, :][mask_row1]) / np.float32(y_spacing)

        return dzdx, dzdy

    def _dominant_normal_ransac(self, nx, ny, nz, valid_mask, iterations, angle_deg, feedback=None):
        """Stima robusta della normale dominante usando RANSAC su un campione di normali."""
        flat_valid = np.flatnonzero(valid_mask.ravel())
        if flat_valid.size < 10:
            return np.array([0.0, 0.0, 1.0], dtype=np.float64)

        max_sample = min(flat_valid.size, 200000)
        rng = np.random.default_rng(12345)
        sample_idx = rng.choice(flat_valid, size=max_sample, replace=False)

        normals = np.column_stack((
            nx.ravel()[sample_idx],
            ny.ravel()[sample_idx],
            nz.ravel()[sample_idx],
        )).astype(np.float64)

        norms = np.linalg.norm(normals, axis=1)
        keep = np.isfinite(norms) & (norms > 0)
        normals = normals[keep] / norms[keep, None]
        if normals.shape[0] < 10:
            return np.array([0.0, 0.0, 1.0], dtype=np.float64)

        cos_threshold = math.cos(math.radians(angle_deg))
        best_count = -1
        best_candidate = normals[0]
        best_inliers = None

        n = normals.shape[0]
        for i in range(iterations):
            if feedback is not None and feedback.isCanceled():
                break

            candidate = normals[rng.integers(0, n)]
            if candidate[2] < 0:
                candidate = -candidate

            dots = normals @ candidate
            inliers = dots >= cos_threshold
            count = int(np.count_nonzero(inliers))

            if count > best_count:
                best_count = count
                best_candidate = candidate
                best_inliers = inliers

        if best_inliers is not None and best_count > 0:
            dominant = normals[best_inliers].mean(axis=0)
        else:
            dominant = best_candidate

        dominant_norm = np.linalg.norm(dominant)
        if not np.isfinite(dominant_norm) or dominant_norm == 0:
            dominant = np.array([0.0, 0.0, 1.0], dtype=np.float64)
        else:
            dominant = dominant / dominant_norm

        if dominant[2] < 0:
            dominant = -dominant

        return dominant.astype(np.float64)

    def _rotation_matrix_from_vectors(self, source, target):
        """Matrice di rotazione che porta source su target."""
        a = np.array(source, dtype=np.float64)
        b = np.array(target, dtype=np.float64)
        a = a / np.linalg.norm(a)
        b = b / np.linalg.norm(b)

        v = np.cross(a, b)
        c = float(np.dot(a, b))
        s = float(np.linalg.norm(v))

        if s < 1e-12:
            if c > 0:
                return np.eye(3, dtype=np.float64)
            # Caso opposto quasi impossibile qui, ma gestito per completezza.
            return np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]], dtype=np.float64)

        k = np.array([
            [0.0, -v[2], v[1]],
            [v[2], 0.0, -v[0]],
            [-v[1], v[0], 0.0],
        ], dtype=np.float64)

        return np.eye(3, dtype=np.float64) + k + (k @ k) * ((1.0 - c) / (s * s))

    def _apply_rotation(self, nx, ny, nz, valid_mask, rotation):
        """Applica la rotazione alle tre componenti e rinormalizza."""
        nx2 = np.full_like(nx, np.nan, dtype=np.float32)
        ny2 = np.full_like(ny, np.nan, dtype=np.float32)
        nz2 = np.full_like(nz, np.nan, dtype=np.float32)

        x = nx[valid_mask].astype(np.float64)
        y = ny[valid_mask].astype(np.float64)
        z = nz[valid_mask].astype(np.float64)

        rx = rotation[0, 0] * x + rotation[0, 1] * y + rotation[0, 2] * z
        ry = rotation[1, 0] * x + rotation[1, 1] * y + rotation[1, 2] * z
        rz = rotation[2, 0] * x + rotation[2, 1] * y + rotation[2, 2] * z

        length = np.sqrt(rx * rx + ry * ry + rz * rz)
        good = np.isfinite(length) & (length > 0)

        valid_positions = np.flatnonzero(valid_mask.ravel())
        good_positions = valid_positions[good]

        nx2_flat = nx2.ravel()
        ny2_flat = ny2.ravel()
        nz2_flat = nz2.ravel()

        nx2_flat[good_positions] = (rx[good] / length[good]).astype(np.float32)
        ny2_flat[good_positions] = (ry[good] / length[good]).astype(np.float32)
        nz2_flat[good_positions] = (rz[good] / length[good]).astype(np.float32)

        return nx2, ny2, nz2

    def _encode_rgb_byte(self, nx, ny, nz, valid_mask):
        """Codifica n.u,n.v,n.w float -1/+1 come RGB Byte 0-255."""
        height, width = nx.shape
        rgb = np.zeros((3, height, width), dtype=np.uint8)
        rgb[0, valid_mask] = np.clip(np.rint((nx[valid_mask] + 1.0) * 127.5), 0, 255).astype(np.uint8)
        rgb[1, valid_mask] = np.clip(np.rint((ny[valid_mask] + 1.0) * 127.5), 0, 255).astype(np.uint8)
        rgb[2, valid_mask] = np.clip(np.rint((nz[valid_mask] + 1.0) * 127.5), 0, 255).astype(np.uint8)
        return rgb

    def _rgb_sibling_path(self, output_path):
        root, ext = os.path.splitext(output_path)
        if ext.lower() not in ('.tif', '.tiff'):
            return output_path + '_rgb.tif'
        return root + '_rgb.tif'

    def _write_geotiff_float32(self, output_path, nx, ny, nz, valid_mask, geotransform, projection):
        driver = gdal.GetDriverByName('GTiff')
        if driver is None:
            raise QgsProcessingException(self.tr('Driver GeoTIFF non disponibile.'))

        if os.path.exists(output_path):
            driver.Delete(output_path)

        height, width = nx.shape
        options = ['COMPRESS=LZW', 'TILED=YES', 'BIGTIFF=IF_SAFER', 'INTERLEAVE=BAND']
        out_ds = driver.Create(output_path, width, height, 3, gdal.GDT_Float32, options=options)
        if out_ds is None:
            raise QgsProcessingException(self.tr('Impossibile creare il raster output float32.'))

        out_ds.SetGeoTransform(geotransform)
        if projection:
            out_ds.SetProjection(projection)

        bands = [nx, ny, nz]
        descriptions = [
            "n.u - componente lungo asse U/X del raster, valore reale -1/+1",
            "n.v - componente lungo asse V/Y georeferenziato del raster, valore reale -1/+1",
            "n.w - componente frontale/normale al piano heightmap, valore reale -1/+1",
        ]
        nodata_value = float('nan')
        for i, arr in enumerate(bands):
            band = out_ds.GetRasterBand(i + 1)
            band.WriteArray(arr.astype(np.float32, copy=False))
            band.SetDescription(descriptions[i])
            band.SetNoDataValue(nodata_value)
            band.FlushCache()

        out_ds.SetMetadataItem('NORMAL_MAP_TYPE', 'metric_float32_direction_cosines')
        out_ds.SetMetadataItem('NORMAL_COMPONENT_RANGE', '-1,+1')
        out_ds.SetMetadataItem('BAND_1', 'n.u - asse U/X del raster')
        out_ds.SetMetadataItem('BAND_2', 'n.v - asse V/Y georeferenziato del raster')
        out_ds.SetMetadataItem('BAND_3', 'n.w - asse frontale/height')
        out_ds.SetMetadataItem('METASHAPE_SEMANTIC_EQUIVALENCE', 'band1=n.u, band2=n.v, band3=n.w')
        out_ds.FlushCache()
        out_ds = None

    def _write_geotiff_rgb(self, output_path, rgb, geotransform, projection):
        driver = gdal.GetDriverByName('GTiff')
        if driver is None:
            raise QgsProcessingException(self.tr('Driver GeoTIFF non disponibile.'))

        if os.path.exists(output_path):
            driver.Delete(output_path)

        height = rgb.shape[1]
        width = rgb.shape[2]
        options = ['COMPRESS=LZW', 'TILED=YES', 'BIGTIFF=IF_SAFER', 'INTERLEAVE=PIXEL']
        out_ds = driver.Create(output_path, width, height, 3, gdal.GDT_Byte, options=options)
        if out_ds is None:
            raise QgsProcessingException(self.tr('Impossibile creare il raster output RGB.'))

        out_ds.SetGeoTransform(geotransform)
        if projection:
            out_ds.SetProjection(projection)

        color_interps = [gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand]
        descriptions = [
            "R = n.u codificato da -1/+1 a 0/255",
            "G = n.v codificato da -1/+1 a 0/255",
            "B = n.w codificato da -1/+1 a 0/255",
        ]
        for i in range(3):
            band = out_ds.GetRasterBand(i + 1)
            band.WriteArray(rgb[i])
            band.SetColorInterpretation(color_interps[i])
            band.SetDescription(descriptions[i])
            band.SetNoDataValue(0)
            band.FlushCache()

        out_ds.SetMetadataItem('NORMAL_MAP_TYPE', 'visual_rgb_byte')
        out_ds.SetMetadataItem('NORMAL_COMPONENT_ENCODING', 'byte = round((component + 1) * 127.5)')
        out_ds.SetMetadataItem('BAND_1', 'R = n.u encoded')
        out_ds.SetMetadataItem('BAND_2', 'G = n.v encoded')
        out_ds.SetMetadataItem('BAND_3', 'B = n.w encoded')
        out_ds.SetMetadataItem('METASHAPE_SEMANTIC_EQUIVALENCE', 'R/band1=n.u, G/band2=n.v, B/band3=n.w')
        out_ds.FlushCache()
        out_ds = None
