# -*- coding: utf-8 -*-
# =============================================================================
# Stack raster per analisi metrologica del paramento - QGIS Processing Script
# Ortofoto RGB (master) + DEM + mappa delle normali + bande derivate
# -> GeoTIFF multibanda float32 sulla griglia ESATTA dell'ortofoto.
# QGIS 3.x (GDAL >= 3.4)
#
# INSTALLAZIONE
#   Processing Toolbox -> icona Python -> "Aggiungi script agli strumenti..."
#   Comparira' sotto "Scripts > Raster".
#
# LOGICA
#   - Griglia master = ortofoto. Ogni altro input viene confrontato con la
#     griglia master in modo STRETTO (dimensioni, passo, origine entro 1/1000
#     di pixel). Se coincide (caso degli output di build_ortho_dem_normali.py)
#     i valori vengono copiati pixel per pixel: nessun ricampionamento, nessun
#     -tap, nessuna riproiezione.
#   - Se un input NON coincide:
#       DEM            -> warp sulla griglia master (estensione e dimensioni
#                         esatte, niente -tap) con il metodo scelto;
#       normali/derivate -> errore, oppure warp NEAREST se richiesto (i versori e
#                         gli angoli non si interpolano). Meglio rigenerarli.
#   - CRS: tutte le bande ereditano un unico CRS (quello dell'ortofoto o quello
#     scelto). Nessuna riproiezione se i CRS sono locali/ingegneristici.
#   - Ortofoto: i pixel trasparenti (alfa = 0 / maschera) diventano NaN in
#     tutte le bande dell'ortofoto.
#   - NoData: NaN in output. Il NoData del DEM (es. -32767) diventa NaN.
#
# ORDINE DELLE BANDE
#   R, G, B -> DEM (se presente) -> n_u, n_v, n_w (se presenti)
#   -> bande derivate (se presenti: inclinazione, azimut, dispersione)
# =============================================================================

import os
import math

import numpy as np
from osgeo import gdal, osr

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterEnum,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterFolderDestination,
    QgsCoordinateReferenceSystem,
)

gdal.UseExceptions()

TOL_ORIGIN_PX = 1e-3     # tolleranza sull'origine (frazione di pixel)
TOL_PIXEL_REL = 1e-9     # tolleranza relativa sul passo

RESAMPLING = ["near", "bilinear", "cubic", "cubicspline", "lanczos", "average"]

# chiave logica, etichetta, gruppo
BAND_CHOICES = [
    ("R", "R (ortofoto)", "ortho"),
    ("G", "G (ortofoto)", "ortho"),
    ("B", "B (ortofoto)", "ortho"),
    ("DEM", "DEM (quota / fuori piano)", "dem"),
    ("NU", "n_u (normale, asse orizzontale)", "normal"),
    ("NV", "n_v (normale, asse verticale)", "normal"),
    ("NW", "n_w (normale, asse uscente)", "normal"),
    ("INCL", "Inclinazione rispetto al piano [deg]", "derived"),
    ("AZIM", "Azimut nel piano [deg]", "derived"),
    ("DISP", "Dispersione normali 1-R", "derived"),
]


# =============================================================================
# FUNZIONI GDAL (indipendenti da QGIS)
# =============================================================================
def ds_path(source):
    """Percorso GDAL dalla sorgente di un layer QGIS (rimuove |layername=...)."""
    return source.split("|")[0]


def grid_of(ds):
    return ds.GetGeoTransform(), ds.RasterXSize, ds.RasterYSize


