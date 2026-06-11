"""
Main dialog for GeoPackage Converter.

UX hardening (Phase 11.5):
* Output label/browser switch dynamically (file vs folder) by strategy.
* Run button stays disabled until inputs are valid.
* Folder scan runs in a QgsTask with progress.
* QSettings remembers the last used paths and options.
* Post-run "Open output folder" / "Open report" buttons.
* Tooltips on every control.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import List, Optional

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsMapLayer,
    QgsMessageLog,
    QgsProject,
    QgsRasterLayer,
    QgsTask,
    QgsVectorLayer,
)
from qgis.gui import QgsProjectionSelectionWidget
from qgis.PyQt import uic
from qgis.PyQt.QtCore import QFileInfo, QSettings, Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QFileDialog,
    QListWidgetItem,
    QMessageBox,
    QSizePolicy,
    QTableWidgetItem,
    QToolButton,
)

from ..compat import CHECKED, ITEM_IS_ENABLED, ITEM_IS_USER_CHECKABLE, UNCHECKED
from ..core.converter import ConversionResult, Converter
from ..core.raster_converter import RasterConverter
from ..core.folder_scanner import scan_folder
from ..core.grouping_strategies import (
    group_all_in_one,
    group_by_legend_group,
    group_by_subfolder,
)
from ..core.report_generator import generate_html_report
from ..processing._common import (
    GROUPING_ALL_IN_ONE,
    GROUPING_BY_LEGEND_GROUP,
    GROUPING_BY_SUBFOLDER,
    GROUPING_LABELS,
)

LOG_TAG = "GeoPackage Converter"
UI_FILE = Path(__file__).resolve().parent / "main_dialog_base.ui"
SETTINGS_PREFIX = "geopackage_converter/"


def _is_file_strategy(grouping_index: int) -> bool:
    """True when the output must be a single .gpkg file path."""
    return grouping_index == GROUPING_ALL_IN_ONE


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------


class _ScanTask(QgsTask):
    """Run `scan_folder` off the UI thread."""

    def __init__(
        self,
        folder: Path,
        mirror_hierarchy: bool = False,
    ) -> None:
        super().__init__("GeoPackage Converter scan", QgsTask.CanCancel)
        self._folder = folder
        self._mirror = mirror_hierarchy
        self.results: List[dict] = []

    def run(self) -> bool:  # noqa: D401
        try:
            # Recursive scanning is always on now: the option proved
            # confusing in the UI and was disabled only in extreme
            # edge cases (backup folders the user could exclude
            # at the filesystem level).
            self.results = scan_folder(
                self._folder,
                recursive=True,
                mirror_hierarchy=self._mirror,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            QgsMessageLog.logMessage(f"Scan failed: {exc}", LOG_TAG)
            return False


class _ConversionTask(QgsTask):
    """Grouping + per-bundle conversion + HTML report."""

    def __init__(
        self,
        items: List[dict],
        output_path: Path,
        grouping_index: int,
        converter: Converter,
        mirror_layout: bool = False,
        raster_converter: Optional[RasterConverter] = None,
        project_name: str = "",
    ) -> None:
        super().__init__("GeoPackage Converter conversion", QgsTask.CanCancel)
        self._items = items
        self._output_path = output_path
        self._grouping_index = grouping_index
        self._converter = converter
        self._raster_converter = raster_converter
        self._project_name = project_name
        self._mirror_layout = mirror_layout
        self.result: Optional[ConversionResult] = None
        self.report_path: Optional[Path] = None
        self._log_lines: List[str] = []

    def consume_log(self) -> List[str]:
        out, self._log_lines = self._log_lines, []
        return out

    def _merge_result(self, aggregate: ConversionResult, r: ConversionResult) -> None:
        """Merge a per-bundle ConversionResult into the aggregate."""
        aggregate.success_count += r.success_count
        aggregate.vector_success_count += r.vector_success_count
        aggregate.raster_success_count += r.raster_success_count
        aggregate.error_count += r.error_count
        aggregate.errors.extend(r.errors)
        aggregate.output_layers.extend(r.output_layers)
        for err in r.errors:
            self._log_lines.append(
                f"  ❌ {Path(err['source']).name}: {err['message']}"
            )
        aggregate.warnings.extend(r.warnings)
        for p in r.output_files:
            if p not in aggregate.output_files:
                aggregate.output_files.append(p)
        aggregate.duration_seconds += r.duration_seconds

    @staticmethod
    def _clean_stale_gpkg(output_path: Path) -> None:
        """Remove a pre-existing GPKG and its SQLite sidecars.

        Called for raster-only bundles (the vector converter does this
        internally, but when there are no vectors we must do it here).
        """
        from ..core._paths import long_path

        try:
            lp = long_path(output_path)
            if os.path.isfile(lp):
                os.remove(lp)
            for suffix in ("-shm", "-wal", "-journal"):
                sidecar = long_path(Path(str(output_path) + suffix))
                if os.path.isfile(sidecar):
                    try:
                        os.remove(sidecar)
                    except OSError:
                        pass
        except OSError:
            pass  # best-effort; converter will report write errors

    def _bundles(self):
        out = self._output_path
        if self._grouping_index == GROUPING_ALL_IN_ONE:
            return group_all_in_one(self._items, out)
        base = out if out.is_dir() or not out.suffix else out.parent
        if self._grouping_index == GROUPING_BY_SUBFOLDER:
            return group_by_subfolder(self._items, base, mirror_layout=self._mirror_layout)
        if self._grouping_index == GROUPING_BY_LEGEND_GROUP:
            return group_by_legend_group(self._items, base, project_name=self._project_name)
        raise ValueError(f"Unknown grouping index: {self._grouping_index}")

    def run(self) -> bool:  # noqa: D401
        try:
            bundles = self._bundles()
            self._log_lines.append(f"Bundles: {len(bundles)}")

            # Detect rename collisions and surface them in the log up front.
            for b in bundles:
                renamed = [
                    f"{i.get('original_name')} → {i['name']}"
                    for i in b["items"]
                    if i.get("original_name") and i["name"] != i["original_name"]
                ]
                if renamed:
                    self._log_lines.append(
                        f"  Conflitti nomi risolti in {b['output_path'].name}: "
                        + ", ".join(renamed)
                    )

            aggregate = ConversionResult(dry_run=self._converter.dry_run)
            for i, bundle in enumerate(bundles, start=1):
                if self.isCanceled():
                    self._log_lines.append("Annullato dall'utente")
                    return False
                # Partition items into vector and raster.
                all_items = bundle["items"]
                vector_items = [it for it in all_items if it.get("item_type", "vector") != "raster"]
                raster_items = [it for it in all_items if it.get("item_type") == "raster"]

                if raster_items:
                    count_label = f"{len(vector_items)} vett. + {len(raster_items)} raster"
                else:
                    count_label = f"{len(all_items)} layer"
                self._log_lines.append(
                    f"[{i}/{len(bundles)}] {bundle['output_path'].name} "
                    f"({count_label})"
                )

                def cb(percent: int, msg: str, _i=i, _n=len(bundles)) -> None:
                    base = int(100 * (_i - 1) / _n)
                    span = int(100 / _n)
                    self.setProgress(base + int(percent * span / 100))
                    if msg:
                        self._log_lines.append(f"  {msg}")

                # Convert vectors first (creates the GPKG — it wipes any
                # pre-existing file internally).
                if vector_items:
                    r = self._converter.convert(vector_items, bundle["output_path"], cb)
                    self._merge_result(aggregate, r)
                elif raster_items:
                    # Raster-only bundle: wipe stale GPKG so we don't
                    # append into a file from a previous run.
                    self._clean_stale_gpkg(bundle["output_path"])

                # Then rasters (appended to the same GPKG).
                if raster_items and self._raster_converter:
                    r = self._raster_converter.convert(raster_items, bundle["output_path"], cb)
                    self._merge_result(aggregate, r)

            self.result = aggregate
            report_dir = (
                aggregate.output_files[0].parent
                if aggregate.output_files
                else Path(tempfile.gettempdir())
            )
            self.report_path = generate_html_report(
                aggregate, report_dir / "geopackage_converter_report.html"
            )
            self._log_lines.append(f"Report: {self.report_path}")
            return True
        except Exception as exc:  # noqa: BLE001
            self._log_lines.append(f"Task fallito: {exc}")
            QgsMessageLog.logMessage(f"Task failed: {exc}", LOG_TAG)
            return False


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class GeoPackageConverterDialog(QDialog):
    """Main interactive dialog."""

    def __init__(self, iface=None, parent=None) -> None:
        super().__init__(parent)
        self.iface = iface
        self._task: Optional[_ConversionTask] = None
        self._scan_task: Optional[_ScanTask] = None
        self._scan_results: List[dict] = []
        self._original_tree_snapshot: list = []
        self._last_output_dir: Optional[Path] = None
        self._last_report: Optional[Path] = None
        self._restoring_settings = False

        uic.loadUi(str(UI_FILE), self)
        self._retranslate_ui()

        self._populate_grouping_combo()
        self._populate_crs_combo()
        self._populate_project_layers()
        self._install_tooltips()
        self._install_help_buttons()
        self._wire_signals()
        self._restore_settings()
        self._update_output_label()
        self._apply_tab_visibility()
        self.btnScan.setVisible(False)
        self._force_suggest_output()
        self._scan_folder()
        self._refresh_run_state()

    # ------------------------------------------------------------------
    # UI text override (makes .ui strings go through self.tr)
    # ------------------------------------------------------------------

    def _retranslate_ui(self) -> None:
        self.setWindowTitle(self.tr("GeoPackage Converter"))
        self.tabWidget.setTabText(0, self.tr("Da progetto"))
        self.tabWidget.setTabText(1, self.tr("Da cartella"))
        self.lblProjectHelp.setText(
            self.tr("Seleziona i layer vettoriali e raster del progetto da convertire in GeoPackage.")
        )
        self.btnSelectAll.setText(self.tr("Seleziona tutto"))
        self.btnSelectNone.setText(self.tr("Deseleziona tutto"))
        self.btnSelectVisible.setText(self.tr("Solo layer attivi"))
        self.lblFolder.setText(self.tr("Cartella di input:"))
        self.btnBrowseFolder.setText(self.tr("Sfoglia…"))
        self.chkMirrorStructure.setText(self.tr("Replica struttura su disco"))
        self.btnScan.setText(self.tr("Scansiona cartella"))
        self.tblFolderPreview.setHorizontalHeaderLabels([
            self.tr("Nome"), self.tr("Formato"), self.tr("Tipo"),
            self.tr("CRS"), self.tr("Dettagli"), self.tr("Encoding"),
            self.tr("Avvisi"),
        ])
        self.grpOptions.setTitle(self.tr("Opzioni di conversione"))
        self.lblTargetCrs.setText(self.tr("CRS di destinazione:"))
        self.lblGrouping.setText(self.tr("Strategia di raggruppamento:"))
        self.chkSaveStyles.setText(self.tr("Salva stili"))
        self.chkValidate.setText(self.tr("Valida geometrie (solo vettoriali)"))
        self.chkDryRun.setText(self.tr("Dry-run (anteprima)"))
        self.btnBrowseOutput.setText(self.tr("Sfoglia…"))
        self.btnOpenOutput.setText(self.tr("Apri cartella"))
        self.btnOpenReport.setText(self.tr("Apri report"))
        self.lblRasterOptions.setText(self.tr("Opzioni raster:"))
        self.lblTileFormat.setText(self.tr("Formato tile:"))
        self.lblTileSize.setText(self.tr("Tile:"))
        self.lblJpegQuality.setText(self.tr("Qualità:"))
        self.btnRun.setText(self.tr("Esegui"))
        self.btnCancel.setText(self.tr("Annulla"))
        self.btnClose.setText(self.tr("Chiudi"))

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _populate_grouping_combo(self) -> None:
        """
        Populate the strategy combo with options valid for the active tab.

        * "Da cartella" tab  -> all-in-one + per sottocartella
        * "Da progetto" tab  -> all-in-one + per gruppo della legenda

        Selection is preserved across rebuilds when the previously
        selected constant is still available; otherwise it falls back
        to "all-in-one".
        """
        previous = self._current_grouping_constant()
        self.cmbGrouping.blockSignals(True)
        self.cmbGrouping.clear()
        on_folder = (
            self.tabWidget.currentWidget() is self.tabFromFolder
            if hasattr(self, "tabFromFolder") else True
        )
        constants = [GROUPING_ALL_IN_ONE]
        if on_folder:
            constants.append(GROUPING_BY_SUBFOLDER)
        else:
            constants.append(GROUPING_BY_LEGEND_GROUP)
        for const in constants:
            self.cmbGrouping.addItem(self.tr(GROUPING_LABELS[const]), userData=const)
        # Restore previous selection if still present.
        if previous is not None:
            for i in range(self.cmbGrouping.count()):
                if self.cmbGrouping.itemData(i) == previous:
                    self.cmbGrouping.setCurrentIndex(i)
                    break
        self.cmbGrouping.blockSignals(False)

    def _current_grouping_constant(self) -> int:
        """Return the GROUPING_* constant currently selected in the combo."""
        data = self.cmbGrouping.currentData() if self.cmbGrouping.count() else None
        if data is None:
            return GROUPING_ALL_IN_ONE
        return int(data)

    def _populate_crs_combo(self) -> None:
        """Configure the QgsProjectionSelectionWidget with sane defaults for Italy."""
        # Allow the "no reprojection" choice and label it clearly.
        self.crsSelector.setOptionVisible(
            QgsProjectionSelectionWidget.CrsNotSet, True
        )
        self.crsSelector.setNotSetText(self.tr("(nessuna riproiezione)"))
        # Default to "no reprojection" (invalid CRS).
        self.crsSelector.setCrs(QgsCoordinateReferenceSystem())

    def _populate_project_layers(self) -> None:
        self.lstProjectLayers.clear()
        project = QgsProject.instance()
        if project is None:
            return
        supported = [
            l for l in project.mapLayers().values()
            if l.type() in (QgsMapLayer.VectorLayer, QgsMapLayer.RasterLayer)
        ]
        if not supported:
            placeholder = QListWidgetItem(self.tr("(nessun layer vettoriale o raster nel progetto)"))
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.lstProjectLayers.addItem(placeholder)
            return
        for layer in supported:
            crs = layer.crs().authid() if layer.crs().isValid() else "?"
            if layer.type() == QgsMapLayer.RasterLayer:
                label = (
                    f"{layer.name()}  —  {crs}  "
                    f"({layer.width()}x{layer.height()}, "
                    f"{layer.bandCount()} {'banda' if layer.bandCount() == 1 else 'bande'})  "
                    f"[Raster]"
                )
            else:
                label = f"{layer.name()}  —  {crs}  ({layer.featureCount()} feat.)"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | ITEM_IS_USER_CHECKABLE | ITEM_IS_ENABLED)
            item.setCheckState(CHECKED)
            item.setData(Qt.ItemDataRole.UserRole, layer.id())
            item.setToolTip(str(layer.source()))
            self.lstProjectLayers.addItem(item)

    def _install_tooltips(self) -> None:
        """
        Three-tier hover help on every control:
          * setToolTip   — rich HTML, appears on hover (~1s).
          * setStatusTip — short, appears in the QGIS status bar.
          * setWhatsThis — long-form, accessible via Shift+F1 or right-click → "What's This?".
        """

        def apply(widget, tip_html: str, status: str, whats: str = "") -> None:
            widget.setToolTip(tip_html)
            widget.setStatusTip(status)
            widget.setWhatsThis(whats or tip_html)

        # ----- Common options -----
        apply(
            self.crsSelector,
            self.tr(
                "<b>CRS di destinazione</b><br>"
                "Riproietta <i>tutte</i> i layer al sistema di coordinate scelto.<br><br>"
                "Clicca per scegliere <b>qualsiasi</b> CRS supportato da QGIS, "
                "oppure usa il bottone laterale per accedere ai CRS recenti, ai "
                "preferiti e alla ricerca completa.<br><br>"
                "<b>Più usati in Italia:</b><br>"
                "• <b>EPSG:25832 / 25833</b> — ETRS89 / UTM 32N-33N (standard europeo INSPIRE)<br>"
                "• <b>EPSG:32632 / 32633</b> — WGS 84 / UTM 32N-33N<br>"
                "• <b>EPSG:6875 / 6876</b> — RDN2008 (sistema nazionale corrente)<br>"
                "• <b>EPSG:3003 / 3004</b> — Gauss-Boaga (sistema storico)<br>"
                "• <b>EPSG:4326</b> — WGS 84 geografiche<br><br>"
                "Lascia <i>(nessuna riproiezione)</i> per mantenere il CRS originale di ogni layer."
            ),
            self.tr("CRS di destinazione (clicca per scegliere)"),
        )
        self.lblTargetCrs.setToolTip(self.crsSelector.toolTip())

        apply(
            self.cmbGrouping,
            self.tr(
                "<b>Strategia di raggruppamento</b><br>"
                "Decide <i>quanti</i> file GeoPackage verranno generati e come saranno divise i layer:<br><br>"
                "• <b>Tutto in un unico GeoPackage</b><br>"
                "&nbsp;&nbsp;Un solo file <code>.gpkg</code> con tutti i layer dentro.<br>"
                "• <b>Un GeoPackage per sottocartella</b><br>"
                "&nbsp;&nbsp;<i>(solo modalità Da cartella)</i>. Replica la struttura della cartella di origine.<br>"
                "• <b>Un GeoPackage per gruppo della legenda</b><br>"
                "&nbsp;&nbsp;<i>(solo modalità Da progetto)</i>. Crea un <code>.gpkg</code> per ogni gruppo "
                "del Pannello Layer. I layer <b>senza gruppo</b> ottengono ciascuno "
                "il proprio file con il nome del layer."
            ),
            self.tr("Quanti GeoPackage generare e come dividere i layer"),
        )
        self.lblGrouping.setToolTip(self.cmbGrouping.toolTip())

        apply(
            self.lblOutput,
            self.tr(
                "<b>Output</b><br>"
                "Se la strategia è <i>Tutto in uno</i>, indica il percorso del file <code>.gpkg</code>. "
                "Il plugin propone un nome di default basato sulla cartella o sul progetto, "
                "ma puoi <b>modificarlo liberamente</b> scrivendo nel campo o usando <i>Sfoglia…</i>.<br>"
                "Per le altre strategie, indica la <b>cartella</b> dove verranno generati i file "
                "(con nomi automatici)."
            ),
            self.tr("Percorso del file o cartella di destinazione (modificabile)"),
        )
        self.edtOutput.setToolTip(self.lblOutput.toolTip())
        self.btnBrowseOutput.setToolTip(self.tr("Apri il selettore di file/cartella"))
        self.btnBrowseOutput.setStatusTip(self.btnBrowseOutput.toolTip())

        apply(
            self.chkSaveStyles,
            self.tr(
                "<b>Salva stili</b><br>"
                "Memorizza lo stile QML di ogni layer dentro il GeoPackage stesso "
                "(tabella <code>layer_styles</code>).<br><br>"
                "Quando riaprirai il <code>.gpkg</code> in QGIS, lo stile sarà <i>già applicato</i> "
                "senza dover importare manualmente un file <code>.qml</code>.<br><br>"
                "<b>Nota:</b> si applica solo ai layer <i>vettoriali</i>. "
                "I raster non supportano il salvataggio dello stile nel GeoPackage "
                "(limitazione GDAL)."
            ),
            self.tr("Salva lo stile di ogni layer vettoriale dentro il GeoPackage"),
        )

        apply(
            self.chkValidate,
            self.tr(
                "<b>Valida geometrie</b><br>"
                "Esegue l'algoritmo <code>native:fixgeometries</code> di QGIS su ogni layer "
                "<i>prima</i> della scrittura.<br><br>"
                "Corregge problemi tipici come: anelli che si auto-intersecano, geometrie "
                "duplicate, vertici troppo vicini.<br><br>"
                "<b>Attenzione</b>: rallenta la conversione (anche di molto su dataset grandi). "
                "Attiva solo se sai di avere dati 'sporchi'."
            ),
            self.tr("Corregge geometrie invalide prima della scrittura (più lento)"),
        )

        apply(
            self.chkDryRun,
            self.tr(
                "<b>Dry-run (anteprima)</b><br>"
                "Modalità simulazione: nessun file viene scritto su disco.<br><br>"
                "Il plugin esegue tutti i controlli (sorgenti, conflitti di nome, raggruppamenti) "
                "e produce comunque il <i>report HTML</i> con quello che <i>sarebbe</i> stato fatto.<br><br>"
                "Utile per verificare la configurazione prima di una conversione massiva."
            ),
            self.tr("Anteprima: nessuna scrittura su disco"),
        )

        # ----- Folder tab -----
        apply(
            self.chkMirrorStructure,
            self.tr(
                "<b>Replica struttura su disco</b><br><br>"
                "Se la tua cartella di input ha sottocartelle annidate, questa opzione "
                "decide come vengono organizzati i GeoPackage in uscita.<br><br>"
                "<b>Esempio — la tua cartella:</b><br>"
                "<pre style='font-size:11px'>"
                "progetto/\n"
                "├── strade/\n"
                "│   ├── comunali/  →  vie.shp\n"
                "│   └── provinciali/  →  strade_sp.shp\n"
                "└── edifici/  →  residenziali.shp</pre>"
                "<b>✗ Disattivata</b> — tutto nella stessa cartella:<br>"
                "<pre style='font-size:11px'>"
                "output/\n"
                "├── comunali.gpkg\n"
                "├── provinciali.gpkg\n"
                "└── edifici.gpkg</pre>"
                "<b>✓ Attivata</b> — stessa struttura dell'input:<br>"
                "<pre style='font-size:11px'>"
                "output/\n"
                "├── strade/\n"
                "│   ├── comunali/comunali.gpkg\n"
                "│   └── provinciali/provinciali.gpkg\n"
                "└── edifici/edifici.gpkg</pre>"
            ),
            self.tr("Ricrea la struttura cartelle dell'input nell'output"),
        )
        self.edtFolder.setToolTip(self.tr(
            "<b>Cartella di input</b><br>"
            "La cartella che contiene i file da convertire.<br><br>"
            "<b>Vettoriali:</b> <code>.shp .tab .kml .kmz .gml .geojson "
            ".json .dxf .gpx .mif</code><br>"
            "<b>Raster:</b> <code>.tif .tiff .jp2 .ecw .img .asc .vrt</code><br><br>"
            "Sono supportati anche file <code>.zip</code> contenenti shapefile "
            "(letti tramite <code>/vsizip/</code>, niente estrazione)."
        ))
        self.lblFolder.setToolTip(self.edtFolder.toolTip())
        self.btnBrowseFolder.setToolTip(self.tr("Sfoglia per selezionare una cartella"))
        self.btnBrowseFolder.setStatusTip(self.btnBrowseFolder.toolTip())

        apply(
            self.tblFolderPreview,
            self.tr(
                "<b>Anteprima cartella</b><br>"
                "Risultato della scansione. Le colonne mostrano i metadati di ogni file. "
                "Eventuali problemi (CRS sconosciuto, encoding sospetto) compaiono nella "
                "colonna <i>Avvisi</i>."
            ),
            self.tr("Risultato della scansione"),
        )

        # ----- Project tab -----
        apply(
            self.lstProjectLayers,
            self.tr(
                "<b>Layer del progetto</b><br>"
                "Spunta i layer vettoriali e raster da convertire. Dopo il nome trovi CRS e "
                "metadati; il <i>tooltip su ogni riga</i> mostra il percorso del file sorgente."
            ),
            self.tr("Spunta i layer del progetto da convertire"),
        )
        self.btnSelectAll.setToolTip(self.tr("Spunta tutti i layer della lista"))
        self.btnSelectNone.setToolTip(self.tr("Toglie la spunta a tutti i layer"))
        self.btnSelectVisible.setToolTip(self.tr(
            "<b>Solo layer attivi</b><br>"
            "Spunta esclusivamente i layer attualmente <i>visibili</i> sulla mappa, "
            "cioè quelli accesi nel Pannello Layer (incluso lo stato dei gruppi che li contengono). "
            "Tutti gli altri vengono deselezionati.<br><br>"
            "Utile per esportare esattamente ciò che vedi sulla mappa, "
            "ignorando i layer spenti o dentro gruppi disattivati."
        ))
        self.btnSelectAll.setStatusTip(self.btnSelectAll.toolTip())
        self.btnSelectNone.setStatusTip(self.btnSelectNone.toolTip())
        self.btnSelectVisible.setStatusTip(self.tr("Spunta solo i layer visibili sulla mappa"))

        # ----- Bottom buttons -----
        apply(
            self.btnRun,
            self.tr(
                "<b>Esegui</b><br>"
                "Avvia la conversione con le opzioni correnti.<br><br>"
                "Si abilita solo quando sono stati scelti almeno un input "
                "(layer o scansione) e un percorso di output."
            ),
            self.tr("Avvia la conversione"),
        )
        self.btnCancel.setToolTip(self.tr("Interrompe la conversione in corso"))
        self.btnCancel.setStatusTip(self.btnCancel.toolTip())
        self.btnClose.setToolTip(self.tr("Chiude il dialog (le opzioni vengono salvate)"))
        self.btnClose.setStatusTip(self.btnClose.toolTip())

        apply(
            self.btnOpenOutput,
            self.tr(
                "<b>Apri cartella</b><br>"
                "Apre nel file manager di sistema la cartella che contiene "
                "i GeoPackage generati nell'ultima conversione."
            ),
            self.tr("Apre la cartella dei file generati"),
        )
        apply(
            self.btnOpenReport,
            self.tr(
                "<b>Apri report</b><br>"
                "Riapre nel browser il report HTML dell'ultima conversione, "
                "con dettaglio di file convertiti, errori e avvisi."
            ),
            self.tr("Riapre il report HTML dell'ultima conversione"),
        )

        # ----- Raster options -----
        apply(
            self.cmbTileFormat,
            self.tr(
                "<b>Formato tile raster</b><br>"
                "Codifica delle tile all'interno del GeoPackage raster:<br><br>"
                "• <b>AUTO</b> — GDAL sceglie automaticamente in base ai dati<br>"
                "• <b>PNG</b> — Senza perdita, ideale per dati categorici o con trasparenza<br>"
                "• <b>JPEG</b> — Compressione lossy, file più piccoli per ortofoto/satellitari<br>"
                "• <b>WEBP</b> — Buon compromesso qualità/dimensione (richiede GDAL ≥ 2.4)"
            ),
            self.tr("Formato di compressione delle tile raster"),
        )
        self.lblTileFormat.setToolTip(self.cmbTileFormat.toolTip())

        apply(
            self.cmbTileSize,
            self.tr(
                "<b>Dimensione tile</b><br>"
                "Lato in pixel di ogni tile quadrata:<br>"
                "• <b>256</b> — standard, migliore compatibilità<br>"
                "• <b>512</b> — meno tile ma più grandi, leggermente più veloce su dataset enormi"
            ),
            self.tr("Dimensione in pixel delle tile raster"),
        )
        self.lblTileSize.setToolTip(self.cmbTileSize.toolTip())

        apply(
            self.spnJpegQuality,
            self.tr(
                "<b>Qualità compressione</b><br>"
                "Valore 1–100 per JPEG e WebP (ignorato per PNG).<br>"
                "75 è un buon default; valori più alti = migliore qualità ma file più grandi."
            ),
            self.tr("Qualità di compressione JPEG/WebP (1–100)"),
        )
        self.lblJpegQuality.setToolTip(self.spnJpegQuality.toolTip())

        # ----- Group box title -----
        self.grpOptions.setToolTip(self.tr(
            "Opzioni applicate sia alla modalità <i>Da progetto</i> sia <i>Da cartella</i>."
        ))

    @staticmethod
    def _find_containing_layout(widget):
        """Return (layout, index) of the QLayout that holds `widget`, or None."""
        parent = widget.parentWidget()
        if parent is None:
            return None
        # Breadth-first search through all layouts/sublayouts of the parent.
        queue = []
        top = parent.layout()
        if top is not None:
            queue.append(top)
        while queue:
            layout = queue.pop(0)
            idx = layout.indexOf(widget)
            if idx >= 0:
                return layout, idx
            for i in range(layout.count()):
                item = layout.itemAt(i)
                child_layout = item.layout() if item is not None else None
                if child_layout is not None:
                    queue.append(child_layout)
        return None

    def _install_help_buttons(self) -> None:
        """
        Add a small inline ⓘ button next to options that benefit from
        an explicit explanation popup. The button reuses each widget's
        existing tooltip text so wording stays in one place.
        """
        targets = (
            (self.chkMirrorStructure, self.tr("Replica struttura su disco")),
        )
        for widget, title in targets:
            found = self._find_containing_layout(widget)
            if found is None:
                continue
            layout, idx = found

            # Reset checkbox padding (no extra gap before the ⓘ — we want
            # the ⓘ visually attached to its own checkbox).
            widget.setStyleSheet("")

            help_btn = QToolButton(widget.parentWidget())
            help_btn.setText("ⓘ")
            help_btn.setAutoRaise(True)
            help_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            help_btn.setToolTip(self.tr("Mostra spiegazione dettagliata"))
            help_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            help_btn.setStyleSheet(
                "QToolButton { padding: 0 4px; "
                "color: palette(highlight); font-weight: bold; }"
            )
            tip_html = widget.toolTip()
            help_btn.clicked.connect(
                lambda _checked=False, t=title, body=tip_html: QMessageBox.information(
                    self, t, body
                )
            )
            # Layout: <checkbox><tiny gap><ⓘ><big gap><next item>.
            # The small gap keeps ⓘ visually paired with its checkbox;
            # the big gap separates the (checkbox+ⓘ) group from the next.
            layout.insertSpacing(idx + 1, 2)
            layout.insertWidget(idx + 2, help_btn)
            layout.insertSpacing(idx + 3, 24)

    def _wire_signals(self) -> None:
        self.btnBrowseFolder.clicked.connect(self._browse_input_folder)
        self.btnBrowseOutput.clicked.connect(self._browse_output)
        self.btnRun.clicked.connect(self._run)
        self.btnCancel.clicked.connect(self._cancel)
        self.btnClose.clicked.connect(self.reject)
        self.btnSelectAll.clicked.connect(lambda: self._set_all_checked(True))
        self.btnSelectNone.clicked.connect(lambda: self._set_all_checked(False))
        self.btnSelectVisible.clicked.connect(self._select_visible_layers)
        self.btnOpenOutput.clicked.connect(self._open_output_folder)
        self.btnOpenReport.clicked.connect(self._reopen_report)

        # Live-update of Run button state and re-suggest output on tab switch.
        self.tabWidget.currentChanged.connect(self._on_tab_changed)
        self.edtFolder.textChanged.connect(self._on_input_folder_changed)
        self.edtOutput.textChanged.connect(self._refresh_run_state)
        self.lstProjectLayers.itemChanged.connect(self._on_project_layer_toggled)
        self.cmbGrouping.currentIndexChanged.connect(self._on_grouping_changed)
        self.chkMirrorStructure.toggled.connect(self._on_mirror_toggled)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _restore_settings(self) -> None:
        s = QSettings()
        self._restoring_settings = True
        self.edtFolder.setText(s.value(SETTINGS_PREFIX + "folder", "", type=str))
        self._restoring_settings = False
        self.chkMirrorStructure.setChecked(s.value(SETTINGS_PREFIX + "mirror", False, type=bool))
        self.chkSaveStyles.setChecked(s.value(SETTINGS_PREFIX + "save_styles", True, type=bool))
        self.chkValidate.setChecked(s.value(SETTINGS_PREFIX + "validate", False, type=bool))
        self.chkDryRun.setVisible(False)
        # Restore the saved strategy by its constant value (not combo index,
        # which now varies depending on the active tab).
        gi = s.value(SETTINGS_PREFIX + "grouping", GROUPING_ALL_IN_ONE, type=int)
        for i in range(self.cmbGrouping.count()):
            if self.cmbGrouping.itemData(i) == gi:
                self.cmbGrouping.setCurrentIndex(i)
                break
        crs_authid = s.value(SETTINGS_PREFIX + "crs_authid", "", type=str)
        if crs_authid:
            crs = QgsCoordinateReferenceSystem(crs_authid)
            if crs.isValid():
                self.crsSelector.setCrs(crs)
        # Raster options.
        tile_fmt = s.value(SETTINGS_PREFIX + "tile_format", "AUTO", type=str)
        idx = self.cmbTileFormat.findText(tile_fmt)
        if idx >= 0:
            self.cmbTileFormat.setCurrentIndex(idx)
        tile_sz = s.value(SETTINGS_PREFIX + "tile_size", "256", type=str)
        idx = self.cmbTileSize.findText(tile_sz)
        if idx >= 0:
            self.cmbTileSize.setCurrentIndex(idx)
        self.spnJpegQuality.setValue(s.value(SETTINGS_PREFIX + "jpeg_quality", 75, type=int))

    def _save_settings(self) -> None:
        s = QSettings()
        s.setValue(SETTINGS_PREFIX + "folder", self.edtFolder.text())
        s.setValue(SETTINGS_PREFIX + "mirror", self.chkMirrorStructure.isChecked())
        s.setValue(SETTINGS_PREFIX + "save_styles", self.chkSaveStyles.isChecked())
        s.setValue(SETTINGS_PREFIX + "validate", self.chkValidate.isChecked())
        s.setValue(SETTINGS_PREFIX + "grouping", self._current_grouping_constant())
        crs = self.crsSelector.crs()
        s.setValue(
            SETTINGS_PREFIX + "crs_authid",
            crs.authid() if crs.isValid() else "",
        )
        s.setValue(SETTINGS_PREFIX + "tile_format", self.cmbTileFormat.currentText())
        s.setValue(SETTINGS_PREFIX + "tile_size", self.cmbTileSize.currentText())
        s.setValue(SETTINGS_PREFIX + "jpeg_quality", self.spnJpegQuality.value())

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_settings()
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        """
        Refresh the project layer list every time the dialog is reopened.

        The dialog is a singleton (created once, then re-shown) so without
        this hook the list would be a snapshot of the moment of creation —
        including, for example, layers that previous conversions added to
        the project, leading to accidental re-conversion of outputs.
        """
        super().showEvent(event)
        self._populate_project_layers()
        self._refresh_run_state()

    # ------------------------------------------------------------------
    # Dynamic UI (output label, run-button enabled state)
    # ------------------------------------------------------------------

    def _on_project_layer_toggled(self, _item) -> None:
        self._refresh_run_state()
        self._update_raster_options_visibility()

    def _on_input_folder_changed(self, _text: str) -> None:
        if not self._restoring_settings:
            self._force_suggest_output()
            self.tblFolderPreview.setRowCount(0)
            self._scan_results = []
        self._refresh_run_state()

    def _on_mirror_toggled(self, _checked: bool) -> None:
        if self.edtFolder.text().strip():
            self._scan_folder()

    def _on_grouping_changed(self, _index: int) -> None:
        self._update_output_label()
        self._maybe_suggest_output()
        self._refresh_run_state()

    def _on_tab_changed(self, _index: int) -> None:
        """
        When the user switches tabs, refresh:
          * the grouping combo (different strategies are valid per tab)
          * the "Save styles" checkbox visibility (only meaningful in
            project mode; folder mode would just persist QGIS default
            styles, which is noise)
          * the suggested output filename
          * the Run button enabled state
        """
        self._populate_grouping_combo()
        self._apply_tab_visibility()
        self._update_output_label()
        self._force_suggest_output()
        self._refresh_run_state()

    def _apply_tab_visibility(self) -> None:
        """Show/hide tab-specific controls in the shared options pane."""
        on_project = self.tabWidget.currentWidget() is self.tabFromProject
        # "Salva stili" è significativa solo nel tab progetto:
        # in modalità cartella i sorgenti raramente hanno .qml sidecar,
        # e salvare lo stile di default di QGIS è inutile.
        self.chkSaveStyles.setVisible(on_project)
        self._update_raster_options_visibility()

    def _update_raster_options_visibility(self) -> None:
        """Show raster option widgets only when raster items are present."""
        has_raster = False
        if self.tabWidget.currentWidget() is self.tabFromProject:
            # Check if any checked layer is a raster.
            project = QgsProject.instance()
            for i in range(self.lstProjectLayers.count()):
                wi = self.lstProjectLayers.item(i)
                if wi.checkState() != CHECKED:
                    continue
                layer_id = wi.data(Qt.ItemDataRole.UserRole)
                layer = project.mapLayer(layer_id) if (project and layer_id) else None
                if layer is not None and layer.type() == QgsMapLayer.RasterLayer:
                    has_raster = True
                    break
        else:
            has_raster = any(
                it.get("item_type") == "raster" for it in self._scan_results
            )
        for w in (
            self.lblRasterOptions, self.lblTileFormat, self.cmbTileFormat,
            self.lblTileSize, self.cmbTileSize,
            self.lblJpegQuality, self.spnJpegQuality,
        ):
            w.setVisible(has_raster)

    def _maybe_suggest_output(self) -> None:
        """
        Pre-fill the output field with a sensible default when empty
        and the strategy is "all-in-one". Never overwrites a value the
        user already typed — the field stays fully editable.
        """
        if self.edtOutput.text().strip():
            return
        if not _is_file_strategy(self._current_grouping_constant()):
            return
        suggested = self._suggested_output_path()
        if suggested:
            self.edtOutput.setText(str(suggested))

    def _force_suggest_output(self) -> None:
        """Re-suggest the output path when the input or tab changes."""
        on_folder_tab = self.tabWidget.currentWidget() is self.tabFromFolder
        if _is_file_strategy(self._current_grouping_constant()):
            suggested = self._suggested_output_path()
            self.edtOutput.setText(str(suggested) if suggested else "")
        elif on_folder_tab:
            self.edtOutput.setText(self.edtFolder.text().strip())
        else:
            project = QgsProject.instance()
            if project and project.fileName():
                self.edtOutput.setText(str(Path(project.fileName()).parent))
            else:
                self.edtOutput.setText(str(Path.home()))

    def _suggested_output_path(self) -> Optional[Path]:
        """
        Pick a default `.gpkg` path based on the active source.

        On the project tab the suggested name comes from the QGIS project
        name (file stem, project title, or "untitled_project" as last
        resort). On the folder tab it comes from the input folder name.
        """
        on_folder_tab = self.tabWidget.currentWidget() is self.tabFromFolder
        if on_folder_tab:
            folder_text = self.edtFolder.text().strip()
            if folder_text:
                folder = Path(folder_text)
                if folder.is_dir():
                    return folder / f"{folder.name}.gpkg"
            return None
        # Project tab — derive from the QGIS project.
        project = QgsProject.instance()
        if project is not None:
            project_path = project.fileName()
            if project_path:
                ppath = Path(project_path)
                stem = ppath.stem or "geopackage_converter_output"
                return ppath.parent / f"{stem}.gpkg"
            # Project not saved yet: try the user-set project title,
            # then a friendly default. Either way, drop the file in the
            # user's home so it's writable without further setup.
            title = (project.title() or project.baseName() or "").strip()
            stem = title or "untitled_project"
            # Make the stem filesystem-safe.
            for ch in '<>:"/\\|?*':
                stem = stem.replace(ch, "_")
            return Path.home() / f"{stem}.gpkg"
        return Path.home() / "geopackage_converter_output.gpkg"

    def _update_output_label(self) -> None:
        if _is_file_strategy(self._current_grouping_constant()):
            self.lblOutput.setText(self.tr("GeoPackage di output:"))
            self.edtOutput.setPlaceholderText(self.tr("percorso/al/file.gpkg"))
        else:
            self.lblOutput.setText(self.tr("Cartella di output:"))
            self.edtOutput.setPlaceholderText(
                self.tr("cartella dove generare i .gpkg multipli")
            )

    def _has_inputs(self) -> bool:
        if self.tabWidget.currentWidget() is self.tabFromProject:
            return any(
                self.lstProjectLayers.item(i).checkState() == CHECKED
                and self.lstProjectLayers.item(i).data(Qt.ItemDataRole.UserRole) is not None
                for i in range(self.lstProjectLayers.count())
            )
        return bool(self._scan_results)

    def _refresh_run_state(self) -> None:
        valid = self._has_inputs() and bool(self.edtOutput.text().strip())
        self.btnRun.setEnabled(valid and self._task is None)

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _set_all_checked(self, checked: bool) -> None:
        state = CHECKED if checked else UNCHECKED
        for i in range(self.lstProjectLayers.count()):
            item = self.lstProjectLayers.item(i)
            if item.flags() & ITEM_IS_USER_CHECKABLE:
                item.setCheckState(state)
        self._refresh_run_state()
        self._update_raster_options_visibility()

    def _select_visible_layers(self) -> None:
        """
        Check only the layers currently *visible* on the map canvas.

        Visibility is resolved through the layer tree, not the layer
        itself: ``QgsLayerTreeNode.isVisible()`` returns True only when
        the layer's own checkbox is on **and** every ancestor group is
        checked too — i.e. exactly the layers the user actually sees.
        Layers without a tree node (e.g. added with
        ``addToLegend=False``) are treated as not visible.
        Everything else gets unchecked.
        """
        project = QgsProject.instance()
        root = project.layerTreeRoot() if project else None
        visible_count = 0
        for i in range(self.lstProjectLayers.count()):
            item = self.lstProjectLayers.item(i)
            if not (item.flags() & ITEM_IS_USER_CHECKABLE):
                continue
            layer_id = item.data(Qt.ItemDataRole.UserRole)
            visible = False
            if root is not None and layer_id:
                node = root.findLayer(layer_id)
                visible = bool(node is not None and node.isVisible())
            item.setCheckState(CHECKED if visible else UNCHECKED)
            if visible:
                visible_count += 1
        self._log(
            self.tr("Selezionati {n} layer attivi").format(n=visible_count)
        )
        self._refresh_run_state()
        self._update_raster_options_visibility()

    def _browse_input_folder(self) -> None:
        start = self.edtFolder.text() or ""
        folder = QFileDialog.getExistingDirectory(self, self.tr("Seleziona cartella"), start)
        if folder:
            self.edtFolder.setText(folder)
            self._force_suggest_output()
            self._scan_folder()

    def _browse_output(self) -> None:
        start = self.edtOutput.text() or ""
        if _is_file_strategy(self._current_grouping_constant()):
            path, _ = QFileDialog.getSaveFileName(
                self,
                self.tr("Seleziona GeoPackage di output"),
                start,
                "GeoPackage (*.gpkg)",
            )
            if path:
                if not path.lower().endswith(".gpkg"):
                    path += ".gpkg"
                self.edtOutput.setText(path)
        else:
            folder = QFileDialog.getExistingDirectory(
                self, self.tr("Seleziona cartella di output"), start
            )
            if folder:
                self.edtOutput.setText(folder)

    def _log(self, message: str) -> None:
        self.txtLog.appendPlainText(message)
        QgsMessageLog.logMessage(message, LOG_TAG)

    # ------------------------------------------------------------------
    # Folder scan (in QgsTask)
    # ------------------------------------------------------------------

    def _scan_folder(self) -> None:
        if self.tabWidget.currentWidget() is not self.tabFromFolder:
            return
        folder = self.edtFolder.text().strip()
        if not folder or not QFileInfo(folder).isDir():
            return
        self._log(self.tr("Scansione in corso: {f}").format(f=folder))
        self.tblFolderPreview.setRowCount(0)
        self._scan_results = []
        self._refresh_run_state()

        self._scan_task = _ScanTask(
            Path(folder),
            self.chkMirrorStructure.isChecked(),
        )
        self._scan_task.taskCompleted.connect(self._on_scan_done)
        self._scan_task.taskTerminated.connect(self._on_scan_failed)
        QgsApplication.taskManager().addTask(self._scan_task)

    def _on_scan_done(self) -> None:
        if self._scan_task is None:
            return
        results = self._scan_task.results
        self._scan_results = results
        self._populate_preview_table(results)
        self._scan_task = None
        if not results:
            self._log(self.tr("Nessun file supportato trovato."))
            QMessageBox.warning(
                self, self.tr("Cartella vuota"),
                self.tr(
                    "La cartella selezionata non contiene file supportati.\n\n"
                    "Vettoriali: SHP, TAB, KML, GML, GeoJSON, DXF, GPX, MIF, ZIP\n"
                    "Raster: GeoTIFF, JP2, ECW, IMG, ASC, VRT"
                ),
            )
        else:
            n_vec = sum(1 for r in results if r.get("item_type", "vector") != "raster")
            n_ras = sum(1 for r in results if r.get("item_type") == "raster")
            parts = []
            if n_vec:
                parts.append(self.tr("{n} vettoriali").format(n=n_vec))
            if n_ras:
                parts.append(self.tr("{n} raster").format(n=n_ras))
            self._log(self.tr("Trovati {detail}").format(detail=", ".join(parts) or str(len(results))))
        self._maybe_suggest_output()
        self._refresh_run_state()
        self._update_raster_options_visibility()

    def _on_scan_failed(self) -> None:
        self._scan_task = None
        self._log(self.tr("Scansione interrotta o fallita."))

    def _populate_preview_table(self, items: List[dict]) -> None:
        self.tblFolderPreview.setRowCount(len(items))
        for row, it in enumerate(items):
            # For zip entries decorate name/format and use inner path as tooltip.
            display_name = it.get("name", "")
            display_format = it.get("format", "")
            tooltip = ""
            if it.get("is_virtual"):
                archive = it.get("archive")
                inner = it.get("inner_path", "")
                display_format = f"{display_format} (zip)"
                if archive:
                    tooltip = f"{archive} → {inner}"
            # Raster items show dimensions instead of feature count / encoding.
            is_raster = it.get("item_type") == "raster"
            if is_raster:
                w = it.get("raster_width", 0)
                h = it.get("raster_height", 0)
                bands = it.get("band_count", 0)
                dim_str = f"{w}×{h}, {bands} {'banda' if bands == 1 else 'bande'}" if w else ""
                feat_cell = dim_str
                enc_cell = "N/A"
            else:
                fc = it.get("feature_count", 0)
                feat_cell = f"{fc} feat." if fc else ""
                enc_cell = it.get("encoding", "")
            cells = [
                display_name,
                display_format,
                it.get("geometry_type", ""),
                it.get("crs", ""),
                feat_cell,
                enc_cell,
                "; ".join(it.get("warnings", []) or []),
            ]
            for col, text in enumerate(cells):
                cell = QTableWidgetItem(str(text))
                if tooltip:
                    cell.setToolTip(tooltip)
                self.tblFolderPreview.setItem(row, col, cell)
        self.tblFolderPreview.resizeColumnsToContents()

    # ------------------------------------------------------------------
    # Run / cancel
    # ------------------------------------------------------------------

    def _collect_items(self) -> List[dict]:
        if self.tabWidget.currentWidget() is self.tabFromProject:
            return self._items_from_project()
        # Folder mode: clear any project-mode snapshot so
        # _add_layers_to_project uses the folder layout instead.
        self._original_tree_snapshot = []
        return list(self._scan_results)

    def _items_from_project(self) -> List[dict]:
        items = []
        project = QgsProject.instance()
        root = project.layerTreeRoot() if project else None
        # Capture the original tree structure for later reloading.
        self._original_tree_snapshot = self._snapshot_layer_tree(root) if root else []
        for i in range(self.lstProjectLayers.count()):
            wi = self.lstProjectLayers.item(i)
            if wi.checkState() != CHECKED:
                continue
            layer_id = wi.data(Qt.ItemDataRole.UserRole)
            layer = project.mapLayer(layer_id) if (project and layer_id) else None
            if layer is None:
                continue
            legend_group = ""
            if root is not None:
                node = root.findLayer(layer.id())
                if node is not None and node.parent() is not None and node.parent() != root:
                    legend_group = node.parent().name() or ""
            if layer.type() == QgsMapLayer.RasterLayer:
                items.append(self._build_raster_project_item(layer, legend_group))
            elif isinstance(layer, QgsVectorLayer):
                items.append(self._build_project_item(layer, legend_group))
        return items

    @staticmethod
    def _snapshot_layer_tree(root) -> list:
        """
        Walk the QGIS layer tree and return a lightweight ordered list
        describing every checked layer's position.

        Each entry is a dict:
          {"name": <layer display name>,
           "group": <parent group name or "">,
           "visible": bool}

        Groups appear in the same top-to-bottom order as the legend.
        This snapshot is used after conversion to reload the GPKG layers
        in the same visual order the user had in the project.
        """
        from qgis.core import QgsLayerTree, QgsLayerTreeLayer, QgsLayerTreeGroup

        snapshot: list = []
        for child in root.children():
            if isinstance(child, QgsLayerTreeGroup) and not isinstance(child, QgsLayerTreeLayer):
                group_name = child.name()
                for sub in child.children():
                    if isinstance(sub, QgsLayerTreeLayer) and sub.layer():
                        snapshot.append({
                            "name": sub.layer().name(),
                            "group": group_name,
                            "visible": sub.isVisible(),
                            "layer_id": sub.layerId(),
                        })
            elif isinstance(child, QgsLayerTreeLayer) and child.layer():
                snapshot.append({
                    "name": child.layer().name(),
                    "group": "",
                    "visible": child.isVisible(),
                    "layer_id": child.layerId(),
                })
        return snapshot

    @staticmethod
    def _build_project_item(layer, legend_group: str) -> dict:
        """
        Build a converter-friendly item dict from a QgsVectorLayer.

        Detects GDAL virtual sources (`/vsizip/`, `/vsicurl/`, ...) so the
        converter doesn't try to `.exists()` them on the local filesystem,
        and snapshots the *current* style XML so the converter can copy
        the user's personalisation instead of re-exporting a fresh default.
        """
        full_source = layer.source()
        raw_source = full_source.split("|", 1)[0]

        # GDAL virtual filesystem prefixes. QGIS may store them with either
        # slash style on Windows; normalise to forward slashes for OGR.
        is_virtual = False
        uri = None
        normalised = raw_source.replace("\\", "/")
        for prefix in ("/vsizip/", "/vsigzip/", "/vsitar/", "/vsicurl/", "/vsis3/"):
            if normalised.startswith(prefix) or normalised.lstrip("/").startswith(prefix.lstrip("/")):
                is_virtual = True
                if not normalised.startswith("/"):
                    normalised = "/" + normalised
                uri = normalised
                break

        # For multi-layer sources (GeoPackage, SpatiaLite, etc.),
        # preserve the full URI so the converter opens the correct layer.
        source_for_converter = raw_source
        if not is_virtual and "|layername=" in full_source:
            source_for_converter = full_source

        item = {
            "path": Path(raw_source),
            "name": layer.name(),
            "item_type": "vector",
            "crs": layer.crs().authid() if layer.crs().isValid() else "Unknown",
            "encoding": layer.dataProvider().encoding() if layer.dataProvider() else "UTF-8",
            "legend_group": legend_group,
            "uri": source_for_converter,
        }
        if is_virtual:
            item["is_virtual"] = True
            item["uri"] = uri

        # Snapshot the live style XML so the converter can persist *the
        # user's symbology*, not a default re-derived from the file.
        try:
            from qgis.PyQt.QtXml import QDomDocument

            doc = QDomDocument()
            layer.exportNamedStyle(doc)
            xml = doc.toString()
            if xml:
                item["style_xml"] = xml
        except Exception:  # noqa: BLE001 - style snapshot is best-effort
            pass
        return item

    @staticmethod
    def _build_raster_project_item(layer, legend_group: str) -> dict:
        """Build a converter-friendly item dict from a QgsRasterLayer.

        Detects GDAL virtual sources (``/vsizip/``, ``/vsicurl/``, …)
        so the raster converter doesn't trip on ``.exists()`` checks.
        """
        full_source = layer.source()
        raw_source = full_source.split("|", 1)[0]

        is_virtual = False
        uri = None
        normalised = raw_source.replace("\\", "/")
        for prefix in ("/vsizip/", "/vsigzip/", "/vsitar/", "/vsicurl/", "/vsis3/"):
            if normalised.startswith(prefix) or normalised.lstrip("/").startswith(prefix.lstrip("/")):
                is_virtual = True
                if not normalised.startswith("/"):
                    normalised = "/" + normalised
                uri = normalised
                break

        crs = layer.crs()
        item: dict = {
            "path": Path(raw_source),
            "name": layer.name(),
            "item_type": "raster",
            "crs": crs.authid() if crs.isValid() else "Unknown",
            "encoding": "N/A",
            "legend_group": legend_group,
            "geometry_type": "Raster",
            "raster_width": layer.width(),
            "raster_height": layer.height(),
            "band_count": layer.bandCount(),
        }
        if is_virtual:
            item["is_virtual"] = True
            item["uri"] = uri
        return item

    def _run(self) -> None:
        items = self._collect_items()
        if not items:
            on_folder = self.tabWidget.currentWidget() is self.tabFromFolder
            if on_folder:
                msg = self.tr(
                    "La cartella non contiene file supportati (vettoriali o raster)."
                )
            else:
                msg = self.tr("Seleziona almeno un layer.")
            QMessageBox.information(
                self, self.tr("Nessun elemento"), msg,
            )
            return
        output = self.edtOutput.text().strip()
        if not output:
            return  # button should be disabled
        out_path = Path(output)
        if _is_file_strategy(self._current_grouping_constant()):
            if out_path.suffix.lower() != ".gpkg":
                out_path = out_path.with_suffix(".gpkg")
        else:
            # Strategie multi-file: out_path è una cartella; creala se manca.
            try:
                out_path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.critical(
                    self, self.tr("Errore output"),
                    self.tr("Impossibile creare la cartella: {e}").format(e=exc),
                )
                return

        crs = self.crsSelector.crs()
        target_crs = crs.authid() if crs.isValid() else None
        # "Salva stili" is meaningful only in project mode; in folder mode
        # we'd persist QGIS default styles, which are noise.
        on_project = self.tabWidget.currentWidget() is self.tabFromProject
        save_styles = on_project and self.chkSaveStyles.isChecked()
        converter = Converter(
            target_crs=target_crs,
            save_styles=save_styles,
            validate_geometries=self.chkValidate.isChecked(),
            dry_run=False,
        )
        # Raster converter: always created so mixed bundles work.
        raster_conv = RasterConverter(
            target_crs=target_crs,
            tile_format=self.cmbTileFormat.currentText(),
            tile_size=int(self.cmbTileSize.currentText()),
            jpeg_quality=self.spnJpegQuality.value(),
            dry_run=False,
        )
        # Derive a project-based fallback name for ungrouped layers.
        _proj = QgsProject.instance()
        _proj_name = ""
        if on_project and _proj:
            _proj_name = (
                Path(_proj.fileName()).stem if _proj.fileName()
                else _proj.title() or ""
            )
        self._task = _ConversionTask(
            items=items,
            output_path=out_path,
            grouping_index=self._current_grouping_constant(),
            converter=converter,
            mirror_layout=self.chkMirrorStructure.isChecked(),
            raster_converter=raster_conv,
            project_name=_proj_name,
        )
        self._task.progressChanged.connect(self._on_progress)
        self._task.taskCompleted.connect(self._on_task_completed)
        self._task.taskTerminated.connect(self._on_task_terminated)
        self._set_running(True)
        self._save_settings()
        self._log(self.tr("Avvio conversione…"))
        QgsApplication.taskManager().addTask(self._task)

    def _cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self.btnCancel.setEnabled(False)
            self._log(self.tr("Annullamento richiesto…"))

    def _set_running(self, running: bool) -> None:
        self.btnRun.setEnabled(not running and self._has_inputs() and bool(self.edtOutput.text().strip()))
        self.btnCancel.setEnabled(running)
        self.tabWidget.setEnabled(not running)
        self.grpOptions.setEnabled(not running)

    # ------------------------------------------------------------------
    # Task callbacks
    # ------------------------------------------------------------------

    def _on_progress(self, value: float) -> None:
        self.prgConversion.setValue(int(value))
        self._drain_log()

    def _on_task_completed(self) -> None:
        self._drain_log()
        if self._task is not None and self._task.result is not None:
            r = self._task.result
            self._log(
                self.tr("Completato: {ok} riusciti, {ko} errori, {w} avvisi").format(
                    ok=r.success_count, ko=r.error_count, w=len(r.warnings)
                )
            )
            if r.output_files:
                self._last_output_dir = r.output_files[0].parent
                self.btnOpenOutput.setEnabled(True)
            if self._task.report_path and self._task.report_path.exists():
                self._last_report = self._task.report_path
                self.btnOpenReport.setEnabled(True)
            # Show a non-intrusive notification in the QGIS message bar.
            self._show_qgis_notification(r)
            # Offer to add the freshly written GeoPackages to the project.
            if r.success_count > 0 and r.output_files and not r.dry_run:
                self._maybe_add_to_project(r.output_layers)
        self._task = None
        self._set_running(False)

    def _show_qgis_notification(self, result) -> None:
        """
        Push a brief, non-blocking message to the QGIS message bar at
        the top of the canvas. Includes an "Apri report" button when a
        report was generated. Replaces the previous auto-open of the
        report HTML in the browser.
        """
        if self.iface is None:
            return

        if result.error_count == 0:
            level = Qgis.MessageLevel.Success
            title = self.tr("GeoPackage Converter")
            message = self.tr("Conversione completata: {ok} layer in {nf} file").format(
                ok=result.success_count, nf=len(result.output_files)
            )
            duration = 8
        elif result.success_count == 0:
            level = Qgis.MessageLevel.Critical
            title = self.tr("GeoPackage Converter")
            message = self.tr("Conversione fallita: {ko} errori").format(
                ko=result.error_count
            )
            duration = 12
        else:
            level = Qgis.MessageLevel.Warning
            title = self.tr("GeoPackage Converter")
            message = self.tr(
                "Conversione parziale: {ok} riusciti, {ko} errori"
            ).format(ok=result.success_count, ko=result.error_count)
            duration = 12

        try:
            bar = self.iface.messageBar()
            widget = bar.createMessage(title, message)
            # Add an "Apri report" button when we actually have a report.
            if self._last_report and self._last_report.exists():
                from qgis.PyQt.QtWidgets import QPushButton

                btn = QPushButton(self.tr("Apri report"))
                btn.clicked.connect(lambda: self._open_url(self._last_report))
                widget.layout().addWidget(btn)
            bar.pushWidget(widget, level, duration)
        except Exception:  # noqa: BLE001 - fallback to a plain message
            self.iface.messageBar().pushMessage(
                title, message, level=level, duration=duration
            )

    def _maybe_add_to_project(self, output_layers: list) -> None:
        """Ask the user whether to load the produced .gpkg layers into QGIS.

        `output_layers` is a list of (gpkg_path, layer_name) tuples — only
        the layers actually written in this run, NOT every layer that
        happens to be in the GeoPackage (which may include leftovers from
        previous conversions to the same file).
        """
        if not output_layers:
            return
        # Group layer names by GPKG path while preserving insertion order.
        per_file_map: dict = {}
        for gpkg_path, layer_name in output_layers:
            per_file_map.setdefault(gpkg_path, []).append(layer_name)
        per_file = list(per_file_map.items())
        total_layers = sum(len(names) for _, names in per_file)
        n_files = len(per_file)

        message = self.tr(
            "La conversione ha prodotto {n_files} file GeoPackage "
            "con un totale di {n_layers} layer.\n\n"
            "Vuoi caricare i layer nel progetto QGIS attivo?"
        ).format(n_files=n_files, n_layers=total_layers)
        reply = QMessageBox.question(
            self,
            self.tr("Caricare i layer nel progetto?"),
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        added, failed = self._add_layers_to_project(per_file)
        # Hide the original project layers so only the converted ones
        # are visible — avoids visual duplication.
        self._hide_original_layers()
        self._log(
            self.tr("Layer aggiunti al progetto: {added}, falliti: {failed}").format(
                added=added, failed=failed
            )
        )

    def _hide_original_layers(self) -> None:
        """Turn off visibility of every original layer that was converted."""
        snapshot = getattr(self, "_original_tree_snapshot", [])
        if not snapshot:
            return
        project = QgsProject.instance()
        root = project.layerTreeRoot() if project else None
        if root is None:
            return
        converted_names = {e["name"] for e in snapshot}
        for child in root.children():
            # Skip the "GeoPackage Converter" group we just created.
            if hasattr(child, "name") and child.name() == "GeoPackage Converter":
                continue
            self._set_subtree_visibility(child, converted_names)

    @staticmethod
    def _set_subtree_visibility(node, names_to_hide: set) -> None:
        """Recursively hide layers whose name is in *names_to_hide*."""
        from qgis.core import QgsLayerTreeLayer, QgsLayerTreeGroup

        if isinstance(node, QgsLayerTreeLayer):
            if node.layer() and node.layer().name() in names_to_hide:
                node.setItemVisibilityChecked(False)
        elif isinstance(node, QgsLayerTreeGroup):
            for child in node.children():
                GeoPackageConverterDialog._set_subtree_visibility(child, names_to_hide)
            # If all children are now hidden, hide the group too.
            if all(
                not c.isVisible() for c in node.children()
            ):
                node.setItemVisibilityChecked(False)

    @staticmethod
    def _list_gpkg_layers(gpkg_path: Path) -> list:
        """Return the list of vector layer names inside a GeoPackage."""
        import sqlite3

        try:
            with sqlite3.connect(str(gpkg_path)) as con:
                rows = con.execute(
                    "SELECT table_name FROM gpkg_contents WHERE data_type='features' "
                    "ORDER BY table_name"
                ).fetchall()
            return [r[0] for r in rows]
        except sqlite3.DatabaseError:
            return []

    @staticmethod
    def _list_gpkg_raster_layers(gpkg_path: Path) -> list:
        """Return the list of raster table names inside a GeoPackage."""
        import sqlite3

        try:
            with sqlite3.connect(str(gpkg_path)) as con:
                rows = con.execute(
                    "SELECT table_name FROM gpkg_contents "
                    "WHERE data_type IN ('tiles', '2d-gridded-coverage') "
                    "ORDER BY table_name"
                ).fetchall()
            return [r[0] for r in rows]
        except sqlite3.DatabaseError:
            return []

    def _add_layers_to_project(self, per_file: list) -> tuple:
        """
        Load each sublayer of every GPKG and return (added, failed) counts.

        When a tree snapshot was captured (project mode), the converted
        layers are added in the **exact same order** with the same group
        structure and visibility as the original project.  Otherwise
        falls back to the folder-based layout.
        """
        project = QgsProject.instance()
        if project is None:
            return 0, 0
        root = project.layerTreeRoot()
        if root is None:
            return 0, 0

        # If we have a tree snapshot from project mode, use it.
        snapshot = getattr(self, "_original_tree_snapshot", [])
        if snapshot:
            return self._add_layers_from_snapshot(per_file, root, project, snapshot)

        # Folder mode: use the generic layout approach.
        parent_group = root.insertGroup(0, "GeoPackage Converter")
        if len(per_file) <= 1:
            return self._add_layers_flat(per_file, parent_group)
        return self._add_layers_by_folder(per_file, parent_group)

    def _add_layers_from_snapshot(self, per_file, root, project, snapshot) -> tuple:
        """
        Recreate the original project layer tree inside a
        'GeoPackage Converter' parent group, preserving the exact
        order, grouping and visibility from the snapshot.
        """
        # Build a lookup: layer_name -> (gpkg_path, is_raster).
        layer_lookup: dict = {}
        raster_cache: dict = {}
        for gpkg_path, layer_names in per_file:
            if gpkg_path not in raster_cache:
                raster_cache[gpkg_path] = set(self._list_gpkg_raster_layers(gpkg_path))
            raster_set = raster_cache[gpkg_path]
            for name in layer_names:
                layer_lookup[name] = (gpkg_path, name in raster_set)

        parent_group = root.insertGroup(0, "GeoPackage Converter")
        group_cache: dict = {}
        added = 0
        failed = 0

        for entry in snapshot:
            layer_name = entry["name"]
            group_name = entry["group"]
            visible = entry["visible"]

            if layer_name not in layer_lookup:
                continue  # layer was not selected for conversion

            gpkg_path, is_raster = layer_lookup[layer_name]
            layer = self._load_gpkg_layer(gpkg_path, layer_name, is_raster)
            if layer is None or not layer.isValid():
                failed += 1
                continue
            try:
                layer.loadDefaultStyle()
            except Exception:  # noqa: BLE001
                pass
            # Raster styles cannot be saved inside GeoPackage (GDAL
            # limitation).  Copy the full style (renderer, opacity,
            # resampling, brightness/contrast …) from the original
            # project layer so the visual appearance is preserved.
            if is_raster:
                orig_id = entry.get("layer_id", "")
                orig = project.mapLayer(orig_id) if orig_id else None
                if orig:
                    from qgis.PyQt.QtXml import QDomDocument
                    doc = QDomDocument("qgis")
                    if not orig.exportNamedStyle(doc):
                        layer.importNamedStyle(doc)
            project.addMapLayer(layer, addToLegend=False)

            if group_name:
                if group_name not in group_cache:
                    group_cache[group_name] = parent_group.addGroup(group_name)
                target = group_cache[group_name]
            else:
                target = parent_group

            node = target.addLayer(layer)
            node.setItemVisibilityChecked(visible)
            added += 1

        return added, failed

    def _add_layers_by_folder(self, per_file, parent_group) -> tuple:
        """Multiple GPKGs from folder mode: mirror the folder layout."""
        project = QgsProject.instance()
        paths = [p for p, _ in per_file]
        try:
            common = Path(os.path.commonpath([str(p.parent) for p in paths]))
        except ValueError:
            common = None

        added = 0
        failed = 0
        group_cache: dict = {}

        raster_tables = {}
        for gpkg_path, layer_names in per_file:
            if not layer_names:
                continue
            if common is not None:
                try:
                    rel = gpkg_path.relative_to(common)
                except ValueError:
                    rel = Path(gpkg_path.name)
            else:
                rel = Path(gpkg_path.name)
            parents = list(rel.parts[:-1])
            if parents and gpkg_path.stem == parents[-1]:
                chain = parents
            else:
                chain = parents + [gpkg_path.stem]

            target = self._ensure_group_chain(parent_group, chain, group_cache)
            if gpkg_path not in raster_tables:
                raster_tables[gpkg_path] = set(self._list_gpkg_raster_layers(gpkg_path))
            raster_set = raster_tables[gpkg_path]
            for layer_name in layer_names:
                layer = self._load_gpkg_layer(gpkg_path, layer_name, layer_name in raster_set)
                if layer is None or not layer.isValid():
                    failed += 1
                    continue
                try:
                    layer.loadDefaultStyle()
                except Exception:  # noqa: BLE001
                    pass
                project.addMapLayer(layer, addToLegend=False)
                target.addLayer(layer)
                added += 1
        return added, failed

    def _add_layers_flat(self, per_file: list, parent_group) -> tuple:
        """Add every sublayer under the parent group (single GPKG)."""
        project = QgsProject.instance()
        added = 0
        failed = 0
        for gpkg_path, layer_names in per_file:
            raster_set = set(self._list_gpkg_raster_layers(gpkg_path))
            for layer_name in layer_names:
                layer = self._load_gpkg_layer(gpkg_path, layer_name, layer_name in raster_set)
                if layer is None or not layer.isValid():
                    failed += 1
                    continue
                try:
                    layer.loadDefaultStyle()
                except Exception:  # noqa: BLE001
                    pass
                project.addMapLayer(layer, addToLegend=False)
                parent_group.addLayer(layer)
                added += 1
        return added, failed

    @staticmethod
    def _load_gpkg_layer(gpkg_path: Path, layer_name: str, is_raster: bool):
        """Load a single layer from a GPKG, choosing the correct QGIS class."""
        if is_raster:
            # GDAL raster subdataset URI for GeoPackage.
            uri = f"GPKG:{gpkg_path}:{layer_name}"
            return QgsRasterLayer(uri, layer_name, "gdal")
        uri = f"{gpkg_path}|layername={layer_name}"
        return QgsVectorLayer(uri, layer_name, "ogr")

    @staticmethod
    def _ensure_group_chain(root, chain: list, cache: dict):
        """
        Walk/create a chain of nested QGIS layer-tree groups and return
        the deepest. ``cache`` is a per-call dict keyed by the joined
        chain, so sibling files reuse the same parent groups.
        """
        if not chain:
            return root
        current = root
        key_parts: list = []
        for name in chain:
            key_parts.append(name)
            key = "/".join(key_parts)
            cached = cache.get(key)
            if cached is not None:
                current = cached
                continue
            existing = current.findGroup(name)
            current = existing if existing is not None else current.addGroup(name)
            cache[key] = current
        return current

    def _on_task_terminated(self) -> None:
        self._drain_log()
        self._log(self.tr("Conversione interrotta o fallita."))
        self._task = None
        self._set_running(False)

    def _drain_log(self) -> None:
        if self._task is not None:
            for line in self._task.consume_log():
                self.txtLog.appendPlainText(line)

    # ------------------------------------------------------------------
    # Post-run actions
    # ------------------------------------------------------------------

    def _open_output_folder(self) -> None:
        if self._last_output_dir and self._last_output_dir.exists():
            self._open_path_in_explorer(self._last_output_dir)

    def _reopen_report(self) -> None:
        if self._last_report and self._last_report.exists():
            self._open_url(self._last_report)

    @staticmethod
    def _open_url(path: Path) -> None:
        try:
            webbrowser.open(path.resolve().as_uri())
        except Exception as exc:  # noqa: BLE001
            QgsMessageLog.logMessage(f"Cannot open {path}: {exc}", LOG_TAG)

    @staticmethod
    def _open_path_in_explorer(path: Path) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            QgsMessageLog.logMessage(f"Cannot open folder {path}: {exc}", LOG_TAG)
