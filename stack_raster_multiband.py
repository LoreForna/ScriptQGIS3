# -*- coding: utf-8 -*-
# =============================================================================
# Stack raster per analisi metrologica - QGIS Processing Script
# Ortofoto RGB + DEM (+ normal map opzionale) -> GeoTIFF multibanda
# Testato per QGIS 3.x con GDAL 3.13
#
# INSTALLAZIONE:
#   Processing Toolbox -> icona Script (Python) -> "Add Script to Toolbox..."
#   oppure menu in alto del Toolbox -> Scripts -> Open Existing Script.
#   Comparira' sotto "Scripts > Archeologia / Raster".
#
# LOGICA RICAMPIONAMENTI:
#   - DEM   -> Warp BILINEAR (dato continuo). Mai Nearest, mai Cubic/Lanczos.
#   - VRT   -> con -separate NON ricampiona: la conformita' griglia la
#              garantisce la fase di warp (stessa res/extent + -tap).
#   - normal map -> NON warpata: va rigenerata dal DEM allineato. Qui si
#              assume gia' conforme alla griglia master se attivata.
# =============================================================================

import os

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterEnum,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterDefinition,
    QgsRasterLayer,
)
import processing


class StackRasterMultiband(QgsProcessingAlgorithm):
    """
    Conforma il DEM alla griglia dell'ortofoto (warp bilinear + -tap),
    impila ortofoto + DEM (+ normal map) in un VRT -separate e materializza
    il GeoTIFF multibanda. Produce anche una variante a 3 bande (R, DEM, B).
    """

    ORTHO = "ORTHO"
    DEM = "DEM"
    NORMAL = "NORMAL"
    USE_NORMAL = "USE_NORMAL"
    DEM_RESAMPLING = "DEM_RESAMPLING"
    MAKE_3BANDS = "MAKE_3BANDS"
    OUT_DIR = "OUT_DIR"

    # enum allineato a gdal:warpreproject
    RESAMPLING_OPTIONS = [
        "Nearest Neighbour",        # 0
        "Bilinear (2x2)",           # 1  <- default consigliato per DEM
        "Cubic (4x4)",              # 2
        "Cubic B-Spline (4x4)",     # 3
        "Lanczos (6x6)",            # 4
        "Average",                  # 5
    ]

    def tr(self, string):
        return QCoreApplication.translate("StackRasterMultiband", string)

    def createInstance(self):
        return StackRasterMultiband()

    def name(self):
        return "stack_raster_multiband"

    def displayName(self):
        return self.tr("Stack raster multiband")

    def group(self):
        return self.tr("Raster")

    def groupId(self):
        return "raster"

    def shortHelpString(self):
        return self.tr(
            "Impila ortofoto RGB, DEM e (opzionale) normal map in un GeoTIFF "
            "multibanda per analisi metrologica.\n\n"
            "Il DEM viene conformato alla griglia dell'ortofoto (master) con "
            "warp Bilinear e -tap (allineamento esatto dei pixel). Lo stack "
            "usa gdalbuildvrt -separate (nessun ricampionamento). Con GDAL >= 3.8 "
            "tutte le bande di ogni input diventano bande separate: "
            "ortofoto(3) + DEM(1) = 4 bande; +normal(3) = 7 bande.\n\n"
            "ATTENZIONE: la normal map NON va warpata (campo di versori). "
            "Attiva 'Includi normal map' solo se e' GIA' sulla griglia "
            "dell'ortofoto (rigenerata dal DEM allineato).\n\n"
            "Output: stack_4b.tif (o stack_7b.tif) e, opzionale, stack_R_DEM_B.tif."
        )

    # -------------------------------------------------------------------------
    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.ORTHO,
                self.tr("Ortofoto RGB (raster MASTER, 3 bande)"),
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.DEM,
                self.tr("DEM monobanda (quote)"),
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.USE_NORMAL,
                self.tr("Includi normal map (deve essere gia' conforme alla griglia ortofoto)"),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.NORMAL,
                self.tr("Normal map (3 bande NX,NY,NZ) - opzionale"),
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.DEM_RESAMPLING,
                self.tr("Metodo di ricampionamento del DEM (warp)"),
                options=self.RESAMPLING_OPTIONS,
                defaultValue=1,  # Bilinear
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.MAKE_3BANDS,
                self.tr("Genera anche variante 3 bande R, DEM, B"),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUT_DIR,
                self.tr("Cartella di output"),
            )
        )

    # -------------------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):
        ortho_lyr = self.parameterAsRasterLayer(parameters, self.ORTHO, context)
        dem_lyr_in = self.parameterAsRasterLayer(parameters, self.DEM, context)
        use_normal = self.parameterAsBool(parameters, self.USE_NORMAL, context)
        normal_lyr = self.parameterAsRasterLayer(parameters, self.NORMAL, context)
        dem_resampling = self.parameterAsEnum(parameters, self.DEM_RESAMPLING, context)
        make_3b = self.parameterAsBool(parameters, self.MAKE_3BANDS, context)
        out_dir = self.parameterAsString(parameters, self.OUT_DIR, context)

        if ortho_lyr is None or not ortho_lyr.isValid():
            raise QgsProcessingException(self.tr("Ortofoto non valida."))
        if dem_lyr_in is None or not dem_lyr_in.isValid():
            raise QgsProcessingException(self.tr("DEM non valido."))
        if use_normal and (normal_lyr is None or not normal_lyr.isValid()):
            raise QgsProcessingException(
                self.tr("Normal map richiesta ma non fornita/valida.")
            )

        os.makedirs(out_dir, exist_ok=True)
        p = lambda name: os.path.join(out_dir, name)

        ortho_path = ortho_lyr.source()
        dem_path = dem_lyr_in.source()

        # --- griglia MASTER dall'ortofoto -----------------------------------
        master_crs = ortho_lyr.crs().authid()
        px = ortho_lyr.rasterUnitsPerPixelX()
        py = ortho_lyr.rasterUnitsPerPixelY()
        res = min(px, py)
        ext = ortho_lyr.extent()
        te = f"{ext.xMinimum()} {ext.yMinimum()} {ext.xMaximum()} {ext.yMaximum()}"
        feedback.pushInfo(f"MASTER  CRS={master_crs}  res={res}")
        feedback.pushInfo(f"MASTER  extent=[{te}]")

        # --- FASE 0: warp DEM sulla griglia master --------------------------
        feedback.pushInfo("[0] Warp DEM sulla griglia dell'ortofoto...")
        dem_aligned = p("dem_aligned.tif")
        processing.run(
            "gdal:warpreproject",
            {
                "INPUT": dem_path,
                "SOURCE_CRS": None,
                "TARGET_CRS": master_crs,
                "RESAMPLING": dem_resampling,      # 1 = Bilinear (default)
                "NODATA": None,
                "TARGET_RESOLUTION": res,
                "TARGET_EXTENT": te,
                "TARGET_EXTENT_CRS": master_crs,
                "DATA_TYPE": 6,                    # Float32
                "MULTITHREADING": True,
                "EXTRA": "-tap -co COMPRESS=LZW -co TILED=YES",
                "OUTPUT": dem_aligned,
            },
            context=context,
            feedback=feedback,
            is_child_algorithm=True,
        )

        dem_chk = QgsRasterLayer(dem_aligned, "dem_aligned")
        if dem_chk.width() != ortho_lyr.width() or dem_chk.height() != ortho_lyr.height():
            raise QgsProcessingException(
                self.tr(
                    "Griglia DEM allineato non coincide con l'ortofoto "
                    f"(DEM {dem_chk.width()}x{dem_chk.height()} vs "
                    f"ortho {ortho_lyr.width()}x{ortho_lyr.height()})."
                )
            )
        feedback.pushInfo("    griglia DEM conforme OK")

        # --- normal map (gia' conforme) -------------------------------------
        inputs = [ortho_path, dem_aligned]
        if use_normal:
            n_path = normal_lyr.source()
            if normal_lyr.width() != ortho_lyr.width() or normal_lyr.height() != ortho_lyr.height():
                raise QgsProcessingException(
                    self.tr(
                        "La normal map NON e' conforme alla griglia dell'ortofoto. "
                        "Rigenerala dal DEM allineato prima di impilarla."
                    )
                )
            inputs.append(n_path)

        n_expected = 7 if use_normal else 4

        # --- FASE 1: VRT -separate ------------------------------------------
        feedback.pushInfo("[1] Build virtual raster (-separate)...")
        vrt = p("stack.vrt")
        processing.run(
            "gdal:buildvirtualraster",
            {
                "INPUT": inputs,
                "RESOLUTION": 1,      # Highest (inerte con SEPARATE)
                "SEPARATE": True,
                "PROJ_DIFFERENCE": False,
                "ADD_ALPHA": False,
                "ASSIGN_CRS": None,
                "RESAMPLING": 0,      # inerte sotto -separate
                "SRC_NODATA": "",
                "EXTRA": "",
                "OUTPUT": vrt,
            },
            context=context,
            feedback=feedback,
            is_child_algorithm=True,
        )

        vrt_lyr = QgsRasterLayer(vrt, "stack_vrt")
        feedback.pushInfo(f"    VRT bande = {vrt_lyr.bandCount()} (attese {n_expected})")
        if vrt_lyr.bandCount() != n_expected:
            raise QgsProcessingException(
                self.tr(
                    f"Numero bande inatteso ({vrt_lyr.bandCount()} invece di "
                    f"{n_expected}). Verifica la conformita' degli input."
                )
            )

        # --- FASE 2a: GeoTIFF completo --------------------------------------
        feedback.pushInfo("[2a] Translate -> GeoTIFF completo...")
        out_full = p(f"stack_{n_expected}b.tif")
        processing.run(
            "gdal:translate",
            {
                "INPUT": vrt,
                "TARGET_CRS": None,
                "NODATA": None,
                "COPY_SUBDATASETS": False,
                "CREATION_OPTIONS": "COMPRESS=LZW|TILED=YES",
                "DATA_TYPE": 6,      # Float32
                "EXTRA": "",
                "OUTPUT": out_full,
            },
            context=context,
            feedback=feedback,
            is_child_algorithm=True,
        )
        feedback.pushInfo(f"    -> {out_full}")

        results = {"OUTPUT_FULL": out_full, "DEM_ALIGNED": dem_aligned, "VRT": vrt}

        # --- FASE 2b: variante 3 bande (R, DEM, B) --------------------------
        if make_3b:
            feedback.pushInfo("[2b] Rearrange bands -> R, DEM, B...")
            out_3b = p("stack_R_DEM_B.tif")
            processing.run(
                "gdal:rearrange_bands",
                {
                    "INPUT": out_full,
                    "BANDS": [1, 4, 3],   # R, DEM, B
                    "CREATION_OPTIONS": "COMPRESS=LZW|TILED=YES",
                    "DATA_TYPE": 6,       # Float32
                    "OUTPUT": out_3b,
                },
                context=context,
                feedback=feedback,
                is_child_algorithm=True,
            )
            feedback.pushInfo(f"    -> {out_3b}")
            results["OUTPUT_3B"] = out_3b

        bande = "1=R 2=G 3=B 4=DEM" + (" 5=NX 6=NY 7=NZ" if use_normal else "")
        feedback.pushInfo(f"Fatto. Bande stack completo: {bande}")
        results[self.OUT_DIR] = out_dir
        return results