def grid_mismatch(ds, master):
    """None se la griglia di ds coincide con quella del master, altrimenti
    una descrizione dello scarto."""
    gt, W, H = grid_of(ds)
    mgt, mW, mH = grid_of(master)
    if abs(gt[2]) > 0 or abs(gt[4]) > 0:
        return "raster ruotato"
    if (W, H) != (mW, mH):
        return "dimensioni {}x{} invece di {}x{}".format(W, H, mW, mH)
    if (abs(gt[1] - mgt[1]) > TOL_PIXEL_REL * abs(mgt[1]) or
            abs(gt[5] - mgt[5]) > TOL_PIXEL_REL * abs(mgt[5])):
        return "passo {:.10g} x {:.10g} invece di {:.10g} x {:.10g}".format(
            gt[1], gt[5], mgt[1], mgt[5])
    dx = abs(gt[0] - mgt[0]) / abs(mgt[1])
    dy = abs(gt[3] - mgt[3]) / abs(mgt[5])
    if dx > TOL_ORIGIN_PX or dy > TOL_ORIGIN_PX:
        return "origine sfalsata di ({:.4f}, {:.4f}) pixel".format(dx, dy)
    return None


def _srs(ds):
    wkt = ds.GetProjection()
    if not wkt:
        return None
    s = osr.SpatialReference()
    try:
        s.ImportFromWkt(wkt)
    except Exception:
        return None
    return s


def _is_mappable(s):
    """True se il CRS e' geografico o proiettato (riproiettabile)."""
    return s is not None and (s.IsProjected() or s.IsGeographic()) and not s.IsLocal()


def align_to_master(ds, master, resample, src_nodata=None, ignore_nodata=False, log=print):
    """Warp di ds sulla griglia esatta del master (VRT in memoria)."""
    mgt, mW, mH = grid_of(master)
    bounds = (mgt[0], mgt[3] + mH * mgt[5], mgt[0] + mW * mgt[1], mgt[3])
    s_src, s_dst = _srs(ds), _srs(master)
    kw = dict(format="VRT", outputBounds=bounds, width=mW, height=mH,
              resampleAlg=resample, multithread=True)
    src = ds
    if _is_mappable(s_src) and _is_mappable(s_dst) and not s_src.IsSame(s_dst):
        kw["srcSRS"] = s_src.ExportToWkt()
        kw["dstSRS"] = s_dst.ExportToWkt()
        log("    riproiezione {} -> {}".format(s_src.GetName(), s_dst.GetName()))
    else:
        # stesso sistema o sistemi locali: solo allineamento geometrico della griglia
        src = gdal.Translate("", ds, format="VRT", outputSRS=master.GetProjection() or None)
        if src.GetProjection():
            kw["dstSRS"] = src.GetProjection()
    if ignore_nodata:
        kw["srcNodata"] = "None"
    elif src_nodata is not None:
        kw["srcNodata"] = src_nodata
        kw["dstNodata"] = src_nodata
    out = gdal.Warp("", src, **kw)
    mm = grid_mismatch(out, master)
    if mm:
        raise RuntimeError("Allineamento fallito: " + mm)
    return out


