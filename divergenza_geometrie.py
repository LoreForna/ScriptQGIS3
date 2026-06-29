"""
Divergenza tra due layer vettoriali (punti / linee / poligoni)
==============================================================
Estrae le zone in cui due layer dello stesso tipo si discostano oltre una
soglia di tolleranza. La logica si adatta al tipo di geometria.

  LINEE     -> OUTPUT_LINEE: sub-tratti di A fuori dal corridoio di tolleranza
               di B (e viceversa), via doppio buffer + difference.
  POLIGONI  -> OUTPUT_AREE : symmetrical difference, aree in uno e non
                             nell'altro (geometria poligonale, misura = area m2)
               OUTPUT_BORDI: scostamenti del contorno oltre tolleranza, come
                             LINEE native (misura = lunghezza m, non gonfiata)
  PUNTI     -> OUTPUT_PUNTI: punti di A senza corrispondente in B entro la
                             tolleranza (e viceversa).

Ogni esecuzione popola solo i sink pertinenti al tipo di input; gli altri
restano vuoti. Indipendente da direzione di digitalizzazione, ordine dei
vertici e progressiva condivisa. Usare un CRS proiettato metrico (UTM).

Campi comuni a tutti gli output:
  origine    A / B
  tipo_div   linea / area / bordo / punto
  misura     lunghezza (m) | area (m2) | distanza al piu' vicino (m)

QGIS 3.x  -  Lorenzo Forna
"""

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterDistance,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterBoolean,
    QgsProcessingUtils,
    QgsFeatureSink,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsWkbTypes,
    QgsSpatialIndex,
    QgsProcessingException,
)
from qgis.PyQt.QtCore import QVariant, QCoreApplication
import processing


