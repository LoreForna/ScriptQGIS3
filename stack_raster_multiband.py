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
#   - DEM   -> Warp alla griglia dell'ortofoto. Default NEAREST: quando DEM e
#              ortofoto hanno la STESSA risoluzione il warp corregge solo lo
#              sfasamento sub-pixel (-tap) e Nearest non altera le quote ne'
#              crea frange di bordo. Bilinear selezionabile per DEM a
#              risoluzione diversa (dato continuo da ricampionare davvero).
#   - VRT   -> con -separate NON ricampiona: la conformita' griglia la
#              garantisce la fase di warp (stessa res/extent + -tap).
#   - normal map -> campo di VERSORI. Idealmente gia' pixel-perfect sulla
#              griglia dell'ortofoto (Build_normal_map modalita' B). Se non lo
#              e' e si attiva il riallineamento, viene warpata con NEAREST (mai
#              bilinear: mediare i versori ne rompe la norma e ne ruota le
#              direzioni); resta un'approssimazione, meglio rigenerarla a monte.
#
# INPUT OPZIONALI:
#   L'ortofoto e' sempre il raster MASTER obbligatorio. DEM e normal map sono
#   entrambi opzionali e indipendenti: lo stack funziona con ortofoto+DEM,
#   ortofoto+normal, ortofoto+DEM+normal o anche sola ortofoto. L'ordine delle
#   bande nello stack e' sempre ortofoto(3) -> DEM(1, se presente) ->
#   normal(3, se presente); la numerazione per il raster custom viene calcolata
#   dinamicamente in base a cosa e' effettivamente presente.
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
    QgsRectangle,
    QgsCoordinateReferenceSystem,
)
import processing