def build_stack(ortho_path, dem_path=None, normal_path=None, derived_path=None,
                out_dir=".", dem_resampling=1, dem_ignore_nodata=False, realign=False,
                target_wkt=None, custom_keys=None, log=print, progress=None, cancelled=None):
    """Crea stack_<N>b.tif (e stack_custom_<M>b.tif). Ritorna dict dei risultati."""
    os.makedirs(out_dir, exist_ok=True)
    master = gdal.Open(ortho_path)
    if master.RasterCount < 3:
        raise RuntimeError("L'ortofoto deve avere almeno 3 bande (RGB).")
    mgt, W, H = grid_of(master)
    if abs(mgt[2]) > 0 or abs(mgt[4]) > 0:
        raise RuntimeError("Ortofoto ruotata: non supportata.")
    log("MASTER ortofoto: {} x {} px, passo {:.10g} x {:.10g}, origine ({:.6f}, {:.6f})".format(
        W, H, mgt[1], mgt[5], mgt[0], mgt[3]))

    # ---- elenco delle bande: (chiave, descrizione, dataset, indice banda, nodata)
    bands = []
    for i, key in enumerate(("R", "G", "B")):
        bands.append((key, key, master, i + 1, None))
    mask_band = master.GetRasterBand(1).GetMaskBand()
    mask_flags = master.GetRasterBand(1).GetMaskFlags()
    use_mask = not (mask_flags & gdal.GMF_ALL_VALID)
    if use_mask:
        log("    ortofoto: maschera/alfa applicata (pixel trasparenti -> NaN)")
    keep = [master]

    if dem_path:
        dem = gdal.Open(dem_path)
        nd = dem.GetRasterBand(1).GetNoDataValue()
        mm = grid_mismatch(dem, master)
        if mm:
            log("[0] DEM non conforme ({}): warp {} sulla griglia master".format(
                mm, RESAMPLING[dem_resampling]))
            dem = align_to_master(dem, master, RESAMPLING[dem_resampling], nd,
                                  dem_ignore_nodata, log)
        else:
            log("[0] DEM conforme alla griglia master: copia diretta, nessun ricampionamento")
        keep.append(dem)
        bands.append(("DEM", dem.GetRasterBand(1).GetDescription() or "DEM", dem, 1,
                      None if dem_ignore_nodata else nd))

    for path, label, keys, group in ((normal_path, "normali", ("NU", "NV", "NW"), "normal"),
                                     (derived_path, "bande derivate", ("INCL", "AZIM", "DISP"), "derived")):
        if not path:
            continue
        ds = gdal.Open(path)
        mm = grid_mismatch(ds, master)
        if mm:
            if not realign:
                raise RuntimeError(
                    "Il raster {} NON e' conforme alla griglia dell'ortofoto ({}). Rigeneralo "
                    "con build_ortho_dem_normali.py oppure attiva il riallineamento "
                    "(NEAREST, approssimato).".format(label, mm))
            log("[!] {} non conformi ({}): warp NEAREST (approssimazione)".format(label, mm))
            ds = align_to_master(ds, master, "near", None, False, log)
        else:
            log("[0] {}: griglia conforme, copia diretta".format(label))
        keep.append(ds)
        n = min(ds.RasterCount, len(keys))
        if group == "normal" and n < 3:
            raise RuntimeError("La mappa delle normali deve avere 3 bande.")
        for i in range(n):
            desc = ds.GetRasterBand(i + 1).GetDescription() or keys[i]
            bands.append((keys[i], desc, ds, i + 1, ds.GetRasterBand(i + 1).GetNoDataValue()))

    layout = {b[0]: i for i, b in enumerate(bands)}
    N = len(bands)
    custom_idx = None
    if custom_keys:
        missing = [k for k in custom_keys if k not in layout]
        if missing:
            raise RuntimeError("Bande richieste ma non disponibili: {}".format(", ".join(missing)))
        custom_idx = [layout[k] for k in custom_keys]

    # ---- output
    if target_wkt:
        wkt = target_wkt
    else:
        wkt = master.GetProjection()
    drv = gdal.GetDriverByName("GTiff")
    opts = ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=DEFLATE",
            "PREDICTOR=3", "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS", "INTERLEAVE=BAND"]

    def create(path, idxs):
        out = drv.Create(path, W, H, len(idxs), gdal.GDT_Float32, opts)
        out.SetGeoTransform(mgt)
        if wkt:
            out.SetProjection(wkt)
        for j, bi in enumerate(idxs):
            ob = out.GetRasterBand(j + 1)
            ob.SetDescription(bands[bi][1])
            ob.SetNoDataValue(float("nan"))
        out.SetMetadataItem("STACK_BANDS", ",".join(bands[bi][0] for bi in idxs))
        out.SetMetadataItem("STACK_MASTER", os.path.basename(ortho_path))
        return out

    out_full = os.path.join(out_dir, "stack_{}b.tif".format(N))
    ds_full = create(out_full, list(range(N)))
    out_custom = None
    ds_custom = None
    if custom_idx:
        out_custom = os.path.join(out_dir, "stack_custom_{}b.tif".format(len(custom_idx)))
        ds_custom = create(out_custom, custom_idx)

    step = 512
    for y in range(0, H, step):
        if cancelled is not None and cancelled():
            raise RuntimeError("Operazione annullata.")
        h = min(step, H - y)
        mask = None
        if use_mask:
            mask = mask_band.ReadAsArray(0, y, W, h) == 0
        for bi, (key, desc, ds, idx, nd) in enumerate(bands):
            a = ds.GetRasterBand(idx).ReadAsArray(0, y, W, h).astype(np.float32)
            if key in ("R", "G", "B") and mask is not None:
                a[mask] = np.nan
            elif nd is not None and not (isinstance(nd, float) and math.isnan(nd)):
                a[a == np.float32(nd)] = np.nan
            ds_full.GetRasterBand(bi + 1).WriteArray(a, 0, y)
            if custom_idx and bi in custom_idx:
                for j, cbi in enumerate(custom_idx):
                    if cbi == bi:
                        ds_custom.GetRasterBand(j + 1).WriteArray(a, 0, y)
        if progress is not None:
            progress(100.0 * (y + h) / H)
    ds_full.FlushCache()
    ds_full = None
    if ds_custom is not None:
        ds_custom.FlushCache()
        ds_custom = None
    del keep
    log("Stack completo: {}  bande = {}".format(
        out_full, " ".join("{}={}".format(i + 1, b[0]) for i, b in enumerate(bands))))
    res = {"OUTPUT_FULL": out_full, "BANDS": [b[0] for b in bands]}
    if out_custom:
        log("Stack custom: {}  bande = {}".format(out_custom, " ".join(custom_keys)))
        res["OUTPUT_CUSTOM"] = out_custom
    return res


