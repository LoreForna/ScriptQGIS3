from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsFeature, QgsGeometry, QgsWkbTypes,
    QgsFields, QgsField, QgsPoint, QgsPointXY,
)
from PyQt5.QtCore import QVariant
import numpy as np


class BestFitLine(QgsProcessingAlgorithm):

    INPUT         = 'INPUT'
    OUTPUT_LINE   = 'OUTPUT_LINE'
    OUTPUT_POINTS = 'OUTPUT_POINTS'
    EXTENSION     = 'EXTENSION'
    OUTPUT_MODE   = 'OUTPUT_MODE'

    MODE_OPTIONS  = ['2D', '3D']

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, 'Layer di punti (2D o 3D)',
            [QgsProcessing.TypeVectorPoint]))

        self.addParameter(QgsProcessingParameterNumber(
            self.EXTENSION,
            'Estensione della retta (0 = automatica dai punti)',
            defaultValue=0.0,
            minValue=0.0,
            type=QgsProcessingParameterNumber.Double))

        self.addParameter(QgsProcessingParameterEnum(
            self.OUTPUT_MODE, 'Tipo di output geometria',
            options=self.MODE_OPTIONS,
            defaultValue=0))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_LINE, 'Retta best-fit',
            QgsProcessing.TypeVectorLine))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_POINTS, 'Punti con residui',
            QgsProcessing.TypeVectorPoint))

    def shortHelpString(self):
        return """

<p>Calcola la retta di regressione che meglio approssima una nuvola di punti 2D o 3D, minimizzando le distanze ortogonali di tutti i punti dalla retta (<b>Total Least Squares</b>, anche detto <i>orthogonal regression</i>).</p>

<h4>Metodo di calcolo</h4>
<p>Il calcolo si basa su <b>PCA (Principal Component Analysis)</b> tramite decomposizione SVD (Singular Value Decomposition) della matrice dei punti centrati sul loro centroide.</p>
<ol>
  <li>Si calcola il <b>centroide</b> della nuvola di punti.</li>
  <li>Si <b>centra</b> la nuvola sottraendo il centroide a ogni punto.</li>
  <li>Si applica la <b>SVD</b>: il primo autovettore (direzione di massima varianza) è la direzione della retta best-fit.</li>
  <li>Ogni punto viene <b>proiettato ortogonalmente</b> sulla retta tramite il prodotto scalare con il vettore direzione.</li>
  <li>Il <b>residuo</b> di ogni punto è la distanza euclidea tra il punto originale e la sua proiezione.</li>
</ol>

<p>A differenza della regressione OLS classica (che minimizza i residui su un solo asse), questo metodo minimizza le distanze ortogonali nello spazio 3D ed è quindi corretto quando nessuna delle tre coordinate è la variabile "dipendente".</p>

<p><b>Estensione automatica (t_min / t_max)</b></p>
<p>Con estensione 0 gli estremi del segmento corrispondono alle proiezioni dei punti con valore minimo e massimo lungo la direzione della retta (<b>t_min</b> e <b>t_max</b>). Questo garantisce che tutti i punti rientrino nel segmento indipendentemente dall'ordine in cui sono stati misurati, anche quando alcuni punti proiettano oltre i punti di inizio e fine della serie.</p>

<h4>Parametri</h4>
<ul>
  <li><b>Layer di punti</b>: layer vettoriale di punti, con o senza coordinata Z. Se è attiva una selezione, vengono usati solo i punti selezionati.</li>
  <li><b>Estensione della retta</b>: lunghezza del segmento di output in unità mappa. Con valore <b>0</b> (default) l'estensione è automatica: il segmento va dalla proiezione del punto con t minimo a quella del punto con t massimo, coprendo tutti i punti. Con valore manuale il segmento è centrato sul centroide.</li>
  <li><b>Tipo di output geometria</b>:
    <ul>
      <li><i>2D</i>: la coordinata Z viene ignorata; il calcolo e l'output sono nel piano XY. Valore di default.</li>
      <li><i>3D</i>: il calcolo utilizza le coordinate XYZ e l'output è una geometria 3D (LineStringZ). Se i punti non hanno coordinata Z reale viene usato Z=0 come fallback.</li>
    </ul>
  </li>
</ul>

<h4>Output</h4>
<h4>Retta best-fit</h4>
<p>Segmento (LineString o LineStringZ) con i seguenti attributi:</p>
<ul>
  <li><b>centroid_x/y/z</b>: coordinate del centroide della nuvola di punti.</li>
  <li><b>dir_x/y/z</b>: componenti del vettore unitario che definisce la direzione della retta.</li>
  <li><b>rmse</b>: errore quadratico medio dei residui ortogonali (Root Mean Square Error).</li>
  <li><b>extension</b>: lunghezza effettiva del segmento in unità mappa.</li>
  <li><b>n_points</b>: numero di punti usati nel calcolo.</li>
  <li><b>mode</b>: modalità usata (<i>2D</i> o <i>3D</i>).</li>
</ul>

<h4>Punti con residui</h4>
<p>Copia del layer di input con i seguenti attributi aggiuntivi:</p>
<ul>
  <li><b>fid</b>: identificatore del feature nel layer originale; permette di ricongiungersi agli attributi sorgente tramite join.</li>
  <li><b>residual</b>: distanza euclidea (in unità mappa) tra il punto originale e la sua proiezione ortogonale sulla retta. Misura quanto ogni punto si discosta dalla retta best-fit.</li>
  <li><b>t_param</b>: posizione con segno del piede della perpendicolare lungo la retta, misurata dal centroide. Valori negativi e positivi indicano i due lati del centroide; utile per grafici di dispersione o per verificare l'ordine spaziale dei punti lungo la retta.</li>
  <li><b>proj_x/y/z</b>: coordinate del <i>piede della perpendicolare</i>, cioè il punto sulla retta più vicino a ciascun punto originale. Rappresenta la proiezione ortogonale del punto sulla retta.</li>
</ul>
"""

    def processAlgorithm(self, parameters, context, feedback):
        source    = self.parameterAsSource(parameters, self.INPUT, context)
        extension = self.parameterAsDouble(parameters, self.EXTENSION, context)
        mode_idx  = self.parameterAsEnum(parameters, self.OUTPUT_MODE, context)

        # ── 1. Raccolta coordinate ──────────────────────────────────────────────
        # Nota: se l'utente ha una selezione attiva, QgsProcessingParameterFeatureSource
        # passa automaticamente solo i feature selezionati — l'extent sarà corretto.
        ids, coords = [], []
        detected_z  = False

        for feat in source.getFeatures():
            geom = feat.geometry()
            pt   = geom.constGet()
            feat_has_z = (
                QgsWkbTypes.hasZ(geom.wkbType()) and
                pt.z() == pt.z()  # esclude NaN
            )
            z = pt.z() if feat_has_z else 0.0
            if feat_has_z and z != 0.0:
                detected_z = True
            ids.append(feat.id())
            coords.append([pt.x(), pt.y(), z])

        if len(coords) < 2:
            raise Exception("Servono almeno 2 punti.")

        coords = np.array(coords, dtype=float)

        # ── 2. Modalità output ─────────────────────────────────────────────────
        if mode_idx == 0:
            use_z = False
            feedback.pushInfo("Modalita: output 2D.")
        else:
            use_z = True
            if not detected_z:
                feedback.pushWarning(
                    "Forzato 3D ma i punti non hanno Z reale: Z=0 usato come fallback.")
            feedback.pushInfo("Modalita: output 3D.")

        # ── 3. PCA / SVD: best-fit line ────────────────────────────────────────
        centroid  = coords.mean(axis=0)
        centered  = coords - centroid
        _, _, Vt  = np.linalg.svd(centered)
        direction = Vt[0].copy()

        if not use_z:
            direction[2] = 0.0
            n = np.linalg.norm(direction)
            if n > 1e-10:
                direction /= n

        # ── 4. Residui ─────────────────────────────────────────────────────────
        t_vals    = centered @ direction
        proj      = centroid + np.outer(t_vals, direction)
        diffs     = coords - proj
        residuals = np.linalg.norm(diffs, axis=1)
        rmse      = float(np.sqrt((residuals ** 2).mean()))

        feedback.pushInfo(f"Centroide:     {centroid.round(3)}")
        feedback.pushInfo(f"Direzione:     {direction.round(4)}")
        feedback.pushInfo(f"Punti usati:   {len(coords)}")
        feedback.pushInfo(f"Residuo medio: {residuals.mean():.4f}")
        feedback.pushInfo(f"Residuo max:   {residuals.max():.4f}")
        feedback.pushInfo(f"RMSE:          {rmse:.4f}")

        # ── 5. Calcolo estremi della retta ─────────────────────────────────────
        if extension == 0.0:
            # Gli estremi corrispondono alle proiezioni dei punti con t_min
            # e t_max — i punti angolarmente piu estremi lungo la retta.
            # Questo garantisce che TUTTI i punti rientrino nel segmento,
            # indipendentemente dall ordine in cui sono stati misurati.
            # Analogo al minimum enclosing arc per il cerchio.
            p1 = centroid + direction * t_vals.min()
            p2 = centroid + direction * t_vals.max()
            extension = float(t_vals.max() - t_vals.min())
            feedback.pushInfo(
                f"Estensione automatica: {extension:.4f} unita mappa "
                f"(da t={t_vals.min():.4f} a t={t_vals.max():.4f}).")
        else:
            # Estensione manuale: centrata sul centroide
            p1 = centroid - direction * extension / 2
            p2 = centroid + direction * extension / 2
            feedback.pushInfo(f"Estensione manuale: {extension:.4f} unità mappa.")

        # ── 6. Tipi geometria output ───────────────────────────────────────────
        geom_type_line  = QgsWkbTypes.LineStringZ if use_z else QgsWkbTypes.LineString
        geom_type_point = QgsWkbTypes.PointZ      if use_z else QgsWkbTypes.Point

        # ── 7. Output retta ────────────────────────────────────────────────────
        fields_line = QgsFields()
        for name in ('centroid_x', 'centroid_y', 'centroid_z',
                     'dir_x', 'dir_y', 'dir_z', 'rmse', 'extension'):
            fields_line.append(QgsField(name, QVariant.Double))
        fields_line.append(QgsField('n_points', QVariant.Int))
        fields_line.append(QgsField('mode',     QVariant.String))

        (sink_line, dest_line) = self.parameterAsSink(
            parameters, self.OUTPUT_LINE, context,
            fields_line, geom_type_line, source.sourceCrs())

        f_line = QgsFeature(fields_line)
        if use_z:
            geom_line = QgsGeometry.fromPolyline([
                QgsPoint(p1[0], p1[1], p1[2]),
                QgsPoint(p2[0], p2[1], p2[2])
            ])
        else:
            geom_line = QgsGeometry.fromPolylineXY([
                QgsPointXY(p1[0], p1[1]),
                QgsPointXY(p2[0], p2[1])
            ])

        f_line.setGeometry(geom_line)
        f_line.setAttributes([
            float(centroid[0]), float(centroid[1]), float(centroid[2]),
            float(direction[0]), float(direction[1]), float(direction[2]),
            rmse, extension, len(coords), '3D' if use_z else '2D'
        ])
        sink_line.addFeature(f_line)

        # ── 8. Output punti con residui ────────────────────────────────────────
        fields_pts = QgsFields()
        fields_pts.append(QgsField('fid',      QVariant.Int))
        fields_pts.append(QgsField('residual', QVariant.Double))
        fields_pts.append(QgsField('t_param',  QVariant.Double))
        fields_pts.append(QgsField('proj_x',   QVariant.Double))
        fields_pts.append(QgsField('proj_y',   QVariant.Double))
        fields_pts.append(QgsField('proj_z',   QVariant.Double))

        (sink_pts, dest_pts) = self.parameterAsSink(
            parameters, self.OUTPUT_POINTS, context,
            fields_pts, geom_type_point, source.sourceCrs())

        for i, (pt, res, t, pj) in enumerate(zip(coords, residuals, t_vals, proj)):
            f = QgsFeature(fields_pts)
            if use_z:
                f.setGeometry(QgsGeometry.fromPoint(
                    QgsPoint(pt[0], pt[1], pt[2])))
            else:
                f.setGeometry(QgsGeometry.fromPointXY(
                    QgsPointXY(pt[0], pt[1])))
            f.setAttributes([
                int(ids[i]), float(res), float(t),
                float(pj[0]), float(pj[1]), float(pj[2])
            ])
            sink_pts.addFeature(f)

        return {self.OUTPUT_LINE: dest_line, self.OUTPUT_POINTS: dest_pts}

    def name(self):           return 'bestfitline'
    def displayName(self):    return 'Best-fit Line 2D/3D (PCA)'
    def group(self):          return 'Best-fit'
    def groupId(self):        return 'best-fit'
    def createInstance(self): return BestFitLine()
