"""
Griglia Orientata — QGIS Processing Script
============================================
Genera una griglia di linee orientata a partire da due punti indicati
sulla mappa (o da P1 + azimut diretto), estesa entro un'area definita
dall'utente (rettangolo o poligono).

Miglioramenti topografici:
1. Validazione CRS (deve essere proiettato/metrico)
2. Coerenza CRS tra punti, extent e poligono di clip (riproiezione)
3. Coordinate locali (u, v) nella tabella attributi
4. Layer opzionale di punti alle intersezioni della griglia
5. Azimut dell'asse, geografico e di griglia, in convenzione N\u00b1E/W
   normalizzata a [0\u00b0, 180\u00b0) per indipendenza dall'ordine P1/P2
6. Direzione consistente delle linee dopo il clip
7. Fattore di scala combinato (opzionale) per correzione
   distanza al suolo vs distanza sulla carta

Integrazioni avanzate:
A. Clip su poligono arbitrario (alternativa al rettangolo)
B. Etichette leggibili per le linee (P+3, P0, T-2, ...)
C. Definizione della griglia tramite azimut diretto (alternativa a P2)
D. Gestione MultiLineString dopo il clip (linee esplose in parti)
E. Barra di progresso proporzionale al numero di linee/punti
F. Layer opzionale dei punti di riferimento P1 e P2
"""

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterPoint,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterCrs,
    QgsProcessingParameterFeatureSink,
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
    QgsWkbTypes,
    QgsProject,
)
import math