# =============================================================================
# ALGORITMO DI PROCESSING
# =============================================================================
class StackRasterMultiband(QgsProcessingAlgorithm):
    ORTHO = "ORTHO"
    DEM = "DEM"
    NORMAL = "NORMAL"
    DERIVED = "DERIVED"
    DEM_RESAMPLING = "DEM_RESAMPLING"
    DEM_IGNORE_NODATA = "DEM_IGNORE_NODATA"
    REALIGN = "NORMAL_REALIGN"
    TARGET_CRS = "TARGET_CRS"
    MAKE_CUSTOM = "MAKE_CUSTOM"
    CUSTOM_BANDS = "CUSTOM_BANDS"
    OUT_DIR = "OUT_DIR"

    RESAMPLING_LABELS = ["Nearest Neighbour", "Bilinear (2x2)", "Cubic (4x4)",
                         "Cubic B-Spline (4x4)", "Lanczos (6x6)", "Average"]

    def tr(self, string):
        return QCoreApplication.translate("StackRasterMultiband", string)

    def createInstance(self):
        return StackRasterMultiband()

    def name(self):
        return "stack_raster_multiband"

    def displayName(self):
        return self.tr("Stack raster multiband (paramento)")

    def group(self):
        return self.tr("Raster")

    def groupId(self):
        return "raster"

    def shortHelpString(self):
        return self.tr(
            "Impila ortofoto RGB (master) e, opzionalmente, DEM, mappa delle normali e bande "
            "derivate in un GeoTIFF float32 multibanda, sulla griglia esatta dell'ortofoto.\n\n"
            "Se gli input sono gia' sulla stessa griglia (output di build_ortho_dem_normali.py) "
            "i pixel vengono copiati senza alcun ricampionamento. La verifica e' stretta: "
            "dimensioni, passo e origine entro 1/1000 di pixel.\n\n"
            "DEM non conforme: warp sulla griglia master con estensione e dimensioni esatte "
            "(metodo scelto). Normali e bande derivate non conformi: errore, oppure warp "
            "NEAREST se attivi il riallineamento (approssimazione: meglio rigenerarle).\n\n"
            "Ordine bande: R, G, B -> DEM -> n_u, n_v, n_w -> inclinazione, azimut, dispersione "
            "(solo quelle presenti). NoData = NaN; i pixel trasparenti dell'ortofoto diventano NaN.\n\n"
            "CRS: per default quello dell'ortofoto; puoi assegnarne uno diverso (nessuna "
            "riproiezione, solo etichetta).\n\n"
            "Output: stack_<N>b.tif e, opzionale, stack_custom_<M>b.tif con le bande scelte "
            "(nell'ordine logico dell'elenco).")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.ORTHO, self.tr("Ortofoto RGB (raster MASTER)")))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.DEM, self.tr("DEM (opzionale)"), optional=True))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.NORMAL, self.tr("Mappa delle normali, 3 bande (opzionale)"), optional=True))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.DERIVED, self.tr("Bande derivate (opzionale)"), optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.DEM_RESAMPLING, self.tr("Ricampionamento del DEM (solo se NON conforme)"),
            options=self.RESAMPLING_LABELS, defaultValue=1))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DEM_IGNORE_NODATA,
            self.tr("Ignora il NoData del DEM (solo se il NoData coincide con quote reali, es. 0.0)"),
            defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.REALIGN,
            self.tr("Riallinea normali/derivate se non conformi (NEAREST, approssimato)"),
            defaultValue=False))
        self.addParameter(QgsProcessingParameterCrs(
            self.TARGET_CRS, self.tr("CRS da assegnare allo stack (opzionale, nessuna riproiezione)"),
            optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MAKE_CUSTOM, self.tr("Genera anche un raster con bande selezionate"),
            defaultValue=False))
        self.addParameter(QgsProcessingParameterEnum(
            self.CUSTOM_BANDS, self.tr("Bande del raster custom"),
            options=[b[1] for b in BAND_CHOICES], allowMultiple=True, defaultValue=[],
            optional=True))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUT_DIR, self.tr("Cartella di output")))

    def processAlgorithm(self, parameters, context, feedback):
        ortho = self.parameterAsRasterLayer(parameters, self.ORTHO, context)
        dem = self.parameterAsRasterLayer(parameters, self.DEM, context)
        normal = self.parameterAsRasterLayer(parameters, self.NORMAL, context)
        derived = self.parameterAsRasterLayer(parameters, self.DERIVED, context)
        if ortho is None or not ortho.isValid():
            raise QgsProcessingException(self.tr("Ortofoto non valida."))
        for lyr, nm in ((dem, "DEM"), (normal, "Mappa delle normali"), (derived, "Bande derivate")):
            if lyr is not None and not lyr.isValid():
                raise QgsProcessingException(self.tr("{} indicato ma non valido.").format(nm))

        crs = self.parameterAsCrs(parameters, self.TARGET_CRS, context)
        target_wkt = None
        if crs is not None and crs.isValid():
            target_wkt = crs.toWkt(QgsCoordinateReferenceSystem.WKT_PREFERRED_GDAL)

        custom_keys = None
        if self.parameterAsBool(parameters, self.MAKE_CUSTOM, context):
            idx = sorted(self.parameterAsEnums(parameters, self.CUSTOM_BANDS, context))
            if not idx:
                raise QgsProcessingException(self.tr(
                    "Nessuna banda selezionata per il raster custom."))
            custom_keys = [BAND_CHOICES[i][0] for i in idx]

        try:
            res = build_stack(
                ds_path(ortho.source()),
                ds_path(dem.source()) if dem else None,
                ds_path(normal.source()) if normal else None,
                ds_path(derived.source()) if derived else None,
                out_dir=self.parameterAsString(parameters, self.OUT_DIR, context),
                dem_resampling=self.parameterAsEnum(parameters, self.DEM_RESAMPLING, context),
                dem_ignore_nodata=self.parameterAsBool(parameters, self.DEM_IGNORE_NODATA, context),
                realign=self.parameterAsBool(parameters, self.REALIGN, context),
                target_wkt=target_wkt,
                custom_keys=custom_keys,
                log=feedback.pushInfo,
                progress=feedback.setProgress,
                cancelled=feedback.isCanceled,
            )
        except RuntimeError as e:
            raise QgsProcessingException(str(e))
        out = {"OUTPUT_FULL": res["OUTPUT_FULL"],
               self.OUT_DIR: self.parameterAsString(parameters, self.OUT_DIR, context)}
        if "OUTPUT_CUSTOM" in res:
            out["OUTPUT_CUSTOM"] = res["OUTPUT_CUSTOM"]
        return out