class DivergenzaGeometrie(QgsProcessingAlgorithm):
    LAYER_A = "LAYER_A"
    LAYER_B = "LAYER_B"
    TOLLERANZA = "TOLLERANZA"
    LUNGH_MIN = "LUNGH_MIN"
    BIDIREZIONALE = "BIDIREZIONALE"
    OUT_LINEE = "OUTPUT_LINEE"
    OUT_AREE = "OUTPUT_AREE"
    OUT_BORDI = "OUTPUT_BORDI"
    OUT_PUNTI = "OUTPUT_PUNTI"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.LAYER_A, "Layer A",
            [QgsProcessing.TypeVectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.LAYER_B, "Layer B",
            [QgsProcessing.TypeVectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterDistance(
            self.TOLLERANZA, "Soglia di tolleranza",
            parentParameterName=self.LAYER_A,
            defaultValue=5.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterDistance(
            self.LUNGH_MIN,
            "Lunghezza minima tratto (scarta i frammenti, solo linee/bordi)",
            parentParameterName=self.LAYER_A,
            defaultValue=0.5, minValue=0.0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.BIDIREZIONALE,
            "Bidirezionale (anche divergenze di B rispetto ad A)",
            defaultValue=True))
        # Output multipli: si popola solo quello pertinente al tipo di input
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_LINEE, "Divergenze - linee",
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_AREE, "Divergenze - aree (poligoni)",
            QgsProcessing.TypeVectorPolygon, optional=True, createByDefault=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_BORDI, "Divergenze - bordi (linee)",
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_PUNTI, "Divergenze - punti",
            QgsProcessing.TypeVectorPoint, optional=True, createByDefault=True))

    # ---------- helper di geoprocessing ----------

    def _buffer(self, layer, raggio, context, feedback):
        return processing.run("native:buffer", {
            "INPUT": layer, "DISTANCE": raggio, "SEGMENTS": 12,
            "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "DISSOLVE": True,
            "OUTPUT": "memory:",
        }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]

    def _difference(self, inp, overlay, context, feedback):
        return processing.run("native:difference", {
            "INPUT": inp, "OVERLAY": overlay, "OUTPUT": "memory:",
        }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]

    def _boundary(self, layer, context, feedback):
        return processing.run("native:boundary", {
            "INPUT": layer, "OUTPUT": "memory:",
        }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]

    def _fields(self):
        f = QgsFields()
        f.append(QgsField("origine", QVariant.String, len=1))
        f.append(QgsField("tipo_div", QVariant.String, len=6))
        f.append(QgsField("misura", QVariant.Double))
        return f

    # ---------- algoritmo principale ----------

    def processAlgorithm(self, parameters, context, feedback):
        src_a = self.parameterAsSource(parameters, self.LAYER_A, context)
        src_b = self.parameterAsSource(parameters, self.LAYER_B, context)
        if src_a is None or src_b is None:
            raise QgsProcessingException("Layer di input non validi")

        tol = self.parameterAsDouble(parameters, self.TOLLERANZA, context)
        lmin = self.parameterAsDouble(parameters, self.LUNGH_MIN, context)
        bidir = self.parameterAsBool(parameters, self.BIDIREZIONALE, context)

        geom_a = QgsWkbTypes.geometryType(src_a.wkbType())
        geom_b = QgsWkbTypes.geometryType(src_b.wkbType())
        if geom_a != geom_b:
            raise QgsProcessingException(
                "I due layer devono essere dello stesso tipo di geometria "
                "(punto-punto, linea-linea, poligono-poligono).")

        fields = self._fields()
        crs = src_a.sourceCrs()

        # crea tutti i sink (quelli non pertinenti resteranno vuoti)
        sink_linee, id_linee = self.parameterAsSink(
            parameters, self.OUT_LINEE, context, fields,
            QgsWkbTypes.MultiLineString, crs)
        sink_aree, id_aree = self.parameterAsSink(
            parameters, self.OUT_AREE, context, fields,
            QgsWkbTypes.MultiPolygon, crs)
        sink_bordi, id_bordi = self.parameterAsSink(
            parameters, self.OUT_BORDI, context, fields,
            QgsWkbTypes.MultiLineString, crs)
        sink_punti, id_punti = self.parameterAsSink(
            parameters, self.OUT_PUNTI, context, fields,
            QgsWkbTypes.Point, crs)

        if geom_a == QgsWkbTypes.LineGeometry:
            self._diverg_linee(parameters, sink_linee, fields, tol, lmin,
                               bidir, context, feedback)
        elif geom_a == QgsWkbTypes.PolygonGeometry:
            self._diverg_poligoni(parameters, sink_aree, sink_bordi, fields,
                                  tol, lmin, bidir, context, feedback)
        else:
            self._diverg_punti(src_a, src_b, sink_punti, fields, tol,
                               bidir, context, feedback)

        return {
            self.OUT_LINEE: id_linee,
            self.OUT_AREE: id_aree,
            self.OUT_BORDI: id_bordi,
            self.OUT_PUNTI: id_punti,
        }

    # ---------- LINEE ----------

    def _diverg_linee(self, parameters, sink, fields, tol, lmin,
                      bidir, context, feedback):
        feedback.pushInfo("Tipo: LINEE - buffer + difference simmetrico")
        buf_b = self._buffer(parameters[self.LAYER_B], tol, context, feedback)
        diff_a = self._difference(parameters[self.LAYER_A], buf_b, context, feedback)
        self._scrivi_linee(diff_a, "A", "linea", sink, fields, lmin, context, feedback)
        if bidir:
            buf_a = self._buffer(parameters[self.LAYER_A], tol, context, feedback)
            diff_b = self._difference(parameters[self.LAYER_B], buf_a, context, feedback)
            self._scrivi_linee(diff_b, "B", "linea", sink, fields, lmin, context, feedback)

    # ---------- POLIGONI ----------

    def _diverg_poligoni(self, parameters, sink_aree, sink_bordi, fields,
                         tol, lmin, bidir, context, feedback):
        feedback.pushInfo("Tipo: POLIGONI - symmetrical difference + bordi nativi")
        # 1) AREE: presenti in uno e non nell'altro
        diff_a = self._difference(parameters[self.LAYER_A],
                                  parameters[self.LAYER_B], context, feedback)
        self._scrivi_aree(diff_a, "A", sink_aree, fields, context, feedback)
        if bidir:
            diff_b = self._difference(parameters[self.LAYER_B],
                                      parameters[self.LAYER_A], context, feedback)
            self._scrivi_aree(diff_b, "B", sink_aree, fields, context, feedback)
        # 2) BORDI: contorni -> linee native -> buffer/difference (lunghezza reale)
        bound_a = self._boundary(parameters[self.LAYER_A], context, feedback)
        bound_b = self._boundary(parameters[self.LAYER_B], context, feedback)
        buf_bb = self._buffer(bound_b, tol, context, feedback)
        bdiff_a = self._difference(bound_a, buf_bb, context, feedback)
        self._scrivi_linee(bdiff_a, "A", "bordo", sink_bordi, fields, lmin, context, feedback)
        if bidir:
            buf_ba = self._buffer(bound_a, tol, context, feedback)
            bdiff_b = self._difference(bound_b, buf_ba, context, feedback)
            self._scrivi_linee(bdiff_b, "B", "bordo", sink_bordi, fields, lmin, context, feedback)

    # ---------- PUNTI ----------

    def _diverg_punti(self, src_a, src_b, sink, fields, tol,
                      bidir, context, feedback):
        feedback.pushInfo("Tipo: PUNTI - mancata corrispondenza entro tolleranza")
        self._scrivi_punti(src_a, src_b, "A", sink, fields, tol, feedback)
        if bidir:
            self._scrivi_punti(src_b, src_a, "B", sink, fields, tol, feedback)

    # ---------- writer ----------

    def _scrivi_linee(self, layer_id, origine, tipo, sink, fields, lmin,
                      context, feedback):
        lyr = QgsProcessingUtils.mapLayerFromString(layer_id, context)
        scartati = 0
        for f in lyr.getFeatures():
            geom = f.geometry()
            if geom.isEmpty():
                continue
            if lmin > 0 and geom.isMultipart():
                parti = [QgsGeometry.fromPolylineXY(p)
                         for p in geom.asMultiPolyline()
                         if QgsGeometry.fromPolylineXY(p).length() >= lmin]
                scartati += len(geom.asMultiPolyline()) - len(parti)
                if not parti:
                    continue
                geom = QgsGeometry.collectGeometry(parti)
            elif lmin > 0 and geom.length() < lmin:
                scartati += 1
                continue
            self._add(sink, fields, geom, origine, tipo, geom.length())
        if lmin > 0:
            feedback.pushInfo("  origine %s (%s): scartati %d frammenti < %.3f m"
                              % (origine, tipo, scartati, lmin))

    def _scrivi_aree(self, layer_id, origine, sink, fields, context, feedback):
        lyr = QgsProcessingUtils.mapLayerFromString(layer_id, context)
        for f in lyr.getFeatures():
            geom = f.geometry()
            if geom.isEmpty() or geom.area() <= 0:
                continue
            self._add(sink, fields, geom, origine, "area", geom.area())

    def _scrivi_punti(self, src_self, src_other, origine, sink, fields, tol,
                      feedback):
        other_feats = {ft.id(): ft for ft in src_other.getFeatures()}
        index = QgsSpatialIndex()
        for ft in other_feats.values():
            index.addFeature(ft)
        senza = 0
        for f in src_self.getFeatures():
            g = f.geometry()
            if g.isEmpty():
                continue
            pt = g.asPoint()
            vicini = index.nearestNeighbor(pt, 1)
            dist = None
            if vicini:
                og = other_feats[vicini[0]].geometry()
                dist = g.distance(og)
            if dist is None or dist > tol:
                senza += 1
                self._add(sink, fields, g, origine, "punto",
                          dist if dist is not None else -1.0)
        feedback.pushInfo("  origine %s: %d punti senza corrispondente entro %.3f m"
                          % (origine, senza, tol))

    def _add(self, sink, fields, geom, origine, tipo, misura):
        out = QgsFeature(fields)
        out.setGeometry(geom)
        out.setAttribute("origine", origine)
        out.setAttribute("tipo_div", tipo)
        out.setAttribute("misura", float(misura))
        sink.addFeature(out, QgsFeatureSink.FastInsert)

    # ---------- metadata ----------

    def name(self):
        return "divergenza_geometrie"

    def displayName(self):
        return "Divergenza tra due layer (punti/linee/poligoni)"

    def group(self):
        return "Analisi tracciati"

    def groupId(self):
        return "analisi_tracciati"

    def shortHelpString(self):
        return ("Estrae le divergenze tra due layer dello stesso tipo. "
                "LINEE -> tratti fuori dal corridoio di tolleranza. "
                "POLIGONI -> aree non comuni (symmetrical difference) come "
                "poligoni + scostamenti del bordo oltre tolleranza come linee "
                "native (lunghezza reale). PUNTI -> punti senza corrispondente "
                "entro la tolleranza. Si popola solo l'output pertinente al tipo "
                "di input. Campi: origine (A/B), tipo_div, misura "
                "(lunghezza m / area m2 / distanza m). CRS proiettato metrico (UTM).")

    def tr(self, s):
        return QCoreApplication.translate("Processing", s)

    def createInstance(self):
        return DivergenzaGeometrie()
