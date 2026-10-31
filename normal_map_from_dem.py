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
   Banda 1 = n.u, componente della normale lungo +X georeferenziato
   Banda 2 = n.v, componente della normale lungo +Y georeferenziato
   Banda 3 = n.w, componente frontale/normale al piano heightmap
   Valori reali: -1.0 ... +1.0
   NoData: NaN

2) RGB Byte visuale (1/255):
   Banda 1 / R = n.u, Banda 2 / G = n.v, Banda 3 / B = n.w
   Codifica: byte = round((n + 1) * 127) + 1   ->  -1 = 1, 0 = 128, +1 = 255
   Lo 0 e' riservato al NoData, quindi nessun pixel valido viene mascherato.
   Decodifica: n = (byte - 1) / 127 - 1
   Opzione DirectX: inverte il canale G (convenzione OpenGL = G verso +Y).

3) Entrambi:
   Il percorso scelto dall'utente viene usato per il GeoTIFF float32 metrico.
   Accanto a questo viene creato un secondo file con suffisso _rgb.tif, dichiarato
   come output OUTPUT_RGB e caricato automaticamente nel progetto.

CALCOLO
-------
1) Derivate nello spazio pixel (dz/dcolonna, dz/driga):
   - kernel di Horn 3x3 (default, come gdaldem), meno sensibile al rumore;
   - oppure differenze centrali.
   In prossimita' di bordi e NoData si scende automaticamente a differenze
   centrali e poi a differenze in avanti/indietro, invece di perdere il pixel.
2) Conversione in derivate georeferenziate tramite lo Jacobiano della
   geotrasformazione: [dz/dX, dz/dY] = inv(J)^T [dz/dc, dz/dr],
   J = [[gt1, gt2], [gt4, gt5]]. Gestisce correttamente raster north-up,
   south-up, ruotati e con pixel anisotropi.
3) n = (-dz/dX, -dz/dY, 1), poi normalizzata.

Il raster viene elaborato a blocchi di righe (con 1 px di bordo), quindi la
memoria occupata non dipende dalle dimensioni del DEM. Con la correzione RANSAC
attiva i passaggi sono due: campionamento delle normali, poi rotazione e scrittura.

