from qgis.core import (
    QgsVectorFileWriter, QgsProject, QgsCoordinateTransformContext, QgsMapLayer
)
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QFileDialog, QLineEdit, QMessageBox,
    QProgressBar, QCheckBox, QGroupBox
)
from qgis.PyQt.QtCore import Qt, QCoreApplication  # FIX: aggiunto QCoreApplication per processEvents
import os


class GeoPackageExporter(QDialog):
    def __init__(self, parent=None):  # FIX: __init__ corretto (era **init**)
        super().__init__(parent)      # FIX: __init__ corretto (era **init**)
        self.setWindowTitle("Esporta Layer in GeoPackage")
        self.setMinimumWidth(520)
        self.setMinimumHeight(550)
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

        # Riga 1 selezione rapida
        sel_layout1 = QHBoxLayout()
        btn_all = QPushButton("Seleziona tutti")
        btn_none = QPushButton("Deseleziona tutti")
        btn_all.clicked.connect(self.seleziona_tutti)
        btn_none.clicked.connect(self.deseleziona_tutti)
        sel_layout1.addWidget(btn_all)
        sel_layout1.addWidget(btn_none)
        layers_layout.addLayout(sel_layout1)

        # Riga 2 filtri smart
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
        opt_layout.addWidget(self.chk_overwrite)
        opt_layout.addWidget(self.chk_selected)
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

    def load_layers(self):
        """Carica i layer vettoriali del progetto nella lista."""
        self.list_widget.clear()
        layers = QgsProject.instance().mapLayers().values()
        tree_root = QgsProject.instance().layerTreeRoot()

        for layer in layers:
            # FIX: uso QgsMapLayer.VectorLayer invece di layer.VectorLayer (deprecato)
            if layer.type() != QgsMapLayer.VectorLayer:
                continue
            item = QListWidgetItem(layer.name())
            item.setData(Qt.UserRole, layer.id())
            # Di default seleziona i layer visibili
            tree_layer = tree_root.findLayer(layer.id())
            is_visible = tree_layer.isVisible() if tree_layer else False
            item.setCheckState(Qt.Checked if is_visible else Qt.Unchecked)
            self.list_widget.addItem(item)

    def scegli_file(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Salva GeoPackage", "", "GeoPackage (*.gpkg)"
        )
        if path:
            if not path.endswith('.gpkg'):
                path += '.gpkg'
            self.gpkg_path = path
            self.txt_path.setText(path)

    # --- Metodi di selezione rapida ---

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

    def esporta(self):
        if not self.gpkg_path:
            QMessageBox.warning(self, "Attenzione", "Seleziona prima il file di destinazione.")
            return

        layers = self.get_layer_selezionati()
        if not layers:
            QMessageBox.warning(self, "Attenzione", "Seleziona almeno un layer da esportare.")
            return

        # Sovrascrittura file esistente
        if self.chk_overwrite.isChecked() and os.path.exists(self.gpkg_path):
            os.remove(self.gpkg_path)

        self.progress.setVisible(True)
        self.progress.setMaximum(len(layers))
        self.btn_export.setEnabled(False)

        successi = []
        errori = []

        for i, layer in enumerate(layers):
            self.lbl_status.setText(f"Esportando: {layer.name()}...")
            self.progress.setValue(i)
            QCoreApplication.processEvents()  # FIX: aggiornamento UI durante il loop

            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = layer.name().replace(" ", "_")
            options.fileEncoding = "UTF-8"
            options.onlySelectedFeatures = self.chk_selected.isChecked()
            options.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrOverwriteFile if i == 0 and not os.path.exists(self.gpkg_path)
                else QgsVectorFileWriter.CreateOrOverwriteLayer
            )

            error, msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer,
                self.gpkg_path,
                QgsCoordinateTransformContext(),
                options
            )

            if error == QgsVectorFileWriter.NoError:
                successi.append(layer.name())
            else:
                errori.append(f"{layer.name()}: {msg}")

        self.progress.setValue(len(layers))
        self.btn_export.setEnabled(True)

        # Report finale
        msg_finale = f"✓ Esportati con successo: {len(successi)}\n"
        if errori:
            msg_finale += f"✗ Errori ({len(errori)}):\n" + "\n".join(errori)
        msg_finale += f"\n\nFile: {self.gpkg_path}"

        self.lbl_status.setText(f"Completato: {len(successi)} layer esportati.")
        QMessageBox.information(self, "Export completato", msg_finale)


# Lancia il dialog
dlg = GeoPackageExporter(iface.mainWindow())
dlg.show()