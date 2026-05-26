"""
CSV / Grouped Points to Polygons
=========================================================

QGIS Processing script that converts a point layer (or a non-spatial table
with X/Y coordinate columns) into polygons by grouping features on a common
attribute field and computing either convex hull or concave hull.

Parameters
----------
- INPUT           : Input layer (point layer or CSV encoded in UTF-8 with X/Y columns)
- X_FIELD         : Field containing X / Easting coordinate (optional if
                    input already has point geometry)
- Y_FIELD         : Field containing Y / Northing coordinate (optional if
                    input already has point geometry)
- INPUT_CRS       : CRS of the X/Y coordinates (required when using X/Y
                    fields on a non-spatial table; ignored if input is spatial)
- GROUP_FIELD     : Field used to group points into distinct polygons
- ATTR_FIELDS     : Additional attribute fields to carry over (1st value/group)
- HULL_TYPE       : 0 = Convex Hull (default), 1 = Concave Hull
- CONCAVE_RATIO   : Concavity ratio 0-1 (0 = very concave, 1 ~ convex)
- OUTPUT_CRS      : CRS for output layers (blank = same as input)
- OUTPUT_POLYGONS : Output polygon layer
- OUTPUT_POINTS   : Output point layer (all input points, reprojected)

Native alternatives (convex hull only)
--------------------------------------
  Processing > Minimum Bounding Geometry (native:minimumboundinggeometry)
  Geometry type = Convex Hull, Field = your group field

This script adds value by:
  - accepting non-spatial tables with X/Y columns
  - offering both hull types in a single interface
  - handling degenerate groups (1-2 points) gracefully
  - carrying over multiple attribute fields
  - producing both points and polygons in one step
  - optional CRS reprojection
  - zero external dependencies (pure PyQGIS)
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
    QgsProcessingParameterCrs,
    QgsCoordinateTransform,
    QgsProject,
    QgsWkbTypes,
)


class PointsToGroupedPolygons(QgsProcessingAlgorithm):
    """Groups point features by a field and creates hull polygons."""

    # -- Parameter constants --------------------------------------------------
    INPUT = "INPUT"
    X_FIELD = "X_FIELD"
    Y_FIELD = "Y_FIELD"
    INPUT_CRS = "INPUT_CRS"
    GROUP_FIELD = "GROUP_FIELD"
    ATTR_FIELDS = "ATTR_FIELDS"
    HULL_TYPE = "HULL_TYPE"
    CONCAVE_RATIO = "CONCAVE_RATIO"
    OUTPUT_CRS = "OUTPUT_CRS"
    OUTPUT_POLYGONS = "OUTPUT_POLYGONS"
    OUTPUT_POINTS = "OUTPUT_POINTS"

    HULL_OPTIONS = ["Convex Hull", "Concave Hull"]

    # -- Metadata -------------------------------------------------------------
    def tr(self, string):
        return QCoreApplication.translate("PointsToGroupedPolygons", string)

    def createInstance(self):
        return PointsToGroupedPolygons()

    def name(self):
        return "points_to_grouped_polygons"

    def displayName(self):
        return self.tr("Grouped Points to Polygons")

    def group(self):
        return self.tr("Points to polygon")

    def groupId(self):
        return "vectorgeometry"

    def shortHelpString(self):
        return self.tr(
            "Raggruppa i punti per un campo attributo e crea un poligono "
            "(convex hull o concave hull) per ciascun gruppo.\n\n"
            "Il layer in ingresso puo' essere un layer di punti (geometria gia' "
            "definita) oppure un CSV codificato in UTF-8 con colonne X/Y.\n\n"
            "Se si specificano i campi X e Y, le coordinate vengono lette da "
            "quegli attributi; altrimenti si usa la geometria esistente.\n\n"
            "Produce due output: il layer dei poligoni e il layer dei punti "
            "(eventualmente riproiettati).\n\n"
            "Rapporto concavita': 0 = molto concavo, 1 = equivalente al convex hull."
        )

    # -- Parameter definition -------------------------------------------------
    def initAlgorithm(self, config=None):
        # Input layer (any vector: points, or table with X/Y columns)
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr("Layer in ingresso (punti o tabella)"),
                [QgsProcessing.TypeVector],
            )
        )

        # X coordinate field (optional — needed for non-spatial tables)
        self.addParameter(
            QgsProcessingParameterField(
                self.X_FIELD,
                self.tr("Campo coordinata X / Easting (opzionale se il layer ha geometria)"),
                parentLayerParameterName=self.INPUT,
                type=QgsProcessingParameterField.Any,
                optional=True,
            )
        )

        # Y coordinate field (optional)
        self.addParameter(
            QgsProcessingParameterField(
                self.Y_FIELD,
                self.tr("Campo coordinata Y / Northing (opzionale se il layer ha geometria)"),
                parentLayerParameterName=self.INPUT,
                type=QgsProcessingParameterField.Any,
                optional=True,
            )
        )

        # Input CRS (for X/Y fields on non-spatial tables)
        self.addParameter(
            QgsProcessingParameterCrs(
                self.INPUT_CRS,
                self.tr("CRS delle coordinate X/Y (se tabella non spaziale)"),
                optional=True,
            )
        )

        # Group field
        self.addParameter(
            QgsProcessingParameterField(
                self.GROUP_FIELD,
                self.tr("Campo di raggruppamento"),
                parentLayerParameterName=self.INPUT,
            )
        )

        # Additional attribute fields to carry over
        self.addParameter(
            QgsProcessingParameterField(
                self.ATTR_FIELDS,
                self.tr("Campi attributo aggiuntivi (opzionale)"),
                parentLayerParameterName=self.INPUT,
                allowMultiple=True,
                optional=True,
            )
        )

        # Hull type
        self.addParameter(
            QgsProcessingParameterEnum(
                self.HULL_TYPE,
                self.tr("Tipo di hull"),
                options=self.HULL_OPTIONS,
                defaultValue=0,
            )
        )

        # Concave ratio
        self.addParameter(
            QgsProcessingParameterNumber(
                self.CONCAVE_RATIO,
                self.tr("Rapporto concavita' (solo concave hull, 0-1)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.3,
                minValue=0.0,
                maxValue=1.0,
                optional=True,
            )
        )

        # Output CRS (optional)
        self.addParameter(
            QgsProcessingParameterCrs(
                self.OUTPUT_CRS,
                self.tr("CRS in uscita (vuoto = stesso dell'ingresso)"),
                optional=True,
            )
        )

        # Output polygon layer
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_POLYGONS,
                self.tr("Poligoni in uscita"),
                QgsProcessing.TypeVectorPolygon,
            )
        )

        # Output point layer
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_POINTS,
                self.tr("Punti in uscita"),
                QgsProcessing.TypeVectorPoint,
            )
        )

    # -- Processing logic -----------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.INPUT)
            )

        # Coordinate field names (may be empty)
        x_field_name = self.parameterAsString(parameters, self.X_FIELD, context)
        y_field_name = self.parameterAsString(parameters, self.Y_FIELD, context)
        use_xy_fields = bool(x_field_name) and bool(y_field_name)

        # Validate: if only one of X/Y is given, error
        if bool(x_field_name) != bool(y_field_name):
            raise QgsProcessingException(
                self.tr("Specificare entrambi i campi X e Y, o nessuno dei due.")
            )

        # Determine if input has usable point geometry
        has_geometry = (
            source.wkbType() != QgsWkbTypes.NoGeometry
            and source.wkbType() != QgsWkbTypes.Unknown
        )

        if not use_xy_fields and not has_geometry:
            raise QgsProcessingException(
                self.tr(
                    "Il layer in ingresso non ha geometria. "
                    "Specificare i campi X e Y per le coordinate."
                )
            )

        # Determine input CRS
        if use_xy_fields and not has_geometry:
            # Non-spatial table: CRS must come from INPUT_CRS parameter
            input_crs_param = self.parameterAsCrs(
                parameters, self.INPUT_CRS, context
            )
            if not input_crs_param.isValid():
                raise QgsProcessingException(
                    self.tr(
                        "Per una tabella non spaziale con campi X/Y, "
                        "e' necessario specificare il CRS delle coordinate (INPUT_CRS)."
                    )
                )
            input_crs = input_crs_param
        else:
            input_crs = source.sourceCrs()
            # Allow INPUT_CRS to override (useful when layer CRS is wrong)
            input_crs_param = self.parameterAsCrs(
                parameters, self.INPUT_CRS, context
            )
            if input_crs_param.isValid():
                input_crs = input_crs_param

        group_field_name = self.parameterAsString(
            parameters, self.GROUP_FIELD, context
        )
        attr_field_names = (
            self.parameterAsFields(parameters, self.ATTR_FIELDS, context) or []
        )
        hull_type = self.parameterAsEnum(parameters, self.HULL_TYPE, context)
        concave_ratio = self.parameterAsDouble(
            parameters, self.CONCAVE_RATIO, context
        )

        # Determine output CRS
        output_crs_param = self.parameterAsCrs(
            parameters, self.OUTPUT_CRS, context
        )
        if output_crs_param.isValid() and output_crs_param != input_crs:
            output_crs = output_crs_param
            do_reproject = True
        else:
            output_crs = input_crs
            do_reproject = False

        # Check concave hull availability
        use_concave = hull_type == 1
        if use_concave:
            test_geom = QgsGeometry.fromPointXY(QgsPointXY(0, 0))
            if not hasattr(test_geom, "concaveHull"):
                raise QgsProcessingException(
                    self.tr(
                        "Concave hull non disponibile in questa versione di QGIS. "
                        "Richiede QGIS >= 3.28 con GEOS >= 3.11."
                    )
                )

        # ---- Build output fields --------------------------------------------
        source_fields = source.fields()

        # Validate group field
        group_field_idx = source_fields.lookupField(group_field_name)
        if group_field_idx < 0:
            raise QgsProcessingException(
                self.tr(f"Campo '{group_field_name}' non trovato nel layer.")
            )

        # Validate X/Y fields
        x_field_idx = -1
        y_field_idx = -1
        if use_xy_fields:
            x_field_idx = source_fields.lookupField(x_field_name)
            y_field_idx = source_fields.lookupField(y_field_name)
            if x_field_idx < 0:
                raise QgsProcessingException(
                    self.tr(f"Campo X '{x_field_name}' non trovato nel layer.")
                )
            if y_field_idx < 0:
                raise QgsProcessingException(
                    self.tr(f"Campo Y '{y_field_name}' non trovato nel layer.")
                )

        # Additional attribute field indices
        attr_field_indices = []
        for fn in attr_field_names:
            if fn == group_field_name:
                continue
            idx = source_fields.lookupField(fn)
            if idx >= 0:
                attr_field_indices.append(idx)

        # -- Polygon output fields --
        poly_fields = QgsFields()
        poly_fields.append(source_fields.field(group_field_idx))
        for idx in attr_field_indices:
            poly_fields.append(source_fields.field(idx))
        poly_fields.append(QgsField("hull_type", QVariant.String))
        poly_fields.append(QgsField("n_points", QVariant.Int))

        # -- Point output fields (same as polygon) --
        point_fields = QgsFields(poly_fields)

        # ---- Create sinks --------------------------------------------------
        (poly_sink, poly_dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT_POLYGONS,
            context,
            poly_fields,
            QgsWkbTypes.Polygon,
            output_crs,
        )
        if poly_sink is None:
            raise QgsProcessingException(
                self.invalidSinkError(parameters, self.OUTPUT_POLYGONS)
            )

        (point_sink, point_dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT_POINTS,
            context,
            point_fields,
            QgsWkbTypes.Point,
            output_crs,
        )
        if point_sink is None:
            raise QgsProcessingException(
                self.invalidSinkError(parameters, self.OUTPUT_POINTS)
            )

        # Coordinate transform
        transform = None
        if do_reproject:
            transform = QgsCoordinateTransform(
                input_crs, output_crs, QgsProject.instance()
            )

        # ---- Pass 1: read features, write points, collect groups ------------
        groups = {}  # {group_value: {"points": [...], "attrs": {...}}}
        total = source.featureCount()
        if total <= 0:
            total = 1  # avoid division by zero; -1 = unknown count
        features = source.getFeatures()
        point_count = 0  # valid points actually written

        for current, feat in enumerate(features):
            if feedback.isCanceled():
                break
            feedback.setProgress(int(current / total * 40))

            group_val = feat[group_field_name]

            # Determine point coordinates
            if use_xy_fields:
                try:
                    x_val = float(feat[x_field_name])
                    y_val = float(feat[y_field_name])
                except (TypeError, ValueError):
                    feedback.pushInfo(
                        self.tr(
                            f"  Riga {current + 1}: coordinate X/Y non valide, ignorata."
                        )
                    )
                    continue
                pt = QgsPointXY(x_val, y_val)
            else:
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                pt = QgsPointXY(geom.asPoint())

            # Accumulate for polygon and point construction
            if group_val not in groups:
                attr_vals = {}
                for idx in attr_field_indices:
                    attr_vals[idx] = feat.attributes()[idx]
                groups[group_val] = {"points": [], "attrs": attr_vals}

            groups[group_val]["points"].append(pt)

        # ---- Pass 2: build polygons and write points ------------------------
        n_groups = len(groups)
        feedback.pushInfo(
            self.tr(f"Trovati {n_groups} gruppi. Costruzione hull in corso...")
        )

        hull_label = "concave" if use_concave else "convex"

        for i, (group_val, data) in enumerate(groups.items()):
            if feedback.isCanceled():
                break
            feedback.setProgress(50 + int(i / n_groups * 50))

            points = data["points"]
            n_pts = len(points)

            # Build geometry depending on point count
            if n_pts == 1:
                geom = QgsGeometry.fromPointXY(points[0]).buffer(5, 8)
                feedback.pushInfo(
                    self.tr(
                        f"  Gruppo '{group_val}': 1 solo punto "
                        f"-> buffer circolare (r=5)"
                    )
                )
            elif n_pts == 2:
                geom = QgsGeometry.fromPolylineXY(points).buffer(5, 8)
                feedback.pushInfo(
                    self.tr(
                        f"  Gruppo '{group_val}': 2 punti "
                        f"-> buffer lineare (r=5)"
                    )
                )
            else:
                mp = QgsGeometry.fromMultiPointXY(points)
                if use_concave:
                    geom = mp.concaveHull(concave_ratio)
                else:
                    geom = mp.convexHull()

                # Fallback: collinear points -> LineString -> buffer
                if geom.type() != QgsWkbTypes.PolygonGeometry:
                    geom = geom.buffer(1, 8)
                    feedback.pushInfo(
                        self.tr(
                            f"  Gruppo '{group_val}': punti collineari "
                            f"-> buffer applicato"
                        )
                    )

            # Reproject polygon if needed
            if transform and geom:
                geom.transform(transform)

            # Build attributes (shared by polygon and its points)
            attrs = [group_val]
            for idx in attr_field_indices:
                attrs.append(data["attrs"].get(idx))
            attrs.append(hull_label)
            attrs.append(n_pts)

            # Create polygon feature
            feat_out = QgsFeature(poly_fields)
            feat_out.setGeometry(geom)
            feat_out.setAttributes(attrs)
            poly_sink.addFeature(feat_out, QgsFeatureSink.FastInsert)

            # Write individual points with same attributes
            for pt in points:
                point_geom = QgsGeometry.fromPointXY(pt)
                if transform:
                    point_geom.transform(transform)
                feat_pt = QgsFeature(point_fields)
                feat_pt.setGeometry(point_geom)
                feat_pt.setAttributes(attrs)
                point_sink.addFeature(feat_pt, QgsFeatureSink.FastInsert)
                point_count += 1

        feedback.pushInfo(
            self.tr(f"Completato: {n_groups} poligoni e {point_count} punti creati.")
        )
        return {
            self.OUTPUT_POLYGONS: poly_dest_id,
            self.OUTPUT_POINTS: point_dest_id,
        }