class StackRasterMultiband(QgsProcessingAlgorithm):
    """
    Conforma il DEM alla griglia dell'ortofoto (warp bilinear + -tap),
    impila ortofoto + DEM (+ normal map) in un VRT -separate e materializza
    il GeoTIFF multibanda. Produce anche una variante a 3 bande con bande
    scelte liberamente dall'utente tra quelle disponibili nello stack.
    """

    ORTHO = "ORTHO"
    DEM = "DEM"
    NORMAL = "NORMAL"
    DEM_RESAMPLING = "DEM_RESAMPLING"
    DEM_IGNORE_NODATA = "DEM_IGNORE_NODATA"
    NORMAL_REALIGN = "NORMAL_REALIGN"
    MAKE_3BANDS = "MAKE_CUSTOM"
    CUSTOM_BANDS = "CUSTOM_BANDS"
    OUT_DIR = "OUT_DIR"

    # Etichette del menu custom. Elencano TUTTE le bande possibili perche' il
    # menu enum viene costruito in initAlgorithm, prima di sapere quali input
    # verranno forniti. La corrispondenza etichetta -> banda reale nello stack
    # e' dinamica (vedi _band_layout in processAlgorithm): se manca il DEM o la
    # normal map, le relative voci vengono scartate a runtime con un messaggio.
    # Ogni voce e' una chiave logica, non un numero di banda fisso.
    #   ortofoto SEMPRE presente (R,G,B); DEM e normal opzionali.
    CUSTOM_CHOICES = [
        ("R", "R (ortofoto)"),
        ("G", "G (ortofoto)"),
        ("B", "B (ortofoto)"),
        ("DEM", "DEM (quota) - richiede DEM"),
        ("NX", "NX (normale) - richiede normal map"),
        ("NY", "NY (normale) - richiede normal map"),
        ("NZ", "NZ (normale) - richiede normal map"),
    ]
    # Etichette mostrate nel menu (solo la parte descrittiva).
    SRC_BAND_LABELS = [label for (_key, label) in CUSTOM_CHOICES]

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

    # -------------------------------------------------------------------------
    def _grid_mismatch(self, lyr, ortho_lyr):
        """Verifica la co-registrazione di `lyr` rispetto all'ortofoto master.
        Confronta NON solo le dimensioni in pixel ma anche origine e pixel size:
        due raster possono avere lo stesso numero di righe/colonne ed essere
        comunque sfalsati (origine diversa) o a scala diversa. Ritorna una
        stringa che descrive lo scarto, oppure None se la griglia e' conforme.

        Tolleranza: mezzo pixel sull'origine, 1e-6 relativo sul pixel size. Il
        warp con -tap garantisce l'allineamento entro queste soglie.
        """
        if lyr.width() != ortho_lyr.width() or lyr.height() != ortho_lyr.height():
            return (f"dimensioni {lyr.width()}x{lyr.height()} vs "
                    f"{ortho_lyr.width()}x{ortho_lyr.height()}")

        eo = ortho_lyr.extent()
        el = lyr.extent()
        # pixel size (unita' mappa) dell'ortofoto come riferimento di tolleranza
        pxo = ortho_lyr.rasterUnitsPerPixelX()
        pyo = ortho_lyr.rasterUnitsPerPixelY()
        tol_x = 0.5 * pxo
        tol_y = 0.5 * pyo

        dxmin = abs(el.xMinimum() - eo.xMinimum())
        dymax = abs(el.yMaximum() - eo.yMaximum())
        if dxmin > tol_x or dymax > tol_y:
            return (f"origine sfalsata (dX={dxmin:.5f}, dY={dymax:.5f} unita' "
                    f"mappa; tolleranza {tol_x:.5f}/{tol_y:.5f})")

        pxl = lyr.rasterUnitsPerPixelX()
        pyl = lyr.rasterUnitsPerPixelY()
        if (abs(pxl - pxo) > 1e-6 * pxo) or (abs(pyl - pyo) > 1e-6 * pyo):
            return (f"pixel size diverso ({pxl:.8f}x{pyl:.8f} vs "
                    f"{pxo:.8f}x{pyo:.8f})")

        return None

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
            "Impila ortofoto RGB e, opzionalmente, DEM e/o normal map in un "
            "GeoTIFF multibanda per analisi metrologica.\n\n"
            "L'ortofoto e' sempre il raster MASTER. DEM e normal map sono "
            "entrambi opzionali e indipendenti: funziona con ortofoto+DEM, "
            "ortofoto+normal, ortofoto+DEM+normal o sola ortofoto.\n\n"
            "Il DEM (se fornito) viene conformato alla griglia dell'ortofoto "
            "con warp e -tap (allineamento esatto dei pixel). Default Nearest "
            "(stessa risoluzione: solo riallineamento, quote non alterate); "
            "Bilinear per DEM a risoluzione diversa. Lo stack usa gdalbuildvrt "
            "-separate (nessun ricampionamento).\n\n"
            "Ordine bande nello stack: ortofoto(3) -> DEM(1, se presente) -> "
            "normal(3, se presente). Esempi: sola ortofoto = 3 bande; "
            "+DEM = 4; +normal = 6; +DEM+normal = 7.\n\n"
            "NORMAL MAP: e' un campo di versori. Idealmente gia' pixel-perfect "
            "sulla griglia dell'ortofoto (Build_normal_map modalita' B). Se non "
            "lo e', attiva 'Riallinea la normal map': verra' warpata con Nearest "
            "(mai bilinear sui versori), con una piccola imprecisione residua.\n\n"
            "Raster custom: seleziona quali bande includere. Le voci DEM e "
            "NX/NY/NZ sono valide solo se il relativo input e' presente; "
            "altrimenti vengono segnalate con errore.\n\n"
            "Output: stack_Nb.tif e, opzionale, stack_custom_Nb.tif."
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
                self.tr("DEM monobanda (quote) - opzionale"),
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.NORMAL,
                self.tr("Normal map (3 bande NX,NY,NZ) - opzionale"),
                optional=True,
            )
        )
        # Default NEAREST: quando DEM e ortofoto condividono la risoluzione (caso
        # tipico di export dallo stesso progetto Metashape), il warp serve solo a
        # correggere lo sfasamento sub-pixel della griglia (-tap). Nearest non
        # media le quote e non crea la "frangia" di quote fittizie al bordo del
        # DEM. Usare Bilinear SOLO se il DEM e' a risoluzione diversa e va
        # davvero ricampionato (dato continuo); in quel caso serve pero' un
        # NoData reale fuori range per fermare l'interpolazione sul bordo.
        self.addParameter(
            QgsProcessingParameterEnum(
                self.DEM_RESAMPLING,
                self.tr("Metodo di ricampionamento del DEM (warp)"),
                options=self.RESAMPLING_OPTIONS,
                defaultValue=0,  # Nearest (stessa risoluzione: solo riallineamento)
            )
        )
        # Metashape esporta spesso il DEM con NoData = 0.0. Se lo 0.0 e' una
        # quota reale (il piano locale passa per lo zero), trattarlo come NoData
        # crea buchi fasulli nel warp. Attivo di default: ignora il NoData del
        # DEM in modo che lo 0.0 resti quota valida. Disattivare solo se il DEM
        # usa davvero il suo NoData per marcare aree vuote reali.
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.DEM_IGNORE_NODATA,
                self.tr("Ignora il NoData del DEM (0.0 = quota valida, tipico Metashape)"),
                defaultValue=True,
            )
        )
        # La normal map DOVREBBE essere gia' pixel-perfect sulla griglia
        # dell'ortofoto (rigenerata in modalita' B). Se non lo e', con questa
        # opzione lo stack la riallinea con warp alla griglia master. ATTENZIONE:
        # la normal map e' un campo di versori; il riallineamento la ricampiona
        # (nearest, per non mediare le direzioni) e introduce una piccola
        # imprecisione. Preferibile rigenerarla correttamente a monte.
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.NORMAL_REALIGN,
                self.tr("Riallinea la normal map se non conforme (ricampiona i versori)"),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.MAKE_3BANDS,
                self.tr("Genera anche un raster con bande selezionate"),
                defaultValue=True,
            )
        )
        # Menu a selezione multipla: spunta le bande da includere nel raster
        # custom. L'ordine di output segue l'ordine logico (R,G,B,DEM,NX,NY,NZ),
        # NON l'ordine in cui le spunti. Le voci DEM/NX/NY/NZ sono valide solo
        # se il relativo input e' presente (validato a runtime). Nessuna banda
        # preselezionata di default.
        self.addParameter(
            QgsProcessingParameterEnum(
                self.CUSTOM_BANDS,
                self.tr("Bande da includere nel raster custom"),
                options=self.SRC_BAND_LABELS,
                allowMultiple=True,
                defaultValue=[],  # nessuna banda preselezionata
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
        normal_lyr = self.parameterAsRasterLayer(parameters, self.NORMAL, context)
        dem_resampling = self.parameterAsEnum(parameters, self.DEM_RESAMPLING, context)
        dem_ignore_nodata = self.parameterAsBool(parameters, self.DEM_IGNORE_NODATA, context)
        normal_realign = self.parameterAsBool(parameters, self.NORMAL_REALIGN, context)
        make_custom = self.parameterAsBool(parameters, self.MAKE_3BANDS, context)
        custom_idx = self.parameterAsEnums(parameters, self.CUSTOM_BANDS, context)
        out_dir = self.parameterAsString(parameters, self.OUT_DIR, context)

        # L'ortofoto e' l'unico input obbligatorio (raster MASTER).
        if ortho_lyr is None or not ortho_lyr.isValid():
            raise QgsProcessingException(self.tr("Ortofoto non valida."))

        # DEM e normal map sono opzionali e indipendenti. Un layer indicato ma
        # non valido e' un errore (l'utente lo ha scelto per sbaglio/rotto);
        # un layer non indicato e' semplicemente assente.
        use_dem = dem_lyr_in is not None
        if use_dem and not dem_lyr_in.isValid():
            raise QgsProcessingException(self.tr("DEM indicato ma non valido."))

        use_normal = normal_lyr is not None
        if use_normal and not normal_lyr.isValid():
            raise QgsProcessingException(
                self.tr("Normal map indicata ma non valida.")
            )

        os.makedirs(out_dir, exist_ok=True)
        p = lambda name: os.path.join(out_dir, name)

        ortho_path = ortho_lyr.source()
        dem_path = dem_lyr_in.source() if use_dem else None

        # --- griglia MASTER dall'ortofoto -----------------------------------
        master_crs = ortho_lyr.crs().authid()
        if not master_crs:
            raise QgsProcessingException(
                self.tr("L'ortofoto non ha un CRS valido (authid vuoto).")
            )

        # --- CRS del DEM: gestione export Metashape "Local Coordinates" -----
        # Solo se il DEM e' presente. Metashape spesso esporta il DEM con un
        # EngineeringCRS/Local CRS privo di datum geodetico (authid vuoto). DEM
        # e ortofoto escono pero' dallo stesso allineamento e sono fisicamente
        # sulla stessa griglia, quindi NON vanno riproiettati: si forza
        # SOURCE_CRS = CRS dell'ortofoto cosi' GDAL esegue solo l'allineamento
        # di griglia (resample + -tap) senza cercare una trasformazione di
        # coordinate inesistente ("Cannot find coordinate operations ...").
        dem_source_crs = None
        if use_dem:
            dem_crs = dem_lyr_in.crs()
            dem_authid = dem_crs.authid()
            # Un CRS "utilizzabile" per riproiettare deve essere valido, non
            # locale e con un authid EPSG/utente riconosciuto.
            dem_crs_usable = (
                dem_crs.isValid()
                and bool(dem_authid)
                and not dem_authid.startswith("LOCAL")
            )
            if dem_crs_usable and dem_authid != master_crs:
                # DEM realmente in un altro CRS: lascia che GDAL riproietti.
                dem_source_crs = dem_authid
                feedback.pushInfo(
                    f"DEM in CRS {dem_authid}: verra' riproiettato su {master_crs}."
                )
            else:
                # DEM senza CRS valido o locale (tipico Metashape): assume la
                # griglia master. Nessuna riproiezione, solo allineamento.
                dem_source_crs = master_crs
                feedback.pushInfo(
                    "DEM senza CRS proiettato valido (Local Coordinates?): "
                    f"assumo la griglia dell'ortofoto ({master_crs}). "
                    "Nessuna riproiezione, solo allineamento di griglia."
                )

        px = ortho_lyr.rasterUnitsPerPixelX()
        py = ortho_lyr.rasterUnitsPerPixelY()
        res = min(px, py)
        if not res or res <= 0:
            raise QgsProcessingException(
                self.tr("Risoluzione dell'ortofoto non valida (pixel size <= 0).")
            )

        ext = ortho_lyr.extent()
        if ext.isEmpty() or ext.width() <= 0 or ext.height() <= 0:
            raise QgsProcessingException(
                self.tr("Estensione dell'ortofoto non valida o vuota.")
            )

        # TARGET_EXTENT va passato come oggetto QgsRectangle, NON come stringa.
        # L'API Processing valida questo parametro come extent: una stringa
        # (in qualunque formato) viene rifiutata con
        # "Valore del parametro non corretto per TARGET_EXTENT".
        # NB: lo string-form di Processing per gli extent e' "xMin,xMax,yMin,yMax",
        # non "xmin ymin xmax ymax" -> altra causa del rifiuto.
        te_rect = QgsRectangle(
            ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()
        )
        master_crs_obj = QgsCoordinateReferenceSystem(master_crs)

        feedback.pushInfo(f"MASTER  CRS={master_crs}  res={res:.12f}")
        feedback.pushInfo(
            "MASTER  extent=[{:.10f} {:.10f} {:.10f} {:.10f}]".format(
                ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()
            )
        )

        # --- CRS coerente su disco per il VRT -------------------------------
        # gdalbuildvrt confronta il CRS SCRITTO NEI FILE, non quello che QGIS
        # mostra nel pannello layer. Se l'ortofoto e' LOCAL_CS su disco (tipico
        # Metashape) ma il DEM allineato esce con EPSG master, il VRT li giudica
        # eterogenei e SCARTA il DEM ("does not support heterogeneous
        # projection ... Skipping dem_aligned.tif"), lasciando meno bande del
        # previsto. Per evitarlo materializziamo una copia dell'ortofoto con il
        # CRS master ASSEGNATO (nessun ricampionamento, solo il tag CRS) e usiamo
        # quella nel VRT, cosi' tutti gli input hanno lo stesso CRS su disco.
        # assignprojection opererebbe in-place sul file originale; per non
        # toccarlo copiamo l'ortofoto assegnando il CRS con gdal:translate
        # (TARGET_CRS scrive -a_srs), che crea una nuova copia senza
        # ricampionare i pixel.
        feedback.pushInfo("[0a] Assegno il CRS master all'ortofoto per il VRT...")
        ortho_tagged = p("ortho_crs.tif")
        processing.run(
            "gdal:translate",
            {
                "INPUT": ortho_path,
                "TARGET_CRS": master_crs_obj,   # scrive il CRS nel file copia
                "NODATA": None,
                "COPY_SUBDATASETS": False,
                "CREATION_OPTIONS": "COMPRESS=LZW|TILED=YES",
                "DATA_TYPE": 0,                 # mantiene il dtype dell'ortofoto
                "EXTRA": "",
                "OUTPUT": ortho_tagged,
            },
            context=context,
            feedback=feedback,
            is_child_algorithm=True,
        )

        # Lo stack parte sempre dall'ortofoto (banda 1-3). DEM e normal map si
        # aggiungono in coda, nell'ordine ortofoto -> DEM -> normal.
        inputs = [ortho_tagged]

        # --- FASE 0: warp DEM sulla griglia master (solo se DEM presente) ---
        if use_dem:
            dem_source_crs_obj = QgsCoordinateReferenceSystem(dem_source_crs)
            feedback.pushInfo("[0] Warp DEM sulla griglia dell'ortofoto...")

            # Gestione NoData del DEM. Metashape spesso esporta il DEM con
            # NoData = 0.0; se lo 0.0 e' una quota reale, trattarlo come NoData
            # crea buchi fasulli. Con l'opzione attiva si usa -srcnodata none:
            # lo 0.0 resta quota valida. NB: NON si usa -dstnodata none insieme
            # a -tap, perche' GDAL prova a inizializzare la destinazione a NoData
            # e, non avendone uno, emette "ERROR 1: INIT_DEST was set to NO_DATA".
            # Lasciamo quindi che GDAL non scriva alcun NoData in output.
            extra = "-tap -co COMPRESS=LZW -co TILED=YES"
            if dem_ignore_nodata:
                extra += " -srcnodata none"
                feedback.pushInfo(
                    "    NoData del DEM ignorato: 0.0 trattato come quota valida."
                )

            dem_aligned = p("dem_aligned.tif")
            processing.run(
                "gdal:warpreproject",
                {
                    "INPUT": dem_path,
                    "SOURCE_CRS": dem_source_crs_obj,   # forzato = master se DEM locale
                    "TARGET_CRS": master_crs_obj,
                    "RESAMPLING": dem_resampling,      # 1 = Bilinear (default)
                    "NODATA": None,
                    "TARGET_RESOLUTION": res,          # float nativo (non stringa)
                    "TARGET_EXTENT": te_rect,          # QgsRectangle, non stringa
                    "TARGET_EXTENT_CRS": master_crs_obj,
                    "DATA_TYPE": 6,                    # Float32
                    "MULTITHREADING": True,
                    "EXTRA": extra,
                    "OUTPUT": dem_aligned,
                },
                context=context,
                feedback=feedback,
                is_child_algorithm=True,
            )

            dem_chk = QgsRasterLayer(dem_aligned, "dem_aligned")
            mm = self._grid_mismatch(dem_chk, ortho_lyr)
            if mm:
                raise QgsProcessingException(
                    self.tr(f"Griglia DEM allineato non coincide con l'ortofoto: {mm}.")
                )
            feedback.pushInfo("    griglia DEM conforme OK (dimensioni, origine, pixel size)")
            inputs.append(dem_aligned)
        else:
            dem_aligned = None
            feedback.pushInfo("[0] DEM non fornito: salto warp e allineamento DEM.")

        # --- normal map -----------------------------------------------------
        if use_normal:
            n_path = normal_lyr.source()
            mm_normal = self._grid_mismatch(normal_lyr, ortho_lyr)
            conforme = mm_normal is None

            if not conforme and not normal_realign:
                raise QgsProcessingException(
                    self.tr(
                        "La normal map NON e' conforme alla griglia dell'ortofoto "
                        f"({mm_normal}). Rigenerala pixel-perfect (Build_normal_map "
                        "modalita' B) oppure attiva 'Riallinea la normal map se "
                        "non conforme'."
                    )
                )

            normal_tagged = p("normal_crs.tif")
            if conforme:
                # Gia' pixel-perfect: basta assegnare il CRS master su disco
                # (nessun ricampionamento), cosi' il VRT non la scarta per CRS
                # eterogeneo. I versori restano intatti.
                feedback.pushInfo("    assegno il CRS master alla normal map per il VRT...")
                processing.run(
                    "gdal:translate",
                    {
                        "INPUT": n_path,
                        "TARGET_CRS": master_crs_obj,
                        "NODATA": None,
                        "COPY_SUBDATASETS": False,
                        "CREATION_OPTIONS": "COMPRESS=LZW|TILED=YES",
                        "DATA_TYPE": 6,      # Float32 (coseni direttori / versori)
                        "EXTRA": "",
                        "OUTPUT": normal_tagged,
                    },
                    context=context,
                    feedback=feedback,
                    is_child_algorithm=True,
                )
            else:
                # Non conforme + riallineamento richiesto: warp sulla griglia
                # master, per coordinate (stesso trattamento del DEM). La normal
                # map e' un campo di VERSORI: si usa NEAREST per non mediare /
                # ruotare le direzioni; resta una piccola imprecisione sub-pixel.
                # Forziamo SOURCE_CRS = master perche' l'ENVI locale non ha un CRS
                # geodetico e GDAL non troverebbe l'operazione di trasformazione.
                feedback.pushInfo(
                    "    [!] normal map non conforme: riallineo con warp "
                    "(NEAREST, versori ricampionati)."
                )
                processing.run(
                    "gdal:warpreproject",
                    {
                        "INPUT": n_path,
                        "SOURCE_CRS": master_crs_obj,
                        "TARGET_CRS": master_crs_obj,
                        "RESAMPLING": 0,   # 0 = Nearest: non media i versori
                        "NODATA": None,
                        "TARGET_RESOLUTION": res,
                        "TARGET_EXTENT": te_rect,
                        "TARGET_EXTENT_CRS": master_crs_obj,
                        "DATA_TYPE": 6,    # Float32
                        "MULTITHREADING": True,
                        "EXTRA": "-tap -co COMPRESS=LZW -co TILED=YES",
                        "OUTPUT": normal_tagged,
                    },
                    context=context,
                    feedback=feedback,
                    is_child_algorithm=True,
                )
                n_chk = QgsRasterLayer(normal_tagged, "normal_aligned")
                mm2 = self._grid_mismatch(n_chk, ortho_lyr)
                if mm2:
                    raise QgsProcessingException(
                        self.tr(
                            "Riallineamento normal map fallito: griglia ancora non "
                            f"conforme ({mm2}). Rigenerala pixel-perfect a monte."
                        )
                    )
                feedback.pushInfo("    normal map riallineata, griglia conforme OK")

            inputs.append(normal_tagged)

        # Banda attese = ortofoto(3) + DEM(1 se c'e') + normal(3 se c'e').
        n_expected = 3 + (1 if use_dem else 0) + (3 if use_normal else 0)

        # Layout dinamico: chiave logica della banda -> numero banda GDAL
        # (1-based) nello stack completo. Serve per il raster custom.
        band_layout = {"R": 1, "G": 2, "B": 3}
        next_band = 4
        if use_dem:
            band_layout["DEM"] = next_band
            next_band += 1
        if use_normal:
            band_layout["NX"] = next_band
            band_layout["NY"] = next_band + 1
            band_layout["NZ"] = next_band + 2
            next_band += 3

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
                "ASSIGN_CRS": master_crs_obj,   # CRS esplicito sul VRT risultante
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

        results = {"OUTPUT_FULL": out_full, "VRT": vrt}
        if dem_aligned is not None:
            results["DEM_ALIGNED"] = dem_aligned

        # --- FASE 2b: raster custom (bande selezionate, da 1 a 7) -----------
        if make_custom:
            if not custom_idx:
                raise QgsProcessingException(
                    self.tr(
                        "Nessuna banda selezionata per il raster custom. "
                        "Seleziona almeno una banda o disattiva l'opzione."
                    )
                )

            # QGIS ordina gli indici della multi-selezione in modo crescente:
            # l'ordine di output segue l'ordine logico (R,G,B,DEM,NX,NY,NZ),
            # non l'ordine di spunta. Ogni indice enum punta a una chiave logica
            # in CUSTOM_CHOICES; la chiave viene risolta nel numero di banda
            # reale tramite band_layout, che riflette cosa e' davvero presente.
            chosen_idx = sorted(custom_idx)
            bands_sel = []
            for idx in chosen_idx:
                key = self.CUSTOM_CHOICES[idx][0]   # es. "DEM", "NX", ...
                if key not in band_layout:
                    # Banda richiesta ma input corrispondente assente.
                    if key == "DEM":
                        motivo = "il DEM non e' stato fornito"
                    elif key in ("NX", "NY", "NZ"):
                        motivo = "la normal map non e' stata fornita"
                    else:
                        motivo = "la banda non e' presente nello stack"
                    raise QgsProcessingException(
                        self.tr(
                            f"Hai scelto la banda '{self.SRC_BAND_LABELS[idx]}' "
                            f"ma {motivo}. Rimuovila dalla selezione o fornisci "
                            "l'input richiesto."
                        )
                    )
                bands_sel.append(band_layout[key])

            n_custom = len(bands_sel)
            labels = ", ".join(self.SRC_BAND_LABELS[i] for i in chosen_idx)
            feedback.pushInfo(f"[2b] Rearrange bands -> {bands_sel}  ({labels})")
            out_custom = p(f"stack_custom_{n_custom}b.tif")
            processing.run(
                "gdal:rearrange_bands",
                {
                    "INPUT": out_full,
                    "BANDS": bands_sel,
                    "CREATION_OPTIONS": "COMPRESS=LZW|TILED=YES",
                    "DATA_TYPE": 6,       # Float32
                    "OUTPUT": out_custom,
                },
                context=context,
                feedback=feedback,
                is_child_algorithm=True,
            )
            feedback.pushInfo(f"    -> {out_custom}")
            results["OUTPUT_CUSTOM"] = out_custom

        bande = " ".join(f"{num}={key}" for key, num in sorted(band_layout.items(), key=lambda kv: kv[1]))
        feedback.pushInfo(f"Fatto. Bande stack completo: {bande}")
        results[self.OUT_DIR] = out_dir
        return results