REQUISITI
---------
- Il raster deve essere letto dal provider GDAL (file, GPKG, VRT...).
- Il CRS non deve essere geografico: pixel in gradi e quote in metri danno
  normali prive di significato. Riproiettare prima in un CRS metrico.
  Raster senza CRS (es. heightmap di paramenti in sistema locale) sono ammessi:
  si assume che unita' orizzontali e verticali coincidano (vedi Scala verticale).

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
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingOutputRasterLayer,
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
    GRADIENT_METHOD = 'GRADIENT_METHOD'
    ALIGN_DOMINANT = 'ALIGN_DOMINANT'
    RANSAC_ITERATIONS = 'RANSAC_ITERATIONS'
    RANSAC_ANGLE = 'RANSAC_ANGLE'
    OUTPUT_FORMAT = 'OUTPUT_FORMAT'
    RGB_DIRECTX = 'RGB_DIRECTX'
    OUTPUT = 'OUTPUT'
    OUTPUT_RGB = 'OUTPUT_RGB'

    # Output format enum indices
    FORMAT_FLOAT32 = 0
    FORMAT_RGB_BYTE = 1
    FORMAT_BOTH = 2

    # Gradient method enum indices
    GRADIENT_HORN = 0
    GRADIENT_CENTRAL = 1

    # Pixel per blocco di elaborazione (controlla il picco di memoria)
    BLOCK_PIXELS = 1000000
    # Numero massimo di normali campionate per il RANSAC
    RANSAC_MAX_SAMPLE = 200000

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
            '  Float32 metrico: bande n.u (+X), n.v (+Y), n.w con valori reali tra -1 e +1.\n\n'
            'Output visuale classico:\n'
            '  RGB Byte: round((n + 1) * 127) + 1, range 1-255 (0 = NoData). '
            'Convenzione OpenGL (G verso +Y); attivare l\'opzione DirectX per invertire G.\n\n'
            'Gradienti: kernel di Horn 3x3 (default, come gdaldem) oppure differenze centrali, '
            'con ripiego automatico su differenze centrali/unilaterali vicino a bordi e NoData. '
            'Le derivate vengono convertite in X/Y georeferenziati tramite la geotrasformazione, '
            'quindi i raster south-up o ruotati sono gestiti correttamente.\n\n'
            'Il CRS non puo essere geografico (gradi): riproiettare prima in un CRS metrico.\n\n'
            'Il parametro Scala verticale moltiplica le quote prima del calcolo dei gradienti. '
            'Usa 1.0 quando le unita verticali e orizzontali coincidono.\n\n'
            'La correzione della normale dominante (RANSAC) ruota le normali affinche la direzione '
            'media della superficie coincida con (0,0,1). Utile per heightmap di pareti leggermente '
            'inclinate; da lasciare disattivata per DEM di terreno, perche elimina l\'esposizione reale.'
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
            QgsProcessingParameterEnum(
                self.GRADIENT_METHOD,
                self.tr('Metodo di calcolo dei gradienti'),
                options=[
                    self.tr('Kernel di Horn 3x3 (come gdaldem)'),
                    self.tr('Differenze centrali'),
                ],
                defaultValue=self.GRADIENT_HORN
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ALIGN_DOMINANT,
                self.tr('Correggi/allinea la normale dominante con RANSAC (heightmap di pareti)'),
                defaultValue=False
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
                    self.tr('RGB Byte visuale: R=n.u, G=n.v, B=n.w 1-255'),
                    self.tr('Entrambi: float32 + file _rgb.tif'),
                ],
                defaultValue=self.FORMAT_BOTH
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.RGB_DIRECTX,
                self.tr('RGB in convenzione DirectX (inverti canale G)'),
                defaultValue=False
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT,
                self.tr('Output normal map GeoTIFF')
            )
        )

        self.addOutput(
            QgsProcessingOutputRasterLayer(
                self.OUTPUT_RGB,
                self.tr('Normal map RGB (modalita Entrambi)')
            )
        )

    # ------------------------------------------------------------------
    # Algoritmo principale
    # ------------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):
        raster_layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if raster_layer is None:
            raise QgsProcessingException(self.tr('Raster sorgente non valido.'))

        output_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        band_index = self.parameterAsInt(parameters, self.BAND, context)
        height_scale = self.parameterAsDouble(parameters, self.HEIGHT_SCALE, context)
        gradient_method = self.parameterAsEnum(parameters, self.GRADIENT_METHOD, context)
        align_dominant = self.parameterAsBool(parameters, self.ALIGN_DOMINANT, context)
        ransac_iterations = self.parameterAsInt(parameters, self.RANSAC_ITERATIONS, context)
        ransac_angle = self.parameterAsDouble(parameters, self.RANSAC_ANGLE, context)
        output_format = self.parameterAsEnum(parameters, self.OUTPUT_FORMAT, context)
        rgb_directx = self.parameterAsBool(parameters, self.RGB_DIRECTX, context)

        # --- CRS: niente coordinate geografiche ---
        crs = raster_layer.crs()
        if crs.isValid() and crs.isGeographic():
            raise QgsProcessingException(self.tr(
                'Il raster e in un CRS geografico ({}): pixel in gradi e quote in metri danno '
                'normali errate. Riproietta il DEM in un CRS metrico (es. UTM) e riprova.'
            ).format(crs.authid()))
        if not crs.isValid():
            feedback.pushWarning(self.tr(
                'Raster senza CRS: si assume che unita orizzontali e verticali coincidano '
                '(regolare eventualmente la Scala verticale).'
            ))

        # --- Apertura con GDAL ---
        src_ds = self._open_with_gdal(raster_layer)

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

        # Jacobiano pixel -> coordinate: [dX, dY] = J [dc, dr]
        jac = np.array([[gt[1], gt[2]], [gt[4], gt[5]]], dtype=np.float64)
        if abs(np.linalg.det(jac)) < 1e-30:
            raise QgsProcessingException(self.tr('Geotrasformazione degenere (pixel size nullo).'))
        inv_jac_t = np.linalg.inv(jac).T

        feedback.pushInfo(self.tr('Dimensioni: {} colonne x {} righe').format(width, height))
        feedback.pushInfo(self.tr('Pixel size: X={} ; Y={}').format(
            math.hypot(gt[1], gt[4]), math.hypot(gt[2], gt[5])))
        feedback.pushInfo(self.tr('Metodo gradienti: {}').format(
            'Horn 3x3' if gradient_method == self.GRADIENT_HORN else 'differenze centrali'))
        feedback.pushInfo(self.tr(
            'Convenzione bande: banda 1 = n.u / +X georeferenziato; banda 2 = n.v / +Y '
            'georeferenziato; banda 3 = n.w / frontale heightmap.'))

        src_band = src_ds.GetRasterBand(band_index)
        mask_band = src_band.GetMaskBand()
        block_rows = max(16, self.BLOCK_PIXELS // width)
        blocks = [(r0, min(r0 + block_rows, height)) for r0 in range(0, height, block_rows)]

        def normals_for_block(r0, r1):
            return self._compute_block_normals(
                src_band, mask_band, width, height, r0, r1,
                height_scale, gradient_method, inv_jac_t
            )

        # --- Passaggio 1 (opzionale): campionamento per RANSAC ---
        rotation = None
        progress_start = 0.0
        if align_dominant:
            feedback.setProgressText(self.tr('Campionamento normali per RANSAC...'))
            rng = np.random.default_rng(12345)
            probability = min(1.0, self.RANSAC_MAX_SAMPLE / float(width * height))
            samples = []
            for i, (r0, r1) in enumerate(blocks):
                if feedback.isCanceled():
                    return {}
                nx, ny, nz, valid = normals_for_block(r0, r1)
                idx = np.flatnonzero(valid)
                if idx.size:
                    idx = idx[rng.random(idx.size) < probability]
                    samples.append(np.column_stack((
                        nx.ravel()[idx], ny.ravel()[idx], nz.ravel()[idx])))
                feedback.setProgress(40.0 * (i + 1) / len(blocks))
            progress_start = 40.0

            if not samples or sum(s.shape[0] for s in samples) == 0:
                raise QgsProcessingException(self.tr('Nessun pixel valido nella heightmap/DEM.'))

            feedback.setProgressText(self.tr('Stima RANSAC della normale dominante...'))
            dominant = self._dominant_normal_ransac(
                np.vstack(samples), ransac_iterations, ransac_angle, rng, feedback)
            feedback.pushInfo(self.tr('Normale dominante stimata: [{:.6f}, {:.6f}, {:.6f}]').format(
                dominant[0], dominant[1], dominant[2]))
            feedback.pushInfo(self.tr('Inclinazione rispetto a (0,0,1): {:.3f} gradi').format(
                math.degrees(math.acos(max(-1.0, min(1.0, dominant[2]))))))
            rotation = self._rotation_matrix_from_vectors(dominant, np.array([0.0, 0.0, 1.0]))

        # --- Preparazione output ---
        output_path = self._ensure_tif_extension(output_path, feedback)
        write_float = output_format in (self.FORMAT_FLOAT32, self.FORMAT_BOTH)
        write_rgb = output_format in (self.FORMAT_RGB_BYTE, self.FORMAT_BOTH)

        float_ds = None
        rgb_ds = None
        rgb_path = None
        if write_float:
            float_ds = self._create_float_dataset(output_path, width, height, gt, projection, rotation is not None)
        if write_rgb:
            rgb_path = output_path if output_format == self.FORMAT_RGB_BYTE else self._rgb_sibling_path(output_path)
            rgb_ds = self._create_rgb_dataset(rgb_path, width, height, gt, projection, rgb_directx, rotation is not None)

        # --- Passaggio 2: calcolo, rotazione e scrittura a blocchi ---
        feedback.setProgressText(self.tr('Calcolo e scrittura normal map...'))
        total_valid = 0
        for i, (r0, r1) in enumerate(blocks):
            if feedback.isCanceled():
                break
            nx, ny, nz, valid = normals_for_block(r0, r1)
            if rotation is not None:
                nx, ny, nz, valid = self._apply_rotation(nx, ny, nz, valid, rotation)
            total_valid += int(np.count_nonzero(valid))

            if float_ds is not None:
                for b, arr in enumerate((nx, ny, nz)):
                    float_ds.GetRasterBand(b + 1).WriteArray(arr.astype(np.float32), 0, r0)
            if rgb_ds is not None:
                rgb = self._encode_rgb_byte(nx, ny, nz, valid, rgb_directx)
                for b in range(3):
                    rgb_ds.GetRasterBand(b + 1).WriteArray(rgb[b], 0, r0)

            feedback.setProgress(progress_start + (100.0 - progress_start) * (i + 1) / len(blocks))

        for ds in (float_ds, rgb_ds):
            if ds is not None:
                ds.FlushCache()
        float_ds = None
        rgb_ds = None
        src_ds = None

        if total_valid == 0 and not feedback.isCanceled():
            raise QgsProcessingException(self.tr('Nessun pixel valido nella heightmap/DEM.'))

        outputs = {}
        if write_float:
            outputs[self.OUTPUT] = output_path
            feedback.pushInfo(self.tr('Output metrico float32 salvato: {}').format(output_path))
        if write_rgb:
            if output_format == self.FORMAT_RGB_BYTE:
                outputs[self.OUTPUT] = rgb_path
            else:
                outputs[self.OUTPUT_RGB] = rgb_path
                context.addLayerToLoadOnCompletion(
                    rgb_path,
                    QgsProcessingContext.LayerDetails(
                        os.path.splitext(os.path.basename(rgb_path))[0],
                        context.project(),
                        self.OUTPUT_RGB
                    )
                )
            feedback.pushInfo(self.tr('Output RGB Byte salvato: {}').format(rgb_path))

        feedback.pushInfo(self.tr('Normal map completata ({} pixel validi).').format(total_valid))
        return outputs

    # ------------------------------------------------------------------
    # Input / output
    # ------------------------------------------------------------------
    def _open_with_gdal(self, raster_layer):
        if raster_layer.providerType() != 'gdal':
            raise QgsProcessingException(self.tr(
                'Il layer usa il provider "{}": sono supportati solo raster letti tramite GDAL '
                '(GeoTIFF, GPKG, VRT...). Esporta prima il layer su file.'
            ).format(raster_layer.providerType()))

        source = raster_layer.source()
        candidates = [source]
        if '|' in source:
            candidates.append(source.split('|')[0])
        for candidate in candidates:
            ds = gdal.Open(candidate, gdal.GA_ReadOnly)
            if ds is not None:
                return ds
        raise QgsProcessingException(
            self.tr('Impossibile aprire il raster sorgente con GDAL: {}').format(source))

    def _ensure_tif_extension(self, output_path, feedback):
        root, ext = os.path.splitext(output_path)
        if ext.lower() in ('.tif', '.tiff'):
            return output_path
        new_path = root + '.tif'
        feedback.pushWarning(self.tr(
            'Formato "{}" non supportato: l\'output viene scritto come GeoTIFF in {}').format(ext, new_path))
        return new_path

    def _rgb_sibling_path(self, output_path):
        root, _ = os.path.splitext(output_path)
        return root + '_rgb.tif'

    def _new_gtiff(self, path, width, height, data_type, options, gt, projection):
        driver = gdal.GetDriverByName('GTiff')
        if driver is None:
            raise QgsProcessingException(self.tr('Driver GeoTIFF non disponibile.'))
        if os.path.exists(path):
            driver.Delete(path)
        ds = driver.Create(path, width, height, 3, data_type, options=options)
        if ds is None:
            raise QgsProcessingException(self.tr('Impossibile creare il raster output: {}').format(path))
        ds.SetGeoTransform(gt)
        if projection:
            ds.SetProjection(projection)
        return ds

    def _create_float_dataset(self, path, width, height, gt, projection, rotated):
        ds = self._new_gtiff(path, width, height, gdal.GDT_Float32,
                             ['COMPRESS=LZW', 'PREDICTOR=3', 'TILED=YES', 'BIGTIFF=IF_SAFER', 'INTERLEAVE=BAND'],
                             gt, projection)
        descriptions = [
            'n.u - componente lungo +X georeferenziato, valore reale -1/+1',
            'n.v - componente lungo +Y georeferenziato, valore reale -1/+1',
            'n.w - componente frontale/normale al piano heightmap, valore reale -1/+1',
        ]
        for i in range(3):
            band = ds.GetRasterBand(i + 1)
            band.SetDescription(descriptions[i])
            band.SetNoDataValue(float('nan'))
        ds.SetMetadataItem('NORMAL_MAP_TYPE', 'metric_float32_direction_cosines')
        ds.SetMetadataItem('NORMAL_COMPONENT_RANGE', '-1,+1')
        ds.SetMetadataItem('BAND_1', 'n.u - asse +X georeferenziato')
        ds.SetMetadataItem('BAND_2', 'n.v - asse +Y georeferenziato')
        ds.SetMetadataItem('BAND_3', 'n.w - asse frontale/height')
        ds.SetMetadataItem('DOMINANT_NORMAL_ALIGNED', 'YES' if rotated else 'NO')
        ds.SetMetadataItem('METASHAPE_SEMANTIC_EQUIVALENCE', 'band1=n.u, band2=n.v, band3=n.w')
        return ds

    def _create_rgb_dataset(self, path, width, height, gt, projection, directx, rotated):
        ds = self._new_gtiff(path, width, height, gdal.GDT_Byte,
                             ['COMPRESS=LZW', 'TILED=YES', 'BIGTIFF=IF_SAFER', 'INTERLEAVE=PIXEL'],
                             gt, projection)
        color_interps = [gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand]
        g_label = '-n.v (DirectX)' if directx else 'n.v (OpenGL)'
        descriptions = [
            'R = n.u codificato 1-255 (0 = NoData)',
            'G = {} codificato 1-255 (0 = NoData)'.format(g_label),
            'B = n.w codificato 1-255 (0 = NoData)',
        ]
        for i in range(3):
            band = ds.GetRasterBand(i + 1)
            band.SetColorInterpretation(color_interps[i])
            band.SetDescription(descriptions[i])
            band.SetNoDataValue(0)
        ds.SetMetadataItem('NORMAL_MAP_TYPE', 'visual_rgb_byte')
        ds.SetMetadataItem('NORMAL_COMPONENT_ENCODING', 'byte = round((component + 1) * 127) + 1 ; 0 = NoData')
        ds.SetMetadataItem('NORMAL_COMPONENT_DECODING', 'component = (byte - 1) / 127 - 1')
        ds.SetMetadataItem('GREEN_CONVENTION', 'DirectX (G = -Y)' if directx else 'OpenGL (G = +Y)')
        ds.SetMetadataItem('DOMINANT_NORMAL_ALIGNED', 'YES' if rotated else 'NO')
        ds.SetMetadataItem('METASHAPE_SEMANTIC_EQUIVALENCE', 'R/band1=n.u, G/band2=n.v, B/band3=n.w')
        return ds

    # ------------------------------------------------------------------
    # Calcolo delle normali
    # ------------------------------------------------------------------
    def _read_padded_block(self, src_band, mask_band, width, height, r0, r1):
        """Legge le righe r0..r1 con 1 px di bordo; restituisce quote e validita' (rows+2, width+2)."""
        rows = r1 - r0
        read_r0 = max(0, r0 - 1)
        read_r1 = min(height, r1 + 1)
        n_read = read_r1 - read_r0

        data = src_band.ReadAsArray(0, read_r0, width, n_read)
        mask = mask_band.ReadAsArray(0, read_r0, width, n_read)
        if data is None or mask is None:
            raise QgsProcessingException(self.tr('Errore di lettura del raster (righe {}-{}).').format(r0, r1))

        z = np.zeros((rows + 2, width + 2), dtype=np.float64)
        valid = np.zeros((rows + 2, width + 2), dtype=bool)
        offset = read_r0 - (r0 - 1)
        z[offset:offset + n_read, 1:-1] = data
        valid[offset:offset + n_read, 1:-1] = (mask > 0)
        valid &= np.isfinite(z)
        z[~valid] = 0.0
        return z, valid

    def _pixel_gradients(self, z, valid, method):
        """
        Derivate per pixel dz/dcolonna e dz/driga sul nucleo del blocco paddato.
        Priorita': Horn 3x3 -> differenze centrali -> differenze in avanti/indietro.
        """
        rows = z.shape[0] - 2
        cols = z.shape[1] - 2

        def s(dr, dc):
            return z[1 + dr:1 + dr + rows, 1 + dc:1 + dc + cols]

        def v(dr, dc):
            return valid[1 + dr:1 + dr + rows, 1 + dc:1 + dc + cols]

        center = v(0, 0)
        dzdc = np.full((rows, cols), np.nan, dtype=np.float64)
        dzdr = np.full((rows, cols), np.nan, dtype=np.float64)

        # Riempimento dal metodo meno preciso al piu' preciso: l'ultimo sovrascrive.
        # Lungo le colonne (X pixel)
        m = center & v(0, -1)
        dzdc[m] = (s(0, 0) - s(0, -1))[m]
        m = center & v(0, 1)
        dzdc[m] = (s(0, 1) - s(0, 0))[m]
        m = center & v(0, -1) & v(0, 1)
        dzdc[m] = ((s(0, 1) - s(0, -1)) / 2.0)[m]

        # Lungo le righe (Y pixel, verso il basso)
        m = center & v(-1, 0)
        dzdr[m] = (s(0, 0) - s(-1, 0))[m]
        m = center & v(1, 0)
        dzdr[m] = (s(1, 0) - s(0, 0))[m]
        m = center & v(-1, 0) & v(1, 0)
        dzdr[m] = ((s(1, 0) - s(-1, 0)) / 2.0)[m]

        if method == self.GRADIENT_HORN:
            m = center & v(-1, -1) & v(0, -1) & v(1, -1) & v(-1, 1) & v(0, 1) & v(1, 1)
            horn_c = ((s(-1, 1) + 2.0 * s(0, 1) + s(1, 1)) - (s(-1, -1) + 2.0 * s(0, -1) + s(1, -1))) / 8.0
            dzdc[m] = horn_c[m]

            m = center & v(-1, -1) & v(-1, 0) & v(-1, 1) & v(1, -1) & v(1, 0) & v(1, 1)
            horn_r = ((s(1, -1) + 2.0 * s(1, 0) + s(1, 1)) - (s(-1, -1) + 2.0 * s(-1, 0) + s(-1, 1))) / 8.0
            dzdr[m] = horn_r[m]

        return dzdc, dzdr

    def _compute_block_normals(self, src_band, mask_band, width, height, r0, r1,
                               height_scale, method, inv_jac_t):
        z, valid = self._read_padded_block(src_band, mask_band, width, height, r0, r1)
        z *= height_scale

        dzdc, dzdr = self._pixel_gradients(z, valid, method)

        # Derivate georeferenziate: [dz/dX, dz/dY] = inv(J)^T [dz/dc, dz/dr]
        dzdx = inv_jac_t[0, 0] * dzdc + inv_jac_t[0, 1] * dzdr
        dzdy = inv_jac_t[1, 0] * dzdc + inv_jac_t[1, 1] * dzdr

        nx = -dzdx
        ny = -dzdy
        norm = np.sqrt(nx * nx + ny * ny + 1.0)
        ok = np.isfinite(norm)

        out_x = np.full(nx.shape, np.nan, dtype=np.float64)
        out_y = np.full(nx.shape, np.nan, dtype=np.float64)
        out_z = np.full(nx.shape, np.nan, dtype=np.float64)
        out_x[ok] = nx[ok] / norm[ok]
        out_y[ok] = ny[ok] / norm[ok]
        out_z[ok] = 1.0 / norm[ok]
        return out_x, out_y, out_z, ok

    # ------------------------------------------------------------------
    # RANSAC e rotazione
    # ------------------------------------------------------------------
    def _dominant_normal_ransac(self, normals, iterations, angle_deg, rng, feedback=None):
        """Stima robusta della normale dominante con RANSAC su un campione (N,3) di normali."""
        normals = np.asarray(normals, dtype=np.float64)
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
        for _ in range(iterations):
            if feedback is not None and feedback.isCanceled():
                break
            candidate = normals[rng.integers(0, n)]
            if candidate[2] < 0:
                candidate = -candidate
            inliers = (normals @ candidate) >= cos_threshold
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
            return np.array([0.0, 0.0, 1.0], dtype=np.float64)
        dominant = dominant / dominant_norm
        if dominant[2] < 0:
            dominant = -dominant
        return dominant

    def _rotation_matrix_from_vectors(self, source, target):
        """Matrice di rotazione (Rodrigues) che porta source su target."""
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
            return np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]], dtype=np.float64)

        k = np.array([
            [0.0, -v[2], v[1]],
            [v[2], 0.0, -v[0]],
            [-v[1], v[0], 0.0],
        ], dtype=np.float64)
        return np.eye(3, dtype=np.float64) + k + (k @ k) * ((1.0 - c) / (s * s))

    def _apply_rotation(self, nx, ny, nz, valid, rotation):
        """Applica la rotazione e rinormalizza; i pixel non validi restano NaN."""
        rx = rotation[0, 0] * nx + rotation[0, 1] * ny + rotation[0, 2] * nz
        ry = rotation[1, 0] * nx + rotation[1, 1] * ny + rotation[1, 2] * nz
        rz = rotation[2, 0] * nx + rotation[2, 1] * ny + rotation[2, 2] * nz
        length = np.sqrt(rx * rx + ry * ry + rz * rz)
        good = valid & np.isfinite(length) & (length > 0)

        out = []
        for comp in (rx, ry, rz):
            arr = np.full(comp.shape, np.nan, dtype=np.float64)
            arr[good] = comp[good] / length[good]
            out.append(arr)
        return out[0], out[1], out[2], good

    # ------------------------------------------------------------------
    # Codifica RGB
    # ------------------------------------------------------------------
    def _encode_rgb_byte(self, nx, ny, nz, valid, directx=False):
        """Codifica -1/+1 in 1-255 (0 riservato al NoData)."""
        rows, cols = nx.shape
        rgb = np.zeros((3, rows, cols), dtype=np.uint8)
        g = -ny if directx else ny
        for i, comp in enumerate((nx, g, nz)):
            vals = np.clip(comp[valid], -1.0, 1.0)
            rgb[i, valid] = (np.rint((vals + 1.0) * 127.0) + 1.0).astype(np.uint8)
        return rgb
