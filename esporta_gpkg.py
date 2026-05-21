from qgis.core import (
    QgsVectorFileWriter, QgsVectorLayer, QgsProject,
    QgsCoordinateTransformContext, QgsMapLayer
)
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QFileDialog, QLineEdit, QMessageBox,
    QProgressBar, QCheckBox, QGroupBox
)
from qgis.PyQt.QtCore import Qt, QCoreApplication
import os
import tempfile


class GeoPackageExporter(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Esporta Layer in GeoPackage")
        self.setMinimumWidth(520)
        self.setMinimumHeight(580)
        self.gpkg_path = ""
        self.init_ui()
        self.load_layers()

    def init_ui(self):
        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        # --- Output file ---
        grp_file = QGroupBox("File di destinazione")
        file_layout = QHBoxLayout()
        self.txt_path = QLineEdit()
        self.txt_path.setPlaceholderText("Seleziona il percorso del GeoPackage...")
        self.txt_path.setReadOnly(True)
        btn_browse = QPushButton("Sfoglia...")
        btn_browse.clicked.connect(self.scegli_file)
        file_layout.addWidget(self.txt_path)
        file_layout.addWidget(btn_browse)
        grp_file.setLayout(file_layout)
        main_layout.addWidget(grp_file)

        # --- Lista layer ---
        grp_layers = QGroupBox("Layer disponibili")
        layers_layout = QVBoxLayout()

        sel_layout1 = QHBoxLayout()
        btn_all = QPushButton("Seleziona tutti")
        btn_none = QPushButton("Deseleziona tutti")
        btn_all.clicked.connect(self.seleziona_tutti)
        btn_none.clicked.connect(self.deseleziona_tutti)
        sel_layout1.addWidget(btn_all)
        sel_layout1.addWidget(btn_none)
        layers_layout.addLayout(sel_layout1)

        sel_layout2 = QHBoxLayout()
        btn_visible = QPushButton("Solo visibili")
        btn_editing = QPushButton("Solo in editing")
        btn_visible.clicked.connect(self.seleziona_visibili)
        btn_editing.clicked.connect(self.seleziona_in_editing)
        sel_layout2.addWidget(btn_visible)
        sel_layout2.addWidget(btn_editing)
        layers_layout.addLayout(sel_layout2)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.MultiSelection)
        layers_layout.addWidget(self.list_widget)
        grp_layers.setLayout(layers_layout)
        main_layout.addWidget(grp_layers)

        # --- Opzioni ---
        grp_opt = QGroupBox("Opzioni")
        opt_layout = QVBoxLayout()
        self.chk_overwrite = QCheckBox("Sovrascrivi file se esistente")
        self.chk_overwrite.setChecked(True)
        self.chk_selected = QCheckBox("Esporta solo le feature selezionate")
        self.chk_save_styles = QCheckBox("Salva gli stili dei layer nel GeoPackage")
        self.chk_save_styles.setToolTip(
            "Salva lo stile QML di ogni layer nella tabella 'layer_styles' del GeoPackage.\n"
            "Al prossimo caricamento del GPKG, gli stili verranno applicati automaticamente."
        )
        opt_layout.addWidget(self.chk_overwrite)
        opt_layout.addWidget(self.chk_selected)
        opt_layout.addWidget(self.chk_save_styles)
        grp_opt.setLayout(opt_layout)
        main_layout.addWidget(grp_opt)

        # --- Progress bar ---
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setVisible(False)
        main_layout.addWidget(self.progress)

        # --- Label stato ---
        self.lbl_status = QLabel("")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(self.lbl_status)

        # --- Bottoni Export/Chiudi ---
        btn_layout = QHBoxLayout()
        self.btn_export = QPushButton("Esporta")
        self.btn_export.setDefault(True)
        self.btn_export.clicked.connect(self.esporta)
        btn_cancel = QPushButton("Chiudi")
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_export)
        btn_layout.addWidget(btn_cancel)
        main_layout.addLayout(btn_layout)

    # -------------------------------------------------------------------------
    # Caricamento layer
    # -------------------------------------------------------------------------

    def load_layers(self):
        """Carica i layer vettoriali del progetto nella lista."""
        self.list_widget.clear()
        layers = QgsProject.instance().mapLayers().values()
        tree_root = QgsProject.instance().layerTreeRoot()

        for layer in layers:
            if layer.type() != QgsMapLayer.VectorLayer:
                continue
            item = QListWidgetItem(layer.name())
            item.setData(Qt.UserRole, layer.id())
            tree_layer = tree_root.findLayer(layer.id())
            is_visible = tree_layer.isVisible() if tree_layer else False
            item.setCheckState(Qt.Checked if is_visible else Qt.Unchecked)
            self.list_widget.addItem(item)

    # -------------------------------------------------------------------------
    # Selezione file
    # -------------------------------------------------------------------------

    def scegli_file(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Salva GeoPackage", "", "GeoPackage (*.gpkg)"
        )
        if path:
            if not path.endswith('.gpkg'):
                path += '.gpkg'
            self.gpkg_path = path
            self.txt_path.setText(path)

    # -------------------------------------------------------------------------
    # Metodi di selezione rapida
    # -------------------------------------------------------------------------

    def seleziona_tutti(self):
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(Qt.Checked)

    def deseleziona_tutti(self):
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(Qt.Unchecked)

    def seleziona_visibili(self):
        tree_root = QgsProject.instance().layerTreeRoot()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            layer_id = item.data(Qt.UserRole)
            tree_layer = tree_root.findLayer(layer_id)
            is_visible = tree_layer.isVisible() if tree_layer else False
            item.setCheckState(Qt.Checked if is_visible else Qt.Unchecked)

    def seleziona_in_editing(self):
        """Seleziona solo i layer con la matita attiva, visibili o meno."""
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            layer_id = item.data(Qt.UserRole)
            layer = QgsProject.instance().mapLayer(layer_id)
            is_editing = layer.isEditable() if layer else False
            item.setCheckState(Qt.Checked if is_editing else Qt.Unchecked)

    def get_layer_selezionati(self):
        selected = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.Checked:
                layer_id = item.data(Qt.UserRole)
                layer = QgsProject.instance().mapLayer(layer_id)
                if layer:
                    selected.append(layer)
        return selected

    # -------------------------------------------------------------------------
    # Salvataggio stili nel GeoPackage
    # -------------------------------------------------------------------------

    def salva_stile_in_gpkg(self, layer, gpkg_path):
        """
        Salva lo stile QML del layer nella tabella 'layer_styles' del GeoPackage.
        Strategia:
          1. Esporta il QML originale in un file temporaneo.
          2. Ricarica il layer dal GPKG.
          3. Applica lo stile e lo salva con saveStyleToDatabase.
        Restituisce (successo: bool, messaggio_errore: str).
        """
        layer_name = layer.name().replace(" ", "_")
        tmp_qml = None

        try:
            tmp_qml = tempfile.mktemp(suffix='.qml')

            msg, ok = layer.saveNamedStyle(tmp_qml)
            if not ok:
                return False, f"Impossibile esportare lo stile: {msg}"

            gpkg_layer = QgsVectorLayer(
                f"{gpkg_path}|layername={layer_name}", layer_name, "ogr"
            )
            if not gpkg_layer.isValid():
                return False, f"Layer '{layer_name}' non trovato nel GPKG dopo l'export."

            gpkg_layer.loadNamedStyle(tmp_qml)
            gpkg_layer.saveStyleToDatabase(
                layer_name,
                f"Stile di {layer.name()}",
                True,  # useAsDefault
                ""     # uiFileContent
            )
            return True, ""

        except Exception as e:
            return False, str(e)

        finally:
            if tmp_qml and os.path.exists(tmp_qml):
                os.remove(tmp_qml)

    # -------------------------------------------------------------------------
    # Export principale
    # -------------------------------------------------------------------------

    def esporta(self):
        if not self.gpkg_path:
            QMessageBox.warning(self, "Attenzione", "Seleziona prima il file di destinazione.")
            return

        layers = self.get_layer_selezionati()
        if not layers:
            QMessageBox.warning(self, "Attenzione", "Seleziona almeno un layer da esportare.")
            return

        salva_stili = self.chk_save_styles.isChecked()

        # Sovrascrittura file esistente
        if self.chk_overwrite.isChecked() and os.path.exists(self.gpkg_path):
            os.remove(self.gpkg_path)

        # Ogni layer conta 1 passo export + 1 passo stile (se abilitato)
        steps_per_layer = 2 if salva_stili else 1
        total_steps = len(layers) * steps_per_layer

        self.progress.setVisible(True)
        self.progress.setMaximum(total_steps)
        self.progress.setValue(0)
        self.btn_export.setEnabled(False)

        successi     = []
        errori       = []
        avvisi_stili = []
        step         = 0

        for i, layer in enumerate(layers):
            self.lbl_status.setText(f"Esportando layer: {layer.name()}...")
            self.progress.setValue(step)
            QCoreApplication.processEvents()

            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = layer.name().replace(" ", "_")
            options.fileEncoding = "UTF-8"
            options.onlySelectedFeatures = self.chk_selected.isChecked()
            options.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrOverwriteFile
                if i == 0 and not os.path.exists(self.gpkg_path)
                else QgsVectorFileWriter.CreateOrOverwriteLayer
            )

            error, msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer,
                self.gpkg_path,
                QgsCoordinateTransformContext(),
                options
            )

            step += 1
            self.progress.setValue(step)
            QCoreApplication.processEvents()

            if error == QgsVectorFileWriter.NoError:
                successi.append(layer.name())
            else:
                errori.append(f"{layer.name()}: {msg}")
                if salva_stili:
                    step += 1  # salta il passo stile: il layer non è nel GPKG
                continue

            # --- Salvataggio stile (opzionale) ---
            if salva_stili:
                self.lbl_status.setText(f"Salvataggio stile: {layer.name()}...")
                QCoreApplication.processEvents()
                ok_stile, err_stile = self.salva_stile_in_gpkg(layer, self.gpkg_path)
                if not ok_stile:
                    avvisi_stili.append(f"{layer.name()}: {err_stile}")
                step += 1
                self.progress.setValue(step)
                QCoreApplication.processEvents()

        self.btn_export.setEnabled(True)

        # --- Report finale ---
        msg_finale = f"✓ Layer esportati con successo: {len(successi)}\n"

        if salva_stili:
            stili_ok = len(successi) - len(avvisi_stili)
            msg_finale += f"✓ Stili salvati nel GPKG: {stili_ok}/{len(successi)}\n"
            if avvisi_stili:
                msg_finale += "⚠ Avvisi stili:\n  " + "\n  ".join(avvisi_stili) + "\n"

        if errori:
            msg_finale += f"\n✗ Errori export ({len(errori)}):\n  " + "\n  ".join(errori)

        msg_finale += f"\n\nFile: {self.gpkg_path}"

        self.lbl_status.setText(f"Completato: {len(successi)} layer esportati.")
        QMessageBox.information(self, "Export completato", msg_finale)


# Lancia il dialog
dlg = GeoPackageExporter(iface.mainWindow())
dlg.show()