class OrientedGridAlgorithm(QgsProcessingAlgorithm):

    POINT1 = 'POINT1'
    POINT2 = 'POINT2'
    USE_AZIMUTH = 'USE_AZIMUTH'
    AZIMUTH_INPUT = 'AZIMUTH_INPUT'
    SPACING_PAR = 'SPACING_PAR'
    SPACING_PERP = 'SPACING_PERP'
    GRID_TYPE = 'GRID_TYPE'
    EXTENT = 'EXTENT'
    CLIP_POLYGON = 'CLIP_POLYGON'
    CRS = 'CRS'
    SCALE_FACTOR_ENABLED = 'SCALE_FACTOR_ENABLED'
    SCALE_FACTOR = 'SCALE_FACTOR'
    OUTPUT_LINES = 'OUTPUT_LINES'
    OUTPUT_POINTS = 'OUTPUT_POINTS'
    OUTPUT_AXIS = 'OUTPUT_AXIS'
    OUTPUT_REF = 'OUTPUT_REF'

    GRID_OPTIONS = [
        'Solo parallele (all\'asse P1-P2)',
        'Solo perpendicolari (all\'asse P1-P2)',
        'Griglia completa (parallele + perpendicolari)',
    ]

    def name(self):
        return 'oriented_grid'

    def displayName(self):
        return 'Griglia orientata da due punti'

    def group(self):
        return 'Griglie e reticoli'

    def groupId(self):
        return 'grids'

    def shortHelpString(self):
        return (
            'Genera una griglia di linee orientata secondo la direzione '
            'definita da due punti sulla mappa (o da P1 + un azimut '
            'diretto), estesa entro un\'area rettangolare o entro un '
            'poligono di clip.\n\n'

            'PARAMETRI DI INPUT\n'
            '\u2022 Punto 1: origine della griglia (sempre richiesto)\n'
            '\u2022 Punto 2: definisce la direzione dell\'asse principale; '
            'opzionale se si usa l\'azimut diretto\n'
            '\u2022 Usa azimut diretto: se attivo, P2 viene ignorato e la '
            'direzione e\' presa dall\'azimut numerico (es. da un rilievo '
            'precedente o da una pubblicazione)\n'
            '\u2022 Azimut dell\'asse: valore in gradi rispetto al Nord '
            'griglia (0\u2013360, senso orario)\n'
            '\u2022 Spaziatura parallele/perpendicolari in metri\n'
            '\u2022 Estensione rettangolare: area di copertura (opzionale '
            'se si fornisce un poligono di clip; altrimenti obbligatoria)\n'
            '\u2022 Poligono di clip (opzionale): se fornito, le linee '
            'vengono ritagliate sul poligono invece che sul rettangolo. '
            'Tutte le feature del layer poligonale vengono unite. Se '
            'sia extent sia poligono sono forniti, prevale il poligono.\n\n'

            'TIPO DI GRIGLIA\n'
            '\u2022 Parallele: linee parallele all\'asse P1\u2013P2\n'
            '\u2022 Perpendicolari: linee ortogonali all\'asse P1\u2013P2\n'
            '\u2022 Griglia completa: entrambe le famiglie di linee\n\n'

            'SISTEMA DI COORDINATE LOCALE\n'
            'Lo script definisce un sistema locale con origine in P1:\n'
            '\u2022 Asse u: direzione P1\u2192P2 (asse principale)\n'
            '\u2022 Asse v: perpendicolare a u, 90\u00b0 antiorario nel piano '
            'cartografico (sinistra guardando da P1 verso P2)\n'
            'Le coordinate (coord_u, coord_v) di ogni linea indicano la '
            'posizione nel sistema locale.\n\n'

            'CAMPI ATTRIBUTI (linee)\n'
            '\u2022 tipo: "parallela" o "perpendicolare"\n'
            '\u2022 indice: numero progressivo della linea (con segno)\n'
            '\u2022 etichetta: nome leggibile (P0 = asse, P+1, P-2, T0 = '
            'perpendicolare passante per P1, T+1, T-2, ecc.). '
            'P = parallela, T = trasversale (perpendicolare).\n'
            '\u2022 dist_asse: distanza dall\'asse o progressiva lungo '
            'l\'asse (m, nel sistema cartografico)\n'
            '\u2022 coord_u, coord_v: coordinate nel sistema locale\n'
            '\u2022 azimut_griglia: azimut della linea rispetto al nord '
            'griglia (es. 33.62301\u00b0E), in [0\u00b0, 90\u00b0] \u00b1 E/W\n'
            '\u2022 azimut_geo: azimut della linea rispetto al nord '
            'geografico (es. 33.12450\u00b0E)\n'
            '\u2022 convergenza: convergenza dei meridiani in P1 '
            '(es. 0.49851\u00b0W)\n'
            'Se attivo il fattore di scala, vengono aggiunti i campi '
            'dist_suolo e fattore_scala.\n\n'

            'CAMPI ATTRIBUTI (punti intersezione)\n'
            '\u2022 etichetta: combinazione delle etichette di parallela e '
            'trasversale (es. "P+3_T-2"), pronta per l\'etichettatura\n'
            '\u2022 coord_u, coord_v: coordinate locali del nodo\n'
            '\u2022 azimut e convergenza: come per le linee\n\n'

            'CAMPI ATTRIBUTI (punti di riferimento P1/P2)\n'
            'Layer opzionale che documenta la griglia: posizione, '
            'distanza P1\u2013P2, azimut, convergenza, spaziature, e una '
            'nota sull\'origine della direzione (P2 reale o azimut diretto). '
            'Utile per archiviazione e riposizionamento futuro.\n\n'

            'FORMATO AZIMUT E CONVERGENZA\n'
            'L\'azimut e\' misurato dal Nord in senso orario verso Est. '
            'I valori sono normalizzati in [0\u00b0, 180\u00b0) per essere '
            'indipendenti dal verso della linea: invertendo P1 e P2 '
            'l\'azimut resta invariato.\n'
            'Notazione:\n'
            '\u2022 XX.XXXXX\u00b0E = linea inclinata X gradi a est del N-S\n'
            '\u2022 XX.XXXXX\u00b0W = linea inclinata X gradi a ovest del N-S\n'
            '\u2022 Valori sempre in [0\u00b0, 90\u00b0]; 0\u00b0E = linea N-S, '
            '90\u00b0E = linea E-W\n'
            'La convergenza segue la convenzione topografica standard '
            '\u03b3 = grid_north \u2212 true_north (positiva in senso orario):\n'
            '\u2022 X.XXXXX\u00b0E = grid nord a est di true nord '
            '(P1 a est del meridiano centrale UTM)\n'
            '\u2022 X.XXXXX\u00b0W = grid nord a ovest di true nord '
            '(P1 a ovest del meridiano centrale UTM)\n\n'

            'CALCOLO AZIMUT\n'
            '\u2022 Azimut griglia: atan2(dE, dN) dal vettore P1\u2192P2 nelle '
            'coordinate proiettate, ridotto modulo 180. Per le '
            'perpendicolari: (azimut parallele + 90\u00b0) mod 180.\n'
            '\u2022 Convergenza dei meridiani (\u03b3): per CRS UTM viene '
            'calcolata con la formula analitica \u03b3 = atan(tan(\u0394\u03bb)\u00b7sin\u03c6) '
            '(precisa al microgrado). Per altri CRS proiettati viene '
            'usato un metodo numerico (riproiezione di un piccolo '
            'spostamento in latitudine).\n'
            '\u2022 Azimut geografico = (azimut griglia + convergenza) '
            'mod 180. Confrontabile tra siti in CRS o fusi diversi.\n\n'

            'FATTORE DI SCALA (opzionale)\n'
            'Se attivato, le spaziature vengono interpretate come distanze '
            'al suolo (ellissoide) e convertite in distanze sulla carta '
            'moltiplicando per il fattore di scala k:\n'
            '  d_carta = d_suolo \u00d7 k\n'
            'Al meridiano centrale UTM k = 0.9996, quindi le distanze '
            'sulla carta sono leggermente piu\' piccole di quelle al suolo. '
            'I campi dist_suolo (= d_carta / k) e fattore_scala vengono '
            'aggiunti alla tabella attributi.\n\n'

            'GESTIONE DELLE LINEE\n'
            '\u2022 Direzione consistente: dopo il ritaglio sull\'estensione '
            'o sul poligono, i vertici di ogni linea vengono riordinati '
            'in modo che il primo vertice abbia sempre la proiezione '
            'minore lungo la direzione della linea.\n'
            '\u2022 Multi-line strings: se il ritaglio produce piu\' parti '
            'separate per una stessa linea (ad esempio attraversando '
            'un poligono concavo o un\'isola), le parti vengono esplose '
            'in feature distinte, ciascuna con stessa etichetta e attributi.\n\n'

            'OUTPUT\n'
            '\u2022 Griglia orientata (linee): layer lineare principale\n'
            '\u2022 Nodi della griglia (punti, opzionale): punti alle '
            'intersezioni con coordinate locali, etichetta P+i_T+j, '
            'azimut e convergenza. Solo in modalita\' griglia completa. '
            'Sono inclusi anche i nodi sul bordo del clip (intersects).\n'
            '\u2022 Punti di riferimento P1 e P2 (opzionale): documenta '
            'la griglia per archiviazione o ripristino futuro. Include '
            'azimut_grid_deg e convergenza_deg numerici per ricostruzione.\n'
            '\u2022 Asse della griglia (linea, opzionale): segmento P1\u2013P2 '
            'come singola feature, utile per visualizzazione e snap.\n\n'

            'CONTROLLI DI SICUREZZA\n'
            '\u2022 CRS di output proiettato (metrico) obbligatorio\n'
            '\u2022 Distanza P1\u2013P2 minima: 1 metro (proteggere da rumore '
            'di click); soglia disattivata con azimut diretto\n'
            '\u2022 Soglia massima linee: 500.000; soglia massima nodi: '
            '1.000.000. Sopra queste soglie lo script si interrompe per '
            'evitare blocchi di QGIS.\n\n'

            'NOTE\n'
            '\u2022 Punti, estensione e poligono di clip vengono riproiettati '
            'automaticamente nel CRS di output se necessario\n'
            '\u2022 La barra di progresso e\' proporzionale al numero totale '
            'di linee e punti generati\n'
            '\u2022 La convergenza e\' calcolata nel solo punto P1; su un '
            'singolo sito la variazione e\' trascurabile'
        )

    def createInstance(self):
        return OrientedGridAlgorithm()

    # ----------------------------------------------------------------
    # Parametri
    # ----------------------------------------------------------------
    def initAlgorithm(self, config=None):

        # --- Punti di riferimento ---
        self.addParameter(QgsProcessingParameterPoint(
            self.POINT1,
            'Punto 1 — Origine della griglia',
        ))
        self.addParameter(QgsProcessingParameterPoint(
            self.POINT2,
            'Punto 2 — Direzione dell\'asse principale (ignorato se '
            'si usa l\'azimut diretto)',
            optional=True,
        ))

        # --- Azimut diretto (alternativa a P2) ---
        self.addParameter(QgsProcessingParameterBoolean(
            self.USE_AZIMUTH,
            'Usa azimut diretto al posto del Punto 2',
            defaultValue=False,
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.AZIMUTH_INPUT,
            'Azimut dell\'asse rispetto al Nord griglia (gradi, '
            '0\u2013360, senso orario)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0,
            minValue=0.0,
            maxValue=360.0,
            optional=True,
        ))

        # --- Tipo di griglia e spaziature ---
        self.addParameter(QgsProcessingParameterEnum(
            self.GRID_TYPE,
            'Tipo di griglia',
            options=self.GRID_OPTIONS,
            defaultValue=2,
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.SPACING_PAR,
            'Spaziatura linee parallele (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=1.0,
            minValue=0.0001,
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.SPACING_PERP,
            'Spaziatura linee perpendicolari (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=1.0,
            minValue=0.0001,
        ))

        # --- Area di copertura ---
        self.addParameter(QgsProcessingParameterExtent(
            self.EXTENT,
            'Estensione rettangolare della griglia (disegna sulla mappa, '
            'opzionale se si fornisce un poligono di clip)',
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.CLIP_POLYGON,
            'Poligono di clip (opzionale, sostituisce l\'estensione '
            'rettangolare per il ritaglio)',
            types=[QgsProcessing.TypeVectorPolygon],
            optional=True,
        ))

        # --- CRS ---
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS,
            'CRS di output',
            defaultValue='ProjectCrs',
        ))

        # --- Fattore di scala ---
        self.addParameter(QgsProcessingParameterBoolean(
            self.SCALE_FACTOR_ENABLED,
            'Applica fattore di scala combinato (distanza al suolo)',
            defaultValue=False,
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.SCALE_FACTOR,
            'Fattore di scala combinato (es. 0.9996 per UTM fuso centrale)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.9996,
            minValue=0.9,
            maxValue=1.1,
            optional=True,
        ))

        # --- Output ---
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_LINES,
            'Griglia orientata (linee)',
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_POINTS,
            'Nodi della griglia (punti)',
            type=QgsProcessing.TypeVectorPoint,
            createByDefault=False,
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_REF,
            'Punti di riferimento P1 e P2',
            type=QgsProcessing.TypeVectorPoint,
            createByDefault=False,
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_AXIS,
            'Asse della griglia (linea P1-P2)',
            type=QgsProcessing.TypeVectorLine,
            createByDefault=False,
            optional=True,
        ))

    # ----------------------------------------------------------------
    # Elaborazione
    # ----------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):

        crs = self.parameterAsCrs(parameters, self.CRS, context)

        # =============================================
        # 1) Validazione CRS: deve essere proiettato
        # =============================================
        if crs.isGeographic():
            raise ValueError(
                'Il CRS di output e\' geografico (gradi). '
                'Selezionare un CRS proiettato/metrico (es. UTM).'
            )

        # =============================================
        # 2) Coerenza CRS: riproiezione punti, extent, poligono
        # =============================================
        project_crs = QgsProject.instance().crs()

        # Trasformazione progetto -> output, definita una sola volta per
        # evitare dipendenze sull'ordine di esecuzione
        if project_crs != crs:
            xform_pts = QgsCoordinateTransform(
                project_crs, crs, QgsProject.instance()
            )
        else:
            xform_pts = None

        def to_output_crs(point_xy):
            """Trasforma un QgsPointXY dal CRS progetto al CRS output."""
            if xform_pts is None:
                return QgsPointXY(point_xy)
            return xform_pts.transform(QgsPointXY(point_xy))

        # --- Punto 1 (sempre richiesto) ---
        pt1_raw = self.parameterAsPoint(parameters, self.POINT1, context)
        pt1 = to_output_crs(pt1_raw)

        # --- Determinazione della direzione: da P2 oppure da azimut ---
        use_azimuth = self.parameterAsBool(
            parameters, self.USE_AZIMUTH, context
        )

        if use_azimuth:
            # Direzione da azimut diretto (gradi da Nord griglia, orario)
            az_input = self.parameterAsDouble(
                parameters, self.AZIMUTH_INPUT, context
            )
            az_rad = math.radians(az_input)
            # In coordinate proiettate (E, N): dE = sin(az), dN = cos(az)
            # P2 fittizio a 1 m di distanza in quella direzione
            pt2 = QgsPointXY(
                pt1.x() + math.sin(az_rad),
                pt1.y() + math.cos(az_rad),
            )
            feedback.pushInfo(
                f'Direzione asse definita da azimut griglia: {az_input:.4f}\u00b0'
            )
        else:
            # Direzione da Punto 2
            # Verifica che P2 sia stato effettivamente fornito (parametro
            # opzionale: parameterAsPoint restituirebbe (0,0) di default)
            if parameters.get(self.POINT2) in (None, ''):
                raise ValueError(
                    'Il Punto 2 e\' richiesto quando non si usa l\'azimut '
                    'diretto. Indicare il Punto 2 sulla mappa oppure '
                    'attivare l\'opzione "Usa azimut diretto".'
                )
            pt2_raw = self.parameterAsPoint(parameters, self.POINT2, context)
            pt2 = to_output_crs(pt2_raw)

        # --- Estensione rettangolare (opzionale) e suo CRS ---
        extent_provided = parameters.get(self.EXTENT) not in (None, '')
        if extent_provided:
            extent_raw = self.parameterAsExtent(
                parameters, self.EXTENT, context
            )
            extent_crs = self.parameterAsExtentCrs(
                parameters, self.EXTENT, context
            )
            if extent_crs.isValid() and extent_crs != crs:
                xform_ext = QgsCoordinateTransform(
                    extent_crs, crs, QgsProject.instance()
                )
                extent = xform_ext.transformBoundingBox(extent_raw)
            else:
                extent = extent_raw
        else:
            extent = None  # potrebbe essere sovrascritto dal poligono

        # --- Poligono di clip (opzionale) ---
        clip_source = self.parameterAsSource(
            parameters, self.CLIP_POLYGON, context
        )
        clip_polygon_geom = None
        if clip_source is not None:
            # Unione di tutte le feature in un unico poligono
            source_crs = clip_source.sourceCrs()
            xform_poly = (
                QgsCoordinateTransform(source_crs, crs, QgsProject.instance())
                if source_crs.isValid() and source_crs != crs else None
            )
            parts = []
            for f in clip_source.getFeatures():
                g = f.geometry()
                if g.isEmpty():
                    continue
                if xform_poly is not None:
                    g = QgsGeometry(g)
                    g.transform(xform_poly)
                parts.append(g)
            if parts:
                clip_polygon_geom = QgsGeometry.unaryUnion(parts)
                # Sovrascrivi l'extent con il bounding box del poligono,
                # cosi' il calcolo delle linee si basa sull'area effettiva
                extent = clip_polygon_geom.boundingBox()
                feedback.pushInfo(
                    'Poligono di clip caricato: il ritaglio usera\' '
                    'il poligono al posto del rettangolo.'
                )

        # Validazione: almeno uno tra extent rettangolare e poligono richiesto
        if extent is None:
            raise ValueError(
                'Indicare un\'estensione rettangolare oppure un poligono '
                'di clip per delimitare l\'area della griglia.'
            )

        # =============================================
        # Parametri rimanenti
        # =============================================
        grid_type = self.parameterAsEnum(parameters, self.GRID_TYPE, context)
        sp_par = self.parameterAsDouble(parameters, self.SPACING_PAR, context)
        sp_perp = self.parameterAsDouble(parameters, self.SPACING_PERP, context)
        use_scale = self.parameterAsBool(parameters, self.SCALE_FACTOR_ENABLED, context)
        scale_factor = self.parameterAsDouble(parameters, self.SCALE_FACTOR, context)

        # =============================================
        # 7) Fattore di scala combinato
        # =============================================
        # La spaziatura richiesta e' "al suolo". Per riportarla sulla carta
        # occorre moltiplicare per il fattore di scala: d_carta = d_suolo * k
        # (k < 1 al meridiano centrale UTM, quindi d_carta < d_suolo)
        if use_scale and scale_factor > 0:
            sp_par_map = sp_par * scale_factor
            sp_perp_map = sp_perp * scale_factor
            feedback.pushInfo(
                f'Fattore di scala combinato: {scale_factor:.6f}\n'
                f'  Spaziatura parallele: {sp_par:.4f} m (suolo) -> {sp_par_map:.4f} m (carta)\n'
                f'  Spaziatura perpendicolari: {sp_perp:.4f} m (suolo) -> {sp_perp_map:.4f} m (carta)'
            )
        else:
            sp_par_map = sp_par
            sp_perp_map = sp_perp

        # =============================================
        # Direzione principale (asse P1 -> P2)
        # =============================================
        dx = pt2.x() - pt1.x()
        dy = pt2.y() - pt1.y()
        length_axis = math.hypot(dx, dy)

        # Soglia minima per la distanza P1-P2: sotto 1 metro, il rumore
        # di click (tipicamente decimetrico in zoom non massimo) puo'
        # ruotare significativamente l'asse. Soglia molto piu' permissiva
        # quando l'azimut e' fornito direttamente (P2 e' fittizio a 1 m).
        min_dist = 0.001 if use_azimuth else 1.0
        if length_axis < min_dist:
            raise ValueError(
                f'La distanza P1-P2 ({length_axis:.4f} m) e\' troppo '
                f'piccola per definire un orientamento affidabile '
                f'(minimo {min_dist} m). Allontanare i punti oppure '
                f'usare l\'opzione "Usa azimut diretto".'
            )

        # Versore lungo l'asse (u) e perpendicolare (v, 90 gradi antiorario)
        ux, uy = dx / length_axis, dy / length_axis
        vx, vy = -uy, ux

        # =============================================
        # 5) Azimut griglia e convergenza dei meridiani
        # =============================================
        # Azimut rispetto al nord griglia, normalizzato a [0, 180) per
        # essere indipendente dall'ordine dei punti P1/P2 (invertendo
        # P1 e P2 l'azimut differirebbe di 180 gradi: la linea pero'
        # geometricamente e' la stessa).
        azimuth_grid_par = math.degrees(math.atan2(dx, dy)) % 180.0
        azimuth_grid_perp = (azimuth_grid_par + 90.0) % 180.0

        # Convergenza dei meridiani (gamma): convenzione topografica
        #   gamma = grid_north - true_north (misurato in senso orario)
        # Equivalentemente: gamma > 0 quando grid nord e' a est di true nord
        # (P1 a est del meridiano centrale UTM).
        #
        # Metodo: per il calcolo si usa il metodo numerico (riproiezione di un
        # piccolo spostamento in latitudine) che funziona con qualsiasi CRS
        # proiettato. Per CRS UTM viene anche calcolata la formula analitica
        # gamma = atan(tan(dlon) * sin(phi)) come riferimento e log.
        crs_geo = QgsCoordinateReferenceSystem('EPSG:4326')
        xform_to_geo = QgsCoordinateTransform(crs, crs_geo, QgsProject.instance())
        xform_from_geo = QgsCoordinateTransform(crs_geo, crs, QgsProject.instance())

        pt1_geo = xform_to_geo.transform(pt1)
        delta_lat = 0.001  # ~111 m
        pt1_north_geo = QgsPointXY(pt1_geo.x(), pt1_geo.y() + delta_lat)

        pt1_proj = QgsPointXY(pt1.x(), pt1.y())
        pt1_north_proj = xform_from_geo.transform(pt1_north_geo)

        # atan2(dn_x, dn_y) = azimut di true_north misurato da grid_north
        # = -(grid_north - true_north) = -gamma. Quindi gamma = -atan2.
        dn_x = pt1_north_proj.x() - pt1_proj.x()
        dn_y = pt1_north_proj.y() - pt1_proj.y()
        convergence = -math.degrees(math.atan2(dn_x, dn_y))

        # Formula analitica UTM (solo per log/verifica): se il CRS e' UTM
        # noto, calcola gamma_analitica = atan(tan(lon-lon0) * sin(phi))
        crs_desc = crs.description() or ''
        is_utm = 'utm' in crs_desc.lower()
        if is_utm:
            try:
                # Estrae il meridiano centrale dalla proj string
                proj_str = crs.toProj()
                lon0 = None
                if '+lon_0=' in proj_str:
                    lon0 = float(
                        proj_str.split('+lon_0=')[1].split()[0]
                    )
                if lon0 is not None:
                    phi = math.radians(pt1_geo.y())
                    dlon = math.radians(pt1_geo.x() - lon0)
                    gamma_analytic = math.degrees(
                        math.atan(math.tan(dlon) * math.sin(phi))
                    )
                    feedback.pushInfo(
                        f'Convergenza analitica UTM (rif.): {gamma_analytic:+.5f}\u00b0 '
                        f'(MC {lon0}\u00b0, lat {math.degrees(phi):.4f}\u00b0)'
                    )
                    # Sostituisce con il valore analitico, piu' preciso
                    convergence = gamma_analytic
            except Exception as e:
                feedback.pushInfo(
                    f'Calcolo convergenza analitica fallito ({e}), '
                    f'uso il metodo numerico.'
                )

        # Formula standard: azimut_geo = azimut_griglia + convergenza
        azimuth_geo_par = (azimuth_grid_par + convergence) % 180.0
        azimuth_geo_perp = (azimuth_grid_perp + convergence) % 180.0

        def format_azimuth(deg):
            """Formatta un azimut di linea (range [0, 180)) come N+-E/W.

            L'azimut e' misurato dal Nord in senso orario verso Est,
            normalizzato modulo 180 per essere indipendente dal verso
            della linea. Convenzione: linea tilted X gradi a est del
            N-S = X.XXXXX gradi E; tilted X gradi a ovest del N-S =
            X.XXXXX gradi W.
            """
            deg = deg % 180.0
            if deg <= 90.0:
                return f'{deg:.5f}\u00b0E'
            else:
                return f'{180.0 - deg:.5f}\u00b0W'

        def format_convergence(deg):
            """Formatta la convergenza dei meridiani: positiva=E, negativa=W."""
            if deg >= 0:
                return f'{deg:.5f}\u00b0E'
            else:
                return f'{abs(deg):.5f}\u00b0W'

        feedback.pushInfo(
            f'Convergenza dei meridiani in P1: {format_convergence(convergence)}\n'
            f'Azimut griglia parallele: {format_azimuth(azimuth_grid_par)}\n'
            f'Azimut geografico parallele: {format_azimuth(azimuth_geo_par)}\n'
            f'Azimut griglia perpendicolari: {format_azimuth(azimuth_grid_perp)}\n'
            f'Azimut geografico perpendicolari: {format_azimuth(azimuth_geo_perp)}'
        )

        # =============================================
        # Proiezione angoli extent nel sistema locale
        # =============================================
        corners = [
            QgsPointXY(extent.xMinimum(), extent.yMinimum()),
            QgsPointXY(extent.xMaximum(), extent.yMinimum()),
            QgsPointXY(extent.xMaximum(), extent.yMaximum()),
            QgsPointXY(extent.xMinimum(), extent.yMaximum()),
        ]

        u_coords = []
        v_coords = []
        for c in corners:
            rx = c.x() - pt1.x()
            ry = c.y() - pt1.y()
            u_coords.append(rx * ux + ry * uy)
            v_coords.append(rx * vx + ry * vy)

        u_min, u_max = min(u_coords), max(u_coords)
        v_min, v_max = min(v_coords), max(v_coords)

        # Geometria dell'extent per il clip
        # Geometria per il clip: poligono se fornito, altrimenti rettangolo
        if clip_polygon_geom is not None:
            clip_rect = clip_polygon_geom
        else:
            clip_rect = QgsGeometry.fromRect(extent)

        # =============================================
        # Campi output — LINEE
        # =============================================
        line_fields = QgsFields()
        line_fields.append(QgsField('id', int))
        line_fields.append(QgsField('tipo', str))
        line_fields.append(QgsField('indice', int))
        line_fields.append(QgsField('etichetta', str))  # B) label leggibile
        line_fields.append(QgsField('dist_asse', float))  # distanza dall'asse
        line_fields.append(QgsField('coord_u', float))    # coord. locale u
        line_fields.append(QgsField('coord_v', float))    # coord. locale v
        line_fields.append(QgsField('azimut_griglia', str))
        line_fields.append(QgsField('azimut_geo', str))
        line_fields.append(QgsField('convergenza', str))
        if use_scale:
            line_fields.append(QgsField('dist_suolo', float))
            line_fields.append(QgsField('fattore_scala', float))

        (sink_lines, dest_lines) = self.parameterAsSink(
            parameters, self.OUTPUT_LINES, context,
            line_fields, QgsWkbTypes.LineString, crs,
        )

        # =============================================
        # Campi output — PUNTI (opzionale, punto 4)
        # =============================================
        point_fields = QgsFields()
        point_fields.append(QgsField('id', int))
        point_fields.append(QgsField('coord_u', float))
        point_fields.append(QgsField('coord_v', float))
        point_fields.append(QgsField('etichetta', str))
        point_fields.append(QgsField('azimut_griglia', str))
        point_fields.append(QgsField('azimut_geo', str))
        point_fields.append(QgsField('convergenza', str))

        sink_points = None
        dest_points = None
        # Sink opzionale: parametro puo' essere None, stringa vuota o assente
        if parameters.get(self.OUTPUT_POINTS) not in (None, ''):
            (sink_points, dest_points) = self.parameterAsSink(
                parameters, self.OUTPUT_POINTS, context,
                point_fields, QgsWkbTypes.Point, crs,
            )

        # =============================================
        # F) Campi output — PUNTI DI RIFERIMENTO P1/P2
        # =============================================
        ref_fields = QgsFields()
        ref_fields.append(QgsField('nome', str))
        ref_fields.append(QgsField('x', float))
        ref_fields.append(QgsField('y', float))
        ref_fields.append(QgsField('distanza_P1_P2', float))
        ref_fields.append(QgsField('azimut_griglia', str))
        ref_fields.append(QgsField('azimut_geo', str))
        ref_fields.append(QgsField('azimut_grid_deg', float))  # numerico per ricostruzione
        ref_fields.append(QgsField('convergenza', str))
        ref_fields.append(QgsField('convergenza_deg', float))  # numerico
        ref_fields.append(QgsField('sp_par_m', float))
        ref_fields.append(QgsField('sp_perp_m', float))
        ref_fields.append(QgsField('origine_direzione', str))

        sink_ref = None
        dest_ref = None
        if parameters.get(self.OUTPUT_REF) not in (None, ''):
            (sink_ref, dest_ref) = self.parameterAsSink(
                parameters, self.OUTPUT_REF, context,
                ref_fields, QgsWkbTypes.Point, crs,
            )

        # =============================================
        # Funzioni ausiliarie
        # =============================================
        feat_id_line = 0
        feat_id_point = 0

        def ensure_consistent_direction(geom, ref_dx, ref_dy):
            """
            6) Direzione consistente: assicura che il primo vertice
            della linea abbia la proiezione minore lungo la direzione
            di riferimento (ref_dx, ref_dy).
            """
            pts = geom.asPolyline()
            if len(pts) < 2:
                return geom

            # Proiezione del primo e dell'ultimo vertice sulla direzione di rif.
            proj_first = pts[0].x() * ref_dx + pts[0].y() * ref_dy
            proj_last = pts[-1].x() * ref_dx + pts[-1].y() * ref_dy

            if proj_first > proj_last:
                # Invertire la linea
                pts.reverse()
                return QgsGeometry.fromPolylineXY(pts)
            return geom

        def make_line_label(tipo, indice):
            """B) Etichetta leggibile: P0, P+1, P-2, T0, T+1, T-2, ..."""
            prefix = 'P' if tipo == 'parallela' else 'T'
            if indice == 0:
                return f'{prefix}0'
            elif indice > 0:
                return f'{prefix}+{indice}'
            else:
                return f'{prefix}{indice}'  # gia' contiene il segno meno

        def add_clipped_line(cx, cy, dir_x, dir_y, half_len,
                             tipo, indice, dist_asse, coord_u_val, coord_v_val,
                             dist_suolo_val, azimut_grid_val, azimut_geo_val):
            """Crea una linea, la clippa, riordina i vertici e la aggiunge.

            D) Gestisce sia LineString sia MultiLineString restituite da
            intersection(), esplodendo le parti in feature separate.
            """
            nonlocal feat_id_line
            ax = cx - dir_x * half_len
            ay = cy - dir_y * half_len
            bx = cx + dir_x * half_len
            by = cy + dir_y * half_len
            line_geom = QgsGeometry.fromPolylineXY(
                [QgsPointXY(ax, ay), QgsPointXY(bx, by)]
            )
            clipped = line_geom.intersection(clip_rect)
            if clipped.isEmpty() or clipped.type() != QgsWkbTypes.LineGeometry:
                return

            # D) Esplosione delle parti se MultiLineString
            if clipped.isMultipart():
                # asMultiPolyline() restituisce lista di liste di QgsPointXY
                parts = clipped.asMultiPolyline()
                part_geoms = [
                    QgsGeometry.fromPolylineXY(pl) for pl in parts if len(pl) >= 2
                ]
            else:
                part_geoms = [clipped]

            label = make_line_label(tipo, indice)
            for part_g in part_geoms:
                # 6) Direzione consistente per ogni parte
                part_g = ensure_consistent_direction(part_g, dir_x, dir_y)

                f = QgsFeature(line_fields)
                f.setGeometry(part_g)
                attrs = [
                    feat_id_line, tipo, indice, label,
                    dist_asse, coord_u_val, coord_v_val,
                    format_azimuth(azimut_grid_val),
                    format_azimuth(azimut_geo_val),
                    format_convergence(convergence),
                ]
                if use_scale:
                    attrs.append(dist_suolo_val)
                    attrs.append(scale_factor)
                f.setAttributes(attrs)
                sink_lines.addFeature(f, QgsFeatureSink.FastInsert)
                feat_id_line += 1

        # Semi-lunghezza: diagonale dell'extent
        diag = math.hypot(
            extent.xMaximum() - extent.xMinimum(),
            extent.yMaximum() - extent.yMinimum(),
        )
        half_len = diag

        # =============================================
        # Generazione linee PARALLELE (spostate lungo v)
        # =============================================
        # Calcolo preliminare dei conteggi per la barra di progresso (E)
        if grid_type in (0, 2):
            i_min = math.floor(v_min / sp_par_map)
            i_max = math.ceil(v_max / sp_par_map)
            total_par = i_max - i_min + 1
        else:
            i_min = i_max = 0
            total_par = 0

        if grid_type in (1, 2):
            j_min = math.floor(u_min / sp_perp_map)
            j_max = math.ceil(u_max / sp_perp_map)
            total_perp = j_max - j_min + 1
        else:
            j_min = j_max = 0
            total_perp = 0

        # Sanity check: griglie sovradimensionate possono bloccare QGIS
        # Soglia: oltre 500.000 linee o 1.000.000 di nodi richiede conferma
        MAX_LINES = 500_000
        MAX_NODES = 1_000_000
        total_lines = total_par + total_perp
        if total_lines > MAX_LINES:
            raise ValueError(
                f'La griglia richiederebbe {total_lines:,} linee, oltre '
                f'la soglia di sicurezza ({MAX_LINES:,}). Aumentare la '
                f'spaziatura o ridurre l\'estensione. Range u: '
                f'{u_max-u_min:.1f}m, range v: {v_max-v_min:.1f}m, '
                f'sp_par: {sp_par_map:.4f}m, sp_perp: {sp_perp_map:.4f}m.'
            )
        total_points_est = (
            total_par * total_perp
            if (sink_points is not None and grid_type == 2)
            else 0
        )
        if total_points_est > MAX_NODES:
            raise ValueError(
                f'I nodi della griglia sarebbero {total_points_est:,}, '
                f'oltre la soglia di sicurezza ({MAX_NODES:,}). '
                f'Aumentare la spaziatura o disattivare l\'output dei nodi.'
            )

        total_ops = max(1, total_par + total_perp + total_points_est)
        ops_done = 0

        def update_progress():
            """Aggiorna la barra di progresso in percentuale."""
            nonlocal ops_done
            ops_done += 1
            feedback.setProgress(int(100.0 * ops_done / total_ops))

        # =============================================
        # Generazione linee PARALLELE (spostate lungo v)
        # =============================================
        par_offsets = []  # lista di (indice, offset_v) per i nodi
        if grid_type in (0, 2):
            feedback.pushInfo(
                f'Linee parallele: {total_par} (indici {i_min}..{i_max})'
            )
            for i in range(i_min, i_max + 1):
                if feedback.isCanceled():
                    break
                offset_v = i * sp_par_map
                cx = pt1.x() + vx * offset_v
                cy = pt1.y() + vy * offset_v
                # distanza al suolo
                dist_suolo = offset_v / scale_factor if use_scale else offset_v
                add_clipped_line(
                    cx, cy, ux, uy, half_len,
                    'parallela', i, offset_v,
                    0.0,       # coord_u: la linea corre lungo u, offset in v
                    offset_v,  # coord_v
                    dist_suolo,
                    azimuth_grid_par, azimuth_geo_par,
                )
                par_offsets.append((i, offset_v))
                update_progress()

        # =============================================
        # Generazione linee PERPENDICOLARI (spostate lungo u)
        # =============================================
        perp_offsets = []
        if grid_type in (1, 2):
            feedback.pushInfo(
                f'Linee perpendicolari: {total_perp} (indici {j_min}..{j_max})'
            )
            for j in range(j_min, j_max + 1):
                if feedback.isCanceled():
                    break
                offset_u = j * sp_perp_map
                cx = pt1.x() + ux * offset_u
                cy = pt1.y() + uy * offset_u
                dist_suolo = offset_u / scale_factor if use_scale else offset_u
                add_clipped_line(
                    cx, cy, vx, vy, half_len,
                    'perpendicolare', j, offset_u,
                    offset_u,  # coord_u
                    0.0,       # coord_v: la linea corre lungo v, offset in u
                    dist_suolo,
                    azimuth_grid_perp, azimuth_geo_perp,
                )
                perp_offsets.append((j, offset_u))
                update_progress()

        # =============================================
        # 4) Punti alle intersezioni della griglia
        # =============================================
        if sink_points is not None and grid_type == 2:
            feedback.pushInfo('Generazione punti intersezione...')
            canceled = False
            for iv, ov in par_offsets:
                if canceled:
                    break
                for iu, ou in perp_offsets:
                    if feedback.isCanceled():
                        canceled = True
                        break
                    px = pt1.x() + ux * ou + vx * ov
                    py = pt1.y() + uy * ou + vy * ov
                    pt_geom = QgsGeometry.fromPointXY(QgsPointXY(px, py))

                    # Includere anche i nodi che cadono sul bordo del clip:
                    # intersects() invece di contains() (essenziale per picchetti
                    # posizionati esattamente sul limite del saggio)
                    if not clip_rect.intersects(pt_geom):
                        continue

                    # Etichetta: indici reali del loop (no arrotondamento)
                    p_lbl = make_line_label('parallela', iv)
                    t_lbl = make_line_label('perpendicolare', iu)
                    label = f'{p_lbl}_{t_lbl}'

                    f = QgsFeature(point_fields)
                    f.setGeometry(pt_geom)
                    f.setAttributes([
                        feat_id_point, ou, ov, label,
                        format_azimuth(azimuth_grid_par),
                        format_azimuth(azimuth_geo_par),
                        format_convergence(convergence),
                    ])
                    sink_points.addFeature(f, QgsFeatureSink.FastInsert)
                    feat_id_point += 1
                    update_progress()

            feedback.pushInfo(f'Punti intersezione: {feat_id_point}')
        elif sink_points is not None and grid_type != 2:
            feedback.pushInfo(
                'Punti intersezione generati solo con griglia completa.'
            )

        # =============================================
        # F) Punti di riferimento P1 e P2
        # =============================================
        if sink_ref is not None:
            origine = (
                'Azimut diretto (P2 fittizio)' if use_azimuth
                else 'Punto 2 indicato sulla mappa'
            )
            dist_p1p2 = math.hypot(pt2.x() - pt1.x(), pt2.y() - pt1.y())

            for nome, p in (('P1', pt1), ('P2', pt2)):
                f = QgsFeature(ref_fields)
                f.setGeometry(QgsGeometry.fromPointXY(p))
                f.setAttributes([
                    nome,
                    p.x(),
                    p.y(),
                    dist_p1p2,
                    format_azimuth(azimuth_grid_par),
                    format_azimuth(azimuth_geo_par),
                    azimuth_grid_par,  # numerico per ricostruzione
                    format_convergence(convergence),
                    convergence,       # numerico
                    sp_par,
                    sp_perp,
                    origine,
                ])
                sink_ref.addFeature(f, QgsFeatureSink.FastInsert)

            feedback.pushInfo('Salvati punti di riferimento P1 e P2.')

        # =============================================
        # Layer asse (linea P1-P2) opzionale
        # =============================================
        if parameters.get(self.OUTPUT_AXIS) not in (None, ''):
            axis_fields = QgsFields()
            axis_fields.append(QgsField('nome', str))
            axis_fields.append(QgsField('lunghezza_m', float))
            axis_fields.append(QgsField('azimut_griglia', str))
            axis_fields.append(QgsField('azimut_geo', str))
            axis_fields.append(QgsField('azimut_grid_deg', float))
            axis_fields.append(QgsField('convergenza', str))
            (sink_axis, dest_axis) = self.parameterAsSink(
                parameters, self.OUTPUT_AXIS, context,
                axis_fields, QgsWkbTypes.LineString, crs,
            )
            f = QgsFeature(axis_fields)
            f.setGeometry(QgsGeometry.fromPolylineXY([pt1, pt2]))
            f.setAttributes([
                'Asse P1-P2',
                math.hypot(pt2.x()-pt1.x(), pt2.y()-pt1.y()),
                format_azimuth(azimuth_grid_par),
                format_azimuth(azimuth_geo_par),
                azimuth_grid_par,
                format_convergence(convergence),
            ])
            sink_axis.addFeature(f, QgsFeatureSink.FastInsert)
            feedback.pushInfo('Salvato layer asse della griglia.')
        else:
            dest_axis = None

        # Barra di progresso a 100% a fine elaborazione
        feedback.setProgress(100)

        feedback.pushInfo(
            f'Totale linee: {feat_id_line} | '
            f'Totale punti: {feat_id_point}'
        )

        results = {self.OUTPUT_LINES: dest_lines}
        if dest_points is not None:
            results[self.OUTPUT_POINTS] = dest_points
        if dest_ref is not None:
            results[self.OUTPUT_REF] = dest_ref
        if dest_axis is not None:
            results[self.OUTPUT_AXIS] = dest_axis
        return results
