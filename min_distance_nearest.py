"""
Minimum Distance to Nearest Feature
====================================

QGIS Processing script that calculates the minimum distance between each
feature in a source layer and the nearest feature in a target layer.

Source geometry:
  - Point layers: uses the point geometry directly
  - Line/Polygon layers: uses the centroid

Target distance mode (user choice):
  0 - Nearest segment: shortest distance to the target geometry (default)
  1 - Nearest vertex: distance to the closest vertex of the target
  2 - Centroid: distance to the centroid of the target

Output: source layer enriched with distance fields.

Native alternatives
-------------------
- native:shortestline
    METHOD: 0 = nearest point on feature, 1 = centroid
    Produces a line layer (not enriched source). No vertex mode.

- native:joinbynearest
    Centroid-based only. Joins attributes from nearest feature.

This script adds value by:
  - nearest vertex mode (not available natively)
  - automatic centroid for non-point source layers
  - enriched source layer output with near point coordinates
  - zero external dependencies (pure PyQGIS + QgsSpatialIndex)
"""

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsSpatialIndex,
    QgsWkbTypes,
)
import math


class MinDistanceToNearest(QgsProcessingAlgorithm):
    """Calculates minimum distance from source features to nearest target."""

    INPUT_SOURCE = "INPUT_SOURCE"
    INPUT_TARGET = "INPUT_TARGET"
    TARGET_MODE = "TARGET_MODE"
    TARGET_ID_FIELD = "TARGET_ID_FIELD"
    MAX_DISTANCE = "MAX_DISTANCE"
    N_NEAREST = "N_NEAREST"
    OUTPUT = "OUTPUT"

    TARGET_MODES = [
        "Segmento piu' vicino (nearest point on geometry)",
        "Vertice piu' vicino",
        "Centroide",
    ]

    def tr(self, string):
        return QCoreApplication.translate("MinDistanceToNearest", string)

    def createInstance(self):
        return MinDistanceToNearest()

    def name(self):
        return "min_distance_to_nearest"

    def displayName(self):
        return self.tr("Distanza minima al feature piu' vicino")

    def group(self):
        return self.tr("Vector analysis")

    def groupId(self):
        return "vectoranalysis"

    def shortHelpString(self):
        return self.tr(
            "Calcola la distanza minima tra ciascun elemento sorgente e "
            "l'elemento piu' vicino nel layer di destinazione.\n\n"
            "SORGENTE: per layer di punti usa il punto; per linee/poligoni "
            "usa il centroide.\n\n"
            "DESTINAZIONE (a scelta):\n"
            "  - Segmento piu' vicino: distanza minima alla geometria\n"
            "  - Vertice piu' vicino: distanza al vertice piu' vicino\n"
            "  - Centroide: distanza al centroide dell'elemento\n\n"
            "Alternativa nativa: 'Shortest line between features' "
            "(native:shortestline) — offre solo segmento e centroide, "
            "senza modalita' vertice."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_SOURCE,
                self.tr("Layer sorgente"),
                [QgsProcessing.TypeVectorAnyGeometry],
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_TARGET,
                self.tr("Layer destinazione"),
                [QgsProcessing.TypeVectorAnyGeometry],
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.TARGET_MODE,
                self.tr("Calcola distanza verso"),
                options=self.TARGET_MODES,
                defaultValue=0,
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.TARGET_ID_FIELD,
                self.tr("Campo identificativo destinazione (opzionale)"),
                parentLayerParameterName=self.INPUT_TARGET,
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.N_NEAREST,
                self.tr("Numero di vicini da restituire"),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=1,
                minValue=1,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.MAX_DISTANCE,
                self.tr("Distanza massima di ricerca (0 = illimitata)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=0.0,
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("Layer in uscita"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT_SOURCE, context)
        target = self.parameterAsSource(parameters, self.INPUT_TARGET, context)
        if source is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.INPUT_SOURCE)
            )
        if target is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.INPUT_TARGET)
            )

        target_mode = self.parameterAsEnum(parameters, self.TARGET_MODE, context)
        target_id_field = self.parameterAsString(
            parameters, self.TARGET_ID_FIELD, context
        )
        n_nearest = self.parameterAsInt(parameters, self.N_NEAREST, context)
        max_dist = self.parameterAsDouble(parameters, self.MAX_DISTANCE, context)
        if max_dist <= 0:
            max_dist = float("inf")

        # Determine source geometry type
        source_is_point = QgsWkbTypes.geometryType(source.wkbType()) == QgsWkbTypes.PointGeometry
        if source_is_point:
            feedback.pushInfo(self.tr("Sorgente: layer di punti (usa geometria diretta)"))
        else:
            feedback.pushInfo(self.tr("Sorgente: layer non-punto (usa centroide)"))

        mode_labels = ["segmento", "vertice", "centroide"]
        feedback.pushInfo(
            self.tr(f"Destinazione: distanza al {mode_labels[target_mode]}")
        )

        # ---- Build output fields --------------------------------------------
        out_fields = QgsFields()
        # Copy all source fields
        for i in range(source.fields().count()):
            out_fields.append(source.fields().field(i))

        # Add distance result fields (per neighbor)
        for k in range(n_nearest):
            suffix = f"_{k + 1}" if n_nearest > 1 else ""
            out_fields.append(QgsField(f"near_fid{suffix}", QVariant.LongLong))
            if target_id_field:
                out_fields.append(
                    QgsField(f"near_id{suffix}", QVariant.String)
                )
            out_fields.append(QgsField(f"near_dist{suffix}", QVariant.Double))
            out_fields.append(QgsField(f"near_x{suffix}", QVariant.Double))
            out_fields.append(QgsField(f"near_y{suffix}", QVariant.Double))

        # Determine output geometry type (same as source)
        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            out_fields,
            source.wkbType(),
            source.sourceCrs(),
        )
        if sink is None:
            raise QgsProcessingException(
                self.invalidSinkError(parameters, self.OUTPUT)
            )

        # ---- Index target features ------------------------------------------
        feedback.pushInfo(self.tr("Indicizzazione layer destinazione..."))
        target_features = {}  # {fid: QgsFeature}
        target_index = QgsSpatialIndex()

        for feat in target.getFeatures():
            if feedback.isCanceled():
                break
            target_features[feat.id()] = feat
            target_index.addFeature(feat)

        feedback.pushInfo(
            self.tr(f"  {len(target_features)} elementi indicizzati.")
        )

        # ---- Process source features ----------------------------------------
        total = source.featureCount()
        if total <= 0:
            total = 1

        # How many candidates to pull from spatial index
        # (we need extra because index uses bounding box, not exact geometry)
        n_candidates = max(n_nearest * 5, 20)

        for current, feat in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break
            feedback.setProgress(int(current / total * 100))

            # Source point
            src_geom = feat.geometry()
            if src_geom.isEmpty():
                continue

            if source_is_point:
                src_point = src_geom.asPoint()
            else:
                centroid = src_geom.centroid()
                src_point = centroid.asPoint()

            src_point_geom = QgsGeometry.fromPointXY(QgsPointXY(src_point))

            # Find nearest candidates via spatial index
            candidate_fids = target_index.nearestNeighbor(
                QgsPointXY(src_point), n_candidates
            )

            # Compute exact distances
            distances = []  # [(dist, near_pt, target_feat), ...]

            for fid in candidate_fids:
                if fid not in target_features:
                    continue
                t_feat = target_features[fid]
                t_geom = t_feat.geometry()
                if t_geom.isEmpty():
                    continue

                if target_mode == 0:
                    # Nearest segment (shortest distance to geometry)
                    dist = src_point_geom.distance(t_geom)
                    near_geom = t_geom.nearestPoint(src_point_geom)
                    near_pt = near_geom.asPoint()

                elif target_mode == 1:
                    # Nearest vertex
                    closest = t_geom.closestVertex(QgsPointXY(src_point))
                    # closestVertex returns (point, index, before, after, sqrDist)
                    near_pt = closest[0]
                    dist = math.sqrt(closest[4])

                elif target_mode == 2:
                    # Centroid
                    t_centroid = t_geom.centroid()
                    near_pt = t_centroid.asPoint()
                    dist = src_point_geom.distance(t_centroid)

                if dist <= max_dist:
                    distances.append((dist, near_pt, t_feat))

            # Sort by distance, take n_nearest
            distances.sort(key=lambda x: x[0])
            distances = distances[:n_nearest]

            # Build output feature
            feat_out = QgsFeature(out_fields)
            feat_out.setGeometry(src_geom)

            # Start with source attributes
            attrs = list(feat.attributes())

            # Append near fields for each neighbor
            for k in range(n_nearest):
                if k < len(distances):
                    dist, near_pt, t_feat = distances[k]
                    attrs.append(t_feat.id())  # near_fid
                    if target_id_field:
                        attrs.append(t_feat[target_id_field])  # near_id
                    attrs.append(dist)  # near_dist
                    attrs.append(near_pt.x())  # near_x
                    attrs.append(near_pt.y())  # near_y
                else:
                    # No neighbor found within max_distance
                    attrs.append(None)
                    if target_id_field:
                        attrs.append(None)
                    attrs.append(None)
                    attrs.append(None)
                    attrs.append(None)

            feat_out.setAttributes(attrs)
            sink.addFeature(feat_out, QgsFeatureSink.FastInsert)

        feedback.pushInfo(self.tr("Completato."))
        return {self.OUTPUT: dest_id}
