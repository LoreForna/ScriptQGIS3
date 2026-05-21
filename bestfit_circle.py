from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsFeature, QgsGeometry, QgsWkbTypes,
    QgsFields, QgsField, QgsPointXY,
)
from PyQt5.QtCore import QVariant
import numpy as np


class BestFitCircle(QgsProcessingAlgorithm):

    INPUT         = 'INPUT'
    OUTPUT_CIRCLE = 'OUTPUT_CIRCLE'
    OUTPUT_POINTS = 'OUTPUT_POINTS'
    OUTPUT_TYPE   = 'OUTPUT_TYPE'
    N_SEGMENTS    = 'N_SEGMENTS'

    TYPE_OPTIONS = ['Cerchio completo', "Arco (minimum enclosing arc)"]

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, 'Layer di punti (2D o 3D)',
            [QgsProcessing.TypeVectorPoint]))

        self.addParameter(QgsProcessingParameterEnum(
            self.OUTPUT_TYPE, 'Tipo di output',
            options=self.TYPE_OPTIONS,
            defaultValue=0))

        self.addParameter(QgsProcessingParameterNumber(
            self.N_SEGMENTS,
            'Numero di segmenti (risoluzione della polilinea)',
            defaultValue=72,
            minValue=8,
            type=QgsProcessingParameterNumber.Integer))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_CIRCLE, 'Cerchio/Arco best-fit',
            QgsProcessing.TypeVectorLine))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_POINTS, 'Punti con residui',
            QgsProcessing.TypeVectorPoint))

    def shortHelpString(self):
        return """

<p>Calcola il cerchio o l'arco di cerchio che meglio approssima una nuvola di punti, lavorando esclusivamente nel piano XY. La coordinata Z viene ignorata. Con 3 punti il risultato coincide con il cerchio esatto, equivalente alla funzione <i>Disegna cerchio da 3 punti</i> di QGIS; con più punti viene applicata una regressione ai minimi quadrati.</p>

<h4>Metodo di calcolo</h4>
<p>Si linearizza l'equazione del cerchio: x²+y² = A·x + B·y + C, dove A=2cx, B=2cy e C=r²−cx²−cy². I punti vengono prima <b>centrati e normalizzati</b> per garantire stabilità numerica con coordinate di grandi valori (es. sistemi metrici UTM in cui le coordinate superano il milione). Il sistema lineare viene risolto con <b>numpy lstsq</b>, da cui si ricavano il centro (cx, cy) e il raggio r.</p>

<p><b>Arco di cerchio (Minimum Enclosing Arc)</b></p>
<p>L'arco viene calcolato come il <b>minimum enclosing arc</b>: l'arco più corto che contiene le proiezioni di tutti i punti sul cerchio. Il metodo è analogo all'uso di t_min/t_max per la retta: gli angoli di tutti i punti vengono ordinati, si individua il gap angolare massimo tra punti consecutivi, e l'arco copre tutto il resto. Questo funziona correttamente anche quando i punti non sono stati misurati in ordine sequenziale lungo l'arco.</p>

<h4>Parametri</h4>
<ul>
  <li><b>Layer di punti</b>: layer vettoriale di punti 2D o 3D. Se è attiva una selezione, vengono usati solo i punti selezionati. La coordinata Z viene ignorata.</li>
  <li><b>Tipo di output</b>:
    <ul>
      <li><i>Cerchio completo</i>: polilinea chiusa (360°) che rappresenta l'intero cerchio best-fit.</li>
      <li><i>Arco (minimum enclosing arc)</i>: arco più corto che contiene le proiezioni radiali di tutti i punti sul cerchio.</li>
    </ul>
  </li>
  <li><b>Numero di segmenti</b>: risoluzione della polilinea di output. 72 segmenti = un vertice ogni 5°; 360 segmenti = un vertice ogni 1°. Per il cerchio completo si consiglia un valore elevato; per l'arco è sufficiente un valore proporzionale all'ampiezza angolare.</li>
</ul>

<h4>Output</h4>
<h4>Cerchio/Arco best-fit</h4>
<p>Polilinea 2D (LineString) con i seguenti attributi:</p>
<ul>
  <li><b>center_x / center_y</b>: coordinate del centro del cerchio nel piano XY.</li>
  <li><b>radius</b>: raggio in unità mappa.</li>
  <li><b>rmse</b>: errore quadratico medio dei residui radiali (Root Mean Square Error). Con esattamente 3 punti non collineari è sempre 0: tre punti non allineati definiscono una e una sola circonferenza su cui cadono esattamente, quindi la distanza radiale di ciascuno dalla circonferenza è nulla. Con 3 punti collineari il fit fallisce (raggio infinito). Con più di 3 punti l'rmse è in genere maggiore di 0 e misura la qualità del fit: un valore basso indica che i punti sono ben distribuiti su una circonferenza.</li>
  <li><b>arc_start_deg</b>: angolo di inizio dell'arco in gradi (0–360).</li>
  <li><b>arc_end_deg</b>: angolo di fine dell'arco in gradi (0–360).</li>
  <li><b>arc_span_deg</b>: ampiezza angolare totale dell'arco in gradi.</li>
  <li><b>n_points</b>: numero di punti usati nel calcolo.</li>
  <li><b>output_type</b>: tipo di output prodotto (<i>circle</i> o <i>arc</i>).</li>
</ul>

<h4>Punti con residui</h4>
<p>Copia del layer di input con i seguenti attributi aggiuntivi:</p>
<ul>
  <li><b>fid</b>: identificatore del feature nel layer originale; permette di ricongiungersi agli attributi sorgente tramite join.</li>
  <li><b>residual</b>: distanza radiale (in unità mappa) tra il punto e il cerchio best-fit, calcolata come | distanza_dal_centro − raggio |. Misura quanto ogni punto si discosta dal cerchio ideale.</li>
  <li><b>angle_deg</b>: angolo del punto rispetto al centro in gradi (0–360). Permette di posizionare ogni punto sulla circonferenza e di fare grafici di dispersione angolare.</li>
  <li><b>proj_x / proj_y</b>: coordinate del punto più vicino sul cerchio, cioè la proiezione radiale del punto sulla circonferenza best-fit.</li>
</ul>
"""

    @staticmethod
    def _fit_circle_2d(xy):
        """
        Fit algebrico del cerchio in XY con centramento e normalizzazione.
        Con 3 punti restituisce il cerchio esatto.
        """
        x_mean = float(xy[:, 0].mean())
        y_mean = float(xy[:, 1].mean())
        scale  = float(np.sqrt(((xy[:, 0] - x_mean)**2 +
                                (xy[:, 1] - y_mean)**2).mean()))
        if scale < 1e-10:
            raise Exception("I punti sono tutti coincidenti o troppo vicini.")

        xn = (xy[:, 0] - x_mean) / scale
        yn = (xy[:, 1] - y_mean) / scale
        rhs   = xn**2 + yn**2
        A_mat = np.column_stack([xn, yn, np.ones(len(xn))])
        res, _, _, _ = np.linalg.lstsq(A_mat, rhs, rcond=None)

        cx_n = float(res[0]) / 2.0
        cy_n = float(res[1]) / 2.0
        r2_n = float(res[2]) + cx_n**2 + cy_n**2
        if r2_n <= 0:
            raise Exception(
                "I punti sono collineari o quasi: impossibile fittare un cerchio.")

        cx = cx_n * scale + x_mean
        cy = cy_n * scale + y_mean
        r  = float(np.sqrt(r2_n)) * scale
        return cx, cy, r

    @staticmethod
    def _minimum_enclosing_arc(angles_raw):
        """
        Calcola il minimum enclosing arc: l'arco piu' corto che contiene
        tutti i punti proiettati sul cerchio.

        Analogo a t_min/t_max per la retta: individua i due punti angolarmente
        estremi trovando il gap angolare massimo tra punti consecutivi
        (ordinati). L'arco e' tutto cio' che NON e' nel gap massimo.

        Funziona correttamente anche se i punti non sono in ordine sequenziale
        lungo l'arco.

        Restituisce (theta_start, theta_end) con theta_end > theta_start.
        """
        # Normalizza in [0, 2*pi] e ordina
        angles_norm = angles_raw % (2.0 * np.pi)
        ang_sorted  = np.sort(angles_norm)

        # Gap tra angoli consecutivi + gap wrap-around
        gaps     = np.diff(ang_sorted)
        wrap_gap = ang_sorted[0] + 2.0 * np.pi - ang_sorted[-1]
        all_gaps = np.append(gaps, wrap_gap)
        k        = int(np.argmax(all_gaps))

        # L'arco inizia subito dopo il gap massimo e finisce subito prima
        if k == len(ang_sorted) - 1:
            # Il gap massimo e' il wrap-around: arco dal min al max angolo
            theta_start = float(ang_sorted[0])
            theta_end   = float(ang_sorted[-1])
        else:
            # Il gap e' interno: arco da ang_sorted[k+1] a ang_sorted[k]+2pi
            theta_start = float(ang_sorted[k + 1])
            theta_end   = float(ang_sorted[k]) + 2.0 * np.pi

        return theta_start, theta_end

    def processAlgorithm(self, parameters, context, feedback):
        source     = self.parameterAsSource(parameters, self.INPUT, context)
        out_type   = self.parameterAsEnum(parameters, self.OUTPUT_TYPE, context)
        n_segments = self.parameterAsInt(parameters, self.N_SEGMENTS, context)

        # 1. Raccolta coordinate XY (Z ignorata)
        ids, xy = [], []
        for feat in source.getFeatures():
            geom = feat.geometry()
            pt   = geom.constGet()
            ids.append(feat.id())
            xy.append([float(pt.x()), float(pt.y())])

        if len(xy) < 3:
            raise Exception("Servono almeno 3 punti per fittare un cerchio.")

        xy = np.array(xy, dtype=float)
        feedback.pushInfo(f"Punti letti: {len(xy)}")

        # 2. Fit cerchio in XY
        cx, cy, radius = self._fit_circle_2d(xy)
        feedback.pushInfo(f"Centro:  ({cx:.6f}, {cy:.6f})")
        feedback.pushInfo(f"Raggio:  {radius:.6f} unita' mappa")

        # 3. Angoli e distanze di ogni punto rispetto al centro
        diff             = xy - np.array([cx, cy])
        angles_raw       = np.arctan2(diff[:, 1], diff[:, 0])
        dist_from_center = np.linalg.norm(diff, axis=1)

        # 4. Residui radiali
        residuals = np.abs(dist_from_center - radius)
        rmse      = float(np.sqrt((residuals**2).mean()))
        feedback.pushInfo(f"Residuo medio: {residuals.mean():.6f}")
        feedback.pushInfo(f"Residuo max:   {residuals.max():.6f}")
        feedback.pushInfo(f"RMSE:          {rmse:.6f}")

        # 5. Calcolo intervallo angolare
        if out_type == 0:
            # Cerchio completo: parte dall'angolo del primo punto, gira 360
            theta_start = float(angles_raw[0])
            theta_end   = theta_start + 2.0 * np.pi
            arc_span    = 360.0
            feedback.pushInfo("Output: cerchio completo.")
        else:
            # Arco: minimum enclosing arc (analogo a t_min/t_max per la retta)
            theta_start, theta_end = self._minimum_enclosing_arc(angles_raw)
            arc_span = float(np.degrees(theta_end - theta_start))
            feedback.pushInfo(
                f"Output: arco da {np.degrees(theta_start):.2f} gradi "
                f"a {np.degrees(theta_end):.2f} gradi "
                f"(ampiezza {arc_span:.2f} gradi).")

        # 6. Generazione polilinea
        thetas   = np.linspace(theta_start, theta_end, n_segments + 1)
        poly_pts = [
            QgsPointXY(float(cx + radius * np.cos(th)),
                       float(cy + radius * np.sin(th)))
            for th in thetas
        ]

        # Per l'arco: forza il primo e l'ultimo vertice sulle proiezioni
        # esatte dei punti estremi sul cerchio (elimina drift da linspace)
        if out_type == 1:
            poly_pts[0] = QgsPointXY(
                float(cx + radius * np.cos(theta_start)),
                float(cy + radius * np.sin(theta_start)))
            poly_pts[-1] = QgsPointXY(
                float(cx + radius * np.cos(theta_end)),
                float(cy + radius * np.sin(theta_end)))

        # 7. Output cerchio/arco
        fields_circ = QgsFields()
        for name in ('center_x', 'center_y', 'radius', 'rmse',
                     'arc_start_deg', 'arc_end_deg', 'arc_span_deg'):
            fields_circ.append(QgsField(name, QVariant.Double))
        fields_circ.append(QgsField('n_points',    QVariant.Int))
        fields_circ.append(QgsField('output_type', QVariant.String))

        (sink_circ, dest_circ) = self.parameterAsSink(
            parameters, self.OUTPUT_CIRCLE, context,
            fields_circ, QgsWkbTypes.LineString, source.sourceCrs())

        if sink_circ is None:
            raise Exception("Impossibile creare il layer di output per il cerchio/arco.")

        f_circ = QgsFeature(fields_circ)
        f_circ.setGeometry(QgsGeometry.fromPolylineXY(poly_pts))
        f_circ.setAttributes([
            float(cx), float(cy),
            float(radius), float(rmse),
            float(np.degrees(theta_start) % 360),
            float(np.degrees(theta_end)   % 360),
            float(arc_span),
            int(len(xy)),
            'circle' if out_type == 0 else 'arc'
        ])
        sink_circ.addFeature(f_circ)
        feedback.pushInfo("Feature cerchio/arco aggiunta.")

        # 8. Output punti con residui
        fields_pts = QgsFields()
        fields_pts.append(QgsField('fid',       QVariant.Int))
        fields_pts.append(QgsField('residual',  QVariant.Double))
        fields_pts.append(QgsField('angle_deg', QVariant.Double))
        fields_pts.append(QgsField('proj_x',    QVariant.Double))
        fields_pts.append(QgsField('proj_y',    QVariant.Double))

        (sink_pts, dest_pts) = self.parameterAsSink(
            parameters, self.OUTPUT_POINTS, context,
            fields_pts, QgsWkbTypes.Point, source.sourceCrs())

        if sink_pts is None:
            raise Exception("Impossibile creare il layer di output per i punti.")

        for i in range(len(xy)):
            d = float(dist_from_center[i])
            if d > 1e-10:
                proj_x = float(cx + radius * diff[i, 0] / d)
                proj_y = float(cy + radius * diff[i, 1] / d)
            else:
                proj_x, proj_y = float(cx + radius), float(cy)

            f = QgsFeature(fields_pts)
            f.setGeometry(QgsGeometry.fromPointXY(
                QgsPointXY(float(xy[i, 0]), float(xy[i, 1]))))
            f.setAttributes([
                int(ids[i]),
                float(residuals[i]),
                float(np.degrees(angles_raw[i]) % 360),
                proj_x, proj_y
            ])
            sink_pts.addFeature(f)

        feedback.pushInfo(f"Punti con residui aggiunti: {len(xy)}")
        return {self.OUTPUT_CIRCLE: dest_circ, self.OUTPUT_POINTS: dest_pts}

    def name(self):           return 'bestfitcircle'
    def displayName(self):    return 'Best-fit Circle/Arc 2D'
    def group(self):          return 'Best-fit'
    def groupId(self):        return 'best-fit'
    def createInstance(self): return BestFitCircle()
