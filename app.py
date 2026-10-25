"""Qt desktop interface; SSH and SFTP work stays on background threads."""

from datetime import datetime
import logging
from pathlib import Path, PurePosixPath
from queue import Empty, Queue
import sys
from threading import Event, Thread

from PySide6.QtCore import QMimeData, QTimer, Qt, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QDialog, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLayout, QLineEdit, QMenu, QMessageBox, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from diagnostics import LOGGER, get_log_directory, get_logging_error, install_qt_message_handler
from folder_picker import FolderPicker
from headset import HeadsetError, HeadsetStatus, fetch_status, power_off_headset
from preferences import DEFAULT_DESTINATION, DestinationPreferences
from theme import BatteryIcon, ControllerStatus, ElidedLabel, StorageUsageBar, TransferProgressBar, UploadIcon, apply_dark_theme
from transfers import TransferCancelled, upload_files


APP_VERSION = "0.1.8"
REFRESH_INTERVALS = (
    ("Never", 0), ("5 seconds", 5000), ("10 seconds", 10000),
    ("30 seconds", 30000), ("1 minute", 60000), ("5 minutes", 300000),
)


def gib(value: int) -> str:
    return f"{value / 1024**3:.1f} GiB"


def ram_amount(used: int, total: int) -> str:
    return f"{used / 1_000_000_000:.1f} / {total / 1_000_000_000:.1f} GB"


def label(text="", *, name=None, muted=False, wrap=False, center=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    if name:
        widget.setObjectName(name)
    if muted:
        widget.setProperty("muted", True)
    if center:
        widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return widget


def button(text, command, *, name=None):
    widget = QPushButton(text)
    if name:
        widget.setObjectName(name)
    widget.clicked.connect(command)
    return widget


def dropped_paths(mime: QMimeData) -> tuple[str, ...]:
    urls = mime.urls() if mime.hasUrls() else []
    if not urls or any(not url.isLocalFile() for url in urls):
        return ()
    return tuple(url.toLocalFile() for url in urls)


class DropPanel(QFrame):
    def __init__(self, can_drop, send):
        super().__init__()
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setAccessibleName("File drop area")
        self.setToolTip("Copy files to Steam Frame. Originals stay on your PC.")
        self.can_drop, self.send = can_drop, send

    def set_drag_active(self, active):
        if self.property("dragActive") == active:
            return
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def dragEnterEvent(self, event):
        if self.can_drop() and event.possibleActions() & Qt.DropAction.CopyAction and dropped_paths(event.mimeData()):
            self.set_drag_active(True)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            self.set_drag_active(False)
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        self.set_drag_active(False)
        event.accept()

    def dropEvent(self, event):
        self.set_drag_active(False)
        paths = dropped_paths(event.mimeData())
        if self.can_drop() and event.possibleActions() & Qt.DropAction.CopyAction and paths and self.send(paths):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()


class FrameConnectApp(QWidget):
    def __init__(self, *, preferences=None):
        super().__init__()
        self.preferences = preferences
        self._default_destination = DEFAULT_DESTINATION
        apply_dark_theme(QApplication.instance())
        self.setObjectName("mainWindow")
        self.setWindowTitle("Frame Connect")
        available_height = self.screen().availableGeometry().height() - 80
        self.resize(800, min(1240, max(680, available_height)))
        self.setMinimumSize(680, 680)
        self.results = Queue()
        self.connected_to = None
        self.busy = False
        self.uploading = False
        self.close_requested = False
        self._closing = False
        self._status_request = None
        self._power_request = None
        self._power_state = "idle"
        self._monitor_connection = None
        self._last_checked = None
        self._last_status_error = None
        self._refresh_interval_ms = 5000
        self.cancel_upload = Event()
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(50)
        self._poll_timer.timeout.connect(self._poll)
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(self._refresh_interval_ms)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.timeout.connect(self._auto_refresh)

        self.host, self.user, self.key_file = QLineEdit("frame"), QLineEdit("steamos"), QLineEdit()
        self.key_file.setPlaceholderText("Use your default SSH key")
        self.entries = [self.host, self.user, self.key_file]
        self._destination_connection = (self.host.text().strip(), self.user.text().strip())
        for entry in self.entries:
            entry.textChanged.connect(self._connection_changed)
        self._build_settings()

        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        self.navigation = QFrame()
        self.navigation.setObjectName("topNavigation")
        self.navigation.setFixedHeight(56)
        navigation_layout = QHBoxLayout(self.navigation)
        navigation_layout.setContentsMargins(28, 8, 28, 8)
        navigation_layout.setSpacing(8)
        self.home_tab = button("Home", lambda: self.pages.setCurrentWidget(self.home_page), name="navigationTab")
        self.settings_tab = button("Settings", lambda: self.pages.setCurrentWidget(self.settings_page), name="navigationTab")
        self.navigation_group = QButtonGroup(self)
        self.navigation_group.setExclusive(True)
        for tab in (self.home_tab, self.settings_tab):
            tab.setCheckable(True)
            self.navigation_group.addButton(tab)
            navigation_layout.addWidget(tab)
        self.home_tab.setChecked(True)
        navigation_layout.addStretch(1)
        main.addWidget(self.navigation)
        self.pages = QStackedWidget()
        self.home_page = QWidget()
        self.home_page.setObjectName("homePage")
        home_layout = QVBoxLayout(self.home_page)
        home_layout.setContentsMargins(0, 0, 0, 0)
        home_layout.setSpacing(0)
        self.pages.addWidget(self.home_page)
        main.addWidget(self.pages, 1)
        body = QWidget()
        body.setObjectName("mainBody")
        body_layout = QVBoxLayout(body)
        body_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        body_layout.setContentsMargins(28, 24, 28, 20)
        body_layout.setSpacing(18)
        self.status_scroll = QScrollArea()
        self.status_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.status_scroll.setWidgetResizable(True)
        self.status_scroll.setAccessibleName("Headset status")
        self.status_scroll.setWidget(body)
        home_layout.addWidget(self.status_scroll, 1)

        header = QHBoxLayout()
        title = QVBoxLayout()
        title.setSpacing(3)
        title.addWidget(label("Frame Connect", name="appTitle"))
        title.addWidget(label("Your Steam Frame companion", muted=True))
        header.addLayout(title, 1)
        self.settings_button = button("Connection settings", self._open_settings, name="quietButton")
        header.addWidget(self.settings_button)
        body_layout.addLayout(header)

        self.connection_panel = QFrame()
        self.connection_panel.setObjectName("card")
        connection_layout = QHBoxLayout(self.connection_panel)
        connection_layout.setContentsMargins(20, 20, 20, 20)
        connection_layout.setSpacing(18)
        copy = QVBoxLayout()
        copy.setSpacing(6)
        copy.addWidget(label("STEAM FRAME", name="eyebrow"))
        self.connection = label("Not connected", name="connectionStatus", wrap=True)
        self.detail = label("Open Connection settings to enter your headset’s address.", muted=True, wrap=True)
        copy.addWidget(self.connection)
        copy.addWidget(self.detail)
        connection_layout.addLayout(copy, 1)
        controls = QVBoxLayout()
        controls.setSpacing(8)
        controls.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self.connect = button("Connect", self.refresh, name="primaryButton")
        self.connect.setFixedWidth(104)
        controls.addWidget(self.connect, 0, Qt.AlignmentFlag.AlignRight)
        self.refresh_interval_button = QPushButton("Auto-refresh: 5 seconds")
        self.refresh_interval_button.setObjectName("refreshIntervalButton")
        self.refresh_interval_button.setAccessibleName("Automatic refresh interval")
        self.refresh_interval_button.setFixedWidth(190)
        self.refresh_interval_button.setToolTip("Choose how often headset readings update automatically")
        self.refresh_menu = QMenu(self.refresh_interval_button)
        self.refresh_menu.setMinimumWidth(self.refresh_interval_button.width())
        self.refresh_group = QActionGroup(self)
        self.refresh_group.setExclusive(True)
        self.refresh_actions = {}
        for text, interval in REFRESH_INTERVALS:
            action = QAction(text, self.refresh_menu)
            action.setCheckable(True)
            action.setData(interval)
            action.setChecked(interval == self._refresh_interval_ms)
            self.refresh_group.addAction(action)
            self.refresh_menu.addAction(action)
            self.refresh_actions[interval] = action
        self.refresh_menu.triggered.connect(lambda action: self._set_refresh_interval(action.data()))
        self.refresh_interval_button.setMenu(self.refresh_menu)
        controls.addWidget(self.refresh_interval_button, 0, Qt.AlignmentFlag.AlignRight)
        connection_layout.addLayout(controls)
        body_layout.addWidget(self.connection_panel)

        cards = QGridLayout()
        cards.setSpacing(14)
        cards.setColumnStretch(0, 1)
        cards.setColumnStretch(1, 1)
        self.battery, self.battery_detail = label("—", name="metricValue", wrap=True), label("Waiting for the headset", muted=True, wrap=True)
        self.storage, self.storage_detail = label("—", name="metricValue", wrap=True), label("Home drive", muted=True, wrap=True)
        self.temperature, self.temperature_detail = label("— °C", name="metricValue", wrap=True), label("Waiting for the headset", muted=True, wrap=True)
        self.ram, self.ram_detail = label("— / — GB", name="metricValue", wrap=True), label("System memory · Used / total", muted=True, wrap=True)
        for index, (title_text, value, description) in enumerate((
            ("BATTERY", self.battery, self.battery_detail), ("STORAGE", self.storage, self.storage_detail),
            ("TEMPERATURE", self.temperature, self.temperature_detail), ("RAM USAGE", self.ram, self.ram_detail),
        )):
            card = QFrame()
            card.setObjectName("card")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(20, 17, 20, 17)
            layout.setSpacing(7)
            layout.addWidget(label(title_text, name="eyebrow"))
            if value is self.battery:
                battery_row = QHBoxLayout()
                battery_row.setSpacing(10)
                self.battery_icon = BatteryIcon(card)
                battery_row.addWidget(self.battery_icon, 0, Qt.AlignmentFlag.AlignVCenter)
                battery_row.addWidget(value, 1)
                layout.addLayout(battery_row)
            else:
                layout.addWidget(value)
            if value is self.storage:
                self.storage_usage = StorageUsageBar()
                layout.addWidget(self.storage_usage)
            layout.addWidget(description)
            cards.addWidget(card, index // 2, index % 2)
        self.controllers_card = QFrame()
        self.controllers_card.setObjectName("card")
        controller_layout = QHBoxLayout(self.controllers_card)
        controller_layout.setContentsMargins(20, 14, 20, 14)
        controller_layout.setSpacing(18)
        self.controllers = {hand: ControllerStatus(hand) for hand in ("left", "right")}
        for controller in self.controllers.values():
            controller_layout.addWidget(controller, 1)
        cards.addWidget(self.controllers_card, 2, 0, 1, 2)
        self.os_card = QFrame()
        self.os_card.setObjectName("card")
        os_layout = QHBoxLayout(self.os_card)
        os_layout.setContentsMargins(20, 11, 20, 11)
        os_layout.setSpacing(14)
        os_layout.addWidget(label("STEAM FRAME OS VERSION", name="eyebrow"))
        self.os_version = label("—", name="osVersion", wrap=True)
        self.os_version.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.os_version.setAccessibleName("Steam Frame OS version")
        os_layout.addWidget(self.os_version, 1)
        cards.addWidget(self.os_card, 3, 0, 1, 2)
        body_layout.addLayout(cards)
        body_layout.addStretch(1)

        self.transfer_panel = QWidget()
        self.transfer_panel.setObjectName("transferPanel")
        transfer_layout = QVBoxLayout(self.transfer_panel)
        transfer_layout.setContentsMargins(28, 12, 28, 14)
        transfer_layout.setSpacing(10)
        transfer_layout.addWidget(label("SEND TO STEAM FRAME", name="eyebrow"))
        destination_row = QHBoxLayout()
        saved_destination = self.preferences.destination(*self._destination_connection) if self.preferences else DEFAULT_DESTINATION
        self.destination = self.destination_entry = QLineEdit(saved_destination)
        self.destination.setAcceptDrops(False)
        self.destination.setAccessibleName("Destination folder on Steam Frame")
        self.destination.editingFinished.connect(self._remember_destination)
        destination_row.addWidget(self.destination, 1)
        self.folder_button = button("Choose folder…", self.choose_folder)
        destination_row.addWidget(self.folder_button)
        transfer_layout.addLayout(destination_row)

        self.drop_area = DropPanel(self._can_drop, self.start_upload)
        self.drop_area.setMinimumHeight(176)
        dock_layout = QVBoxLayout(self.drop_area)
        dock_layout.setContentsMargins(16, 14, 16, 14)
        dock_layout.setSpacing(4)
        dock_layout.addStretch(1)
        self.upload_icon = UploadIcon()
        dock_layout.addWidget(self.upload_icon, 0, Qt.AlignmentFlag.AlignHCenter)
        self.drop_title = label("Drag and drop here", name="dropTitle", center=True)
        dock_layout.addWidget(self.drop_title)
        dock_layout.addWidget(label("or", name="dropOr", muted=True, center=True))
        self.file_button = button("select files", self.choose_files, name="fileSelectButton")
        self.file_button.setCursor(Qt.CursorShape.PointingHandCursor)
        dock_layout.addWidget(self.file_button, 0, Qt.AlignmentFlag.AlignHCenter)
        dock_layout.addStretch(1)
        transfer_layout.addWidget(self.drop_area)
        self.progress = TransferProgressBar()
        transfer_layout.addWidget(self.progress)
        self.transfer_metadata = QFrame()
        self.transfer_metadata.setObjectName("transferMetadata")
        self.transfer_metadata.setFixedHeight(36)
        metadata = QHBoxLayout(self.transfer_metadata)
        metadata.setContentsMargins(12, 6, 12, 6)
        metadata.setSpacing(10)
        self.transfer_filename = ElidedLabel("No files selected")
        self.transfer_filename.setObjectName("transferFileName")
        self.transfer_filename.setAccessibleName("Current transfer file")
        metadata.addWidget(self.transfer_filename, 1)
        self.transfer_percent = label("0%", name="transferPercent")
        self.transfer_percent.setFixedWidth(48)
        self.transfer_percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.transfer_percent.setAccessibleName("Transfer percentage")
        metadata.addWidget(self.transfer_percent)
        transfer_layout.addWidget(self.transfer_metadata)
        footer = QHBoxLayout()
        self.transfer_detail = ElidedLabel("Connect to the headset to send files.")
        self.transfer_detail.setObjectName("transferDetail")
        footer.addWidget(self.transfer_detail, 1)
        self.cancel_button = button("Cancel transfer", self.cancel_transfer)
        # Keep the footer height stable when Cancel appears or disappears.
        cancel_policy = self.cancel_button.sizePolicy()
        cancel_policy.setRetainSizeWhenHidden(True)
        self.cancel_button.setSizePolicy(cancel_policy)
        self.cancel_button.setEnabled(False)
        self.cancel_button.hide()
        footer.addWidget(self.cancel_button)
        transfer_layout.addLayout(footer)
        home_layout.addWidget(self.transfer_panel)
        self._build_settings_page()

        self.status_footer = QFrame()
        self.status_footer.setObjectName("statusFooter")
        self.status_footer.setFixedHeight(32)
        status_layout = QHBoxLayout(self.status_footer)
        status_layout.setContentsMargins(18, 0, 18, 0)
        status_layout.setSpacing(16)
        self.footer_status = label(name="footerStatus")
        self.footer_status.setAccessibleName("Headset connection status")
        status_layout.addWidget(self.footer_status, 1)
        self.footer_version = label(f"v{APP_VERSION}", name="footerVersion")
        self.footer_version.setAccessibleName("Application version")
        status_layout.addWidget(self.footer_version)
        main.addWidget(self.status_footer)
        self._set_footer_status("Waiting for headset…")
        self._update_transfer_controls()

    def _build_settings_page(self):
        self.settings_page = QScrollArea()
        self.settings_page.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_page.setWidgetResizable(True)
        self.settings_page.setAccessibleName("Settings")
        body = QWidget()
        body.setObjectName("settingsBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)
        heading = QVBoxLayout()
        heading.setSpacing(3)
        heading.addWidget(label("Settings", name="appTitle"))
        heading.addWidget(label("Manage your Steam Frame", muted=True))
        layout.addLayout(heading)

        connection_card = QFrame()
        connection_card.setObjectName("card")
        connection_layout = QHBoxLayout(connection_card)
        connection_layout.setContentsMargins(20, 20, 20, 20)
        connection_copy = QVBoxLayout()
        connection_copy.setSpacing(6)
        connection_copy.addWidget(label("CONNECTION", name="eyebrow"))
        connection_copy.addWidget(label("Headset connection", name="connectionStatus"))
        connection_copy.addWidget(label("Change the headset address or SSH key.", muted=True, wrap=True))
        connection_layout.addLayout(connection_copy, 1)
        self.settings_connection_button = button("Connection settings", self._open_settings)
        connection_layout.addWidget(self.settings_connection_button)
        layout.addWidget(connection_card)

        power_card = QFrame()
        power_card.setObjectName("card")
        power_layout = QVBoxLayout(power_card)
        power_layout.setContentsMargins(20, 20, 20, 20)
        power_layout.setSpacing(12)
        power_layout.addWidget(label("POWER", name="eyebrow"))
        power_layout.addWidget(label("Turn off Steam Frame", name="connectionStatus"))
        power_layout.addWidget(label(
            "Shut down your headset remotely. Turn it back on using its power button.", muted=True, wrap=True,
        ))
        self.power_status = label(name="powerStatus", wrap=True)
        power_layout.addWidget(self.power_status)
        self.power_button = button("Turn off headset", self.request_power_off, name="dangerButton")
        power_layout.addWidget(self.power_button, 0, Qt.AlignmentFlag.AlignRight)
        layout.addWidget(power_card)

        diagnostics_card = QFrame()
        diagnostics_card.setObjectName("card")
        diagnostics_layout = QVBoxLayout(diagnostics_card)
        diagnostics_layout.setContentsMargins(20, 20, 20, 20)
        diagnostics_layout.setSpacing(12)
        diagnostics_layout.addWidget(label("DIAGNOSTICS", name="eyebrow"))
        diagnostics_layout.addWidget(label("Error logs", name="connectionStatus"))
        diagnostics_layout.addWidget(label(
            "Connection errors and crash details are saved locally, even after closing the app.",
            muted=True, wrap=True,
        ))
        directory = get_log_directory()
        self.log_status = label(get_logging_error() or (str(directory) if directory else "Logging is unavailable."), muted=True, wrap=True)
        self.log_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        diagnostics_layout.addWidget(self.log_status)
        self.logs_button = button("Open log folder", self._open_log_folder)
        self.logs_button.setEnabled(directory is not None)
        diagnostics_layout.addWidget(self.logs_button, 0, Qt.AlignmentFlag.AlignRight)
        layout.addWidget(diagnostics_card)

        about_card = QFrame()
        about_card.setObjectName("card")
        about_layout = QVBoxLayout(about_card)
        about_layout.setContentsMargins(20, 20, 20, 20)
        about_layout.setSpacing(12)
        about_layout.addWidget(label("ABOUT", name="eyebrow"))
        about_layout.addWidget(label(f"Frame Connect v{APP_VERSION}", name="connectionStatus"))
        about_layout.addWidget(label("Built with Python and Qt/PySide6. Includes open-source libraries.", muted=True, wrap=True))
        self.licenses_button = button("View licenses", self._open_licenses)
        about_layout.addWidget(self.licenses_button, 0, Qt.AlignmentFlag.AlignRight)
        layout.addWidget(about_card)
        layout.addStretch(1)
        self.settings_page.setWidget(body)
        self.pages.addWidget(self.settings_page)

    def _open_log_folder(self):
        directory = get_log_directory()
        if directory is not None and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
            LOGGER.warning("Could not open diagnostic log folder")
            self.log_status.setText(f"Could not open the folder. Open it manually:\n{directory}")

    def _open_licenses(self):
        path = Path(__file__).with_name("THIRD_PARTY_NOTICES.txt")
        if not path.is_file() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, "Licenses", "Could not open the license notices. See THIRD_PARTY_NOTICES.txt in the release download.")

    def _set_power_status(self, text, state="idle"):
        self._power_state = state
        self.power_status.setText(text)
        self.power_status.setProperty("state", state)
        self.power_status.style().unpolish(self.power_status)
        self.power_status.style().polish(self.power_status)
        self.power_status.update()

    def _confirm_power_off(self, host):
        confirmation = QMessageBox(self)
        confirmation.setWindowTitle("Turn off Steam Frame")
        confirmation.setText(f"Turn off the headset connected as {host}?")
        confirmation.setInformativeText(
            "Running games and downloads will stop. Turn the headset back on using its power button."
        )
        turn_off = confirmation.addButton("Turn off", QMessageBox.ButtonRole.DestructiveRole)
        turn_off.setObjectName("dangerButton")
        turn_off.style().unpolish(turn_off)
        turn_off.style().polish(turn_off)
        cancel = confirmation.addButton(QMessageBox.StandardButton.Cancel)
        confirmation.setDefaultButton(cancel)
        confirmation.setEscapeButton(cancel)
        confirmation.exec()
        confirmed = confirmation.clickedButton() is turn_off
        confirmation.deleteLater()
        return confirmed

    def request_power_off(self):
        if not self._can_drop() or self._closing:
            return
        connection = self.connected_to
        if not self._confirm_power_off(connection[0]):
            return
        # The confirmation runs its own event loop: recheck connection and busy state.
        if not self._can_drop() or self.connected_to != connection or self._closing:
            return
        self._status_request = None
        self._power_request = (object(), connection)
        LOGGER.info("Sending power-off request to %s", connection[0])
        self._set_busy(True)
        self._set_power_status("Sending power-off request…", "pending")
        Thread(target=self._power_off, args=(self._power_request,), daemon=True).start()
        self._sync_poll_timer()

    def _power_off(self, request):
        try:
            power_off_headset(*request[1])
            result = None
        except (HeadsetError, OSError, ValueError) as error:
            result = error
        self.results.put(("power", request, result))

    def _handle_power_result(self, request, result):
        if request is not self._power_request:
            return
        self._power_request = None
        if result is None:
            LOGGER.info("Power-off request acknowledged by %s", request[1][0])
            self.connected_to = self._monitor_connection = self._status_request = None
            self._last_status_error = None
            self._clear_status()
            self.connection.setText("Power-off requested")
            self.detail.setText("Turn the headset on manually, then click Connect.")
            self.transfer_detail.setText("Headset is shutting down.")
            self._set_footer_status("Power-off requested", "success")
            self._set_power_status("Power-off request sent. Turn the headset on manually to connect again.", "requested")
        else:
            LOGGER.error("Power-off request failed for %s: %s", request[1][0], result)
            self._set_power_status(str(result), "error")
        self._set_busy(False)
        self._sync_poll_timer()
        if self.close_requested:
            self.close()

    def _set_footer_status(self, text, state="neutral", *, detail=None):
        self.footer_status.setText(text)
        self.footer_status.setToolTip(detail or text)
        self.footer_status.setProperty("state", state)
        style = self.footer_status.style()
        style.unpolish(self.footer_status)
        style.polish(self.footer_status)
        self.footer_status.update()

    def _build_settings(self):
        self.settings_dialog = QDialog(self)
        self.settings_dialog.setWindowTitle("Connection settings")
        self.settings_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.settings_dialog.resize(580, 240)
        layout = QVBoxLayout(self.settings_dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        form = QFormLayout()
        form.setSpacing(12)
        form.addRow("Hostname or IP", self.host)
        form.addRow("SSH username", self.user)
        key_row = QHBoxLayout()
        key_row.addWidget(self.key_file, 1)
        self.browse = button("Browse…", self._browse_key)
        self.browse.setAutoDefault(False)
        key_row.addWidget(self.browse)
        form.addRow("Private key (optional)", key_row)
        layout.addLayout(form)
        done = button("Done", self.settings_dialog.accept)
        done.setDefault(True)
        layout.addWidget(done, 0, Qt.AlignmentFlag.AlignRight)

    def _open_settings(self):
        self.settings_dialog.open()

    def _browse_key(self):
        selected, _filter = QFileDialog.getOpenFileName(
            self.settings_dialog, "Select your SSH private key (not the .pub file)", str(Path.home() / ".ssh"),
        )
        if selected:
            self.key_file.setText(selected)

    def _set_busy(self, busy):
        self.busy = busy
        for widget in (*self.entries, self.browse, self.connect, self.settings_button, self.settings_connection_button):
            widget.setEnabled(not busy)
        self.connect.setEnabled(not busy and (self._status_request is None or self._status_request[2]))
        self._update_transfer_controls()
        self._schedule_auto_refresh()

    def _can_drop(self):
        return self.connected_to is not None and not self.busy

    def _update_transfer_controls(self):
        self.connect.setText("Refresh" if self.connected_to is not None else "Connect")
        self.power_button.setEnabled(self._can_drop())
        if self._power_state == "idle":
            text = "Ready to turn off the connected headset."
            if self.busy:
                text = "Wait for the current operation to finish before turning off."
            elif self.connected_to is None:
                text = "Connect to the headset on Home to use power controls."
            self._set_power_status(text)
        for widget in (self.destination, self.folder_button, self.file_button, self.drop_title, self.upload_icon):
            widget.setEnabled(self._can_drop())
        if not self._can_drop():
            self.drop_area.set_drag_active(False)

    def _connection_changed(self, *_args):
        identity = (self.host.text().strip(), self.user.text().strip())
        if identity != self._destination_connection:
            self._remember_destination()
            self._destination_connection = identity
            self._default_destination = DEFAULT_DESTINATION
            path = self.preferences.destination(*identity) if self.preferences else DEFAULT_DESTINATION
            self.destination.setText(path)
        self._last_status_error = None
        self._power_state = "idle"
        self._set_footer_status("Waiting for headset…")
        if self.connected_to is not None or self._monitor_connection is not None or self._status_request is not None:
            manual_request = self._status_request is not None and not self._status_request[2]
            self._auto_timer.stop()
            self._monitor_connection = self._status_request = None
            self.connected_to = None
            if manual_request:
                self._set_busy(False)
            self.connect.setEnabled(not self.busy)
            self.connection.setText("Not connected")
            self.detail.setText("Connection settings changed. Connect again before sending files.")
            self.transfer_detail.setText("Connect to the headset to send files.")
            self._clear_status()
            self._update_transfer_controls()
            self._sync_poll_timer()

    def _remember_destination(self):
        if self.preferences is None:
            return
        path = self.destination.text().strip()
        if path == self._default_destination:
            path = DEFAULT_DESTINATION
        try:
            saved = self.preferences.remember(*self._destination_connection, path)
        except HeadsetError:
            # Blank/incomplete edits must not overwrite the last valid folder.
            return
        self.destination.setToolTip(
            "" if saved else "Could not save this folder for next time. See Settings → Open log folder."
        )

    def _select_destination(self, path):
        self.destination.setText(path)
        self._remember_destination()

    def _clear_status(self):
        for controller in self.controllers.values():
            controller.set_status()
        self.battery.setText("—")
        self.battery.setToolTip("")
        self.battery_icon.set_level(None)
        self.battery_detail.setText("Waiting for the headset")
        self.storage.setText("—")
        self.storage_usage.set_usage()
        self.storage_detail.setText("Home drive")
        self.temperature.setText("— °C")
        self.temperature_detail.setText("Waiting for the headset")
        self.ram.setText("— / — GB")
        self.ram_detail.setText("System memory · Used / total")
        self.os_version.setText("—")

    def refresh(self):
        if self.busy or self._status_request is not None or self._closing:
            return
        connection = (self.host.text(), self.user.text(), self.key_file.text())
        was_connected = self.connected_to is not None
        self._last_status_error = None
        self.connected_to = None
        self._set_busy(True)
        self.connect.setText("Refresh" if was_connected else "Connect")
        self._clear_status()
        self.connection.setText("Connecting…")
        self.detail.setText("Checking the headset. This can take up to 20 seconds.")
        self._set_footer_status("Connecting to headset…")
        self._start_status_request(connection, automatic=False)

    def _set_refresh_interval(self, interval):
        self._auto_timer.stop()
        self._refresh_interval_ms = interval
        if interval:
            self._auto_timer.setInterval(interval)
        action = self.refresh_actions[interval]
        action.setChecked(True)
        self.refresh_interval_button.setText(f"Auto-refresh: {action.text()}")
        if self._last_status_error is not None:
            error, automatic = self._last_status_error
            self._show_status_error(error, automatic=automatic)
        self._schedule_auto_refresh()

    def _schedule_auto_refresh(self):
        if self._refresh_interval_ms and self._monitor_connection is not None and (not self.busy or self.uploading) and self._status_request is None and not self._closing:
            self._auto_timer.start()
        else:
            self._auto_timer.stop()

    def _auto_refresh(self):
        if self._refresh_interval_ms and self._monitor_connection is not None and (not self.busy or self.uploading) and self._status_request is None and not self._closing:
            self._start_status_request(self._monitor_connection, automatic=True)

    def _start_status_request(self, connection, *, automatic):
        self._auto_timer.stop()
        if not automatic:
            LOGGER.info("Manual connection check for %s", connection[0])
        # Identity lets us discard a result if settings change or the window closes.
        self._status_request = (object(), connection, automatic)
        if not automatic:
            self.connect.setEnabled(False)
        Thread(target=self._fetch, args=(self._status_request,), daemon=True).start()
        self._sync_poll_timer()

    def _fetch(self, request):
        try:
            result = fetch_status(*request[1])
        except HeadsetError as error:
            result = error
        self.results.put(("status", request, result))

    def _sync_poll_timer(self):
        if self.uploading or self._power_request is not None or (self._status_request is not None and not self._closing):
            if not self._poll_timer.isActive():
                self._poll_timer.start()
        else:
            self._poll_timer.stop()

    def _poll(self):
        while True:
            try:
                result = self.results.get_nowait()
            except Empty:
                self._sync_poll_timer()
                return
            if result[0] == "status":
                self._handle_status_result(result[1], result[2])
            elif result[0] == "power":
                self._handle_power_result(result[1], result[2])
            else:
                self._handle_transfer_result(*result)

    def _handle_status_result(self, request, result):
        if request is not self._status_request or self._closing:
            return
        self._status_request = None
        automatic = request[2]
        if not automatic:
            self._set_busy(False)
        self.connect.setEnabled(not self.busy)
        if isinstance(result, HeadsetError):
            LOGGER.warning(
                "%s connection check failed for %s: %s",
                "Automatic" if automatic else "Manual", request[1][0], result,
            )
            self.connected_to = None
            self._update_transfer_controls()
            self._show_status_error(result, automatic=automatic)
        else:
            if self.connected_to is None:
                LOGGER.info("Headset connection established: %s", result.hostname)
            self.show_status(result, automatic=automatic)
        self._schedule_auto_refresh()

    def _show_status_error(self, error, *, automatic):
        for controller in self.controllers.values():
            controller.set_status(message="Status unavailable")
        self._last_status_error = (error, automatic)
        retrying = bool(self._refresh_interval_ms and self._monitor_connection is not None)
        if automatic:
            self.connection.setText("Headset unavailable")
            retry_note = " Retrying automatically." if retrying else ""
            self.detail.setText(f"Last update at {self._last_checked}.{retry_note}\n{error}")
            footer = "Headset unavailable · Retrying…" if retrying else "Headset unavailable"
            self._set_footer_status(footer, "error", detail=str(error))
            if not self.busy:
                self.transfer_detail.setText(
                    "Headset unavailable. Reconnecting automatically…" if retrying
                    else "Headset unavailable. Click Connect to retry."
                )
        else:
            self.connection.setText("Could not connect")
            retry_note = "\nRetrying automatically." if retrying else ""
            self.detail.setText(f"{error}{retry_note}")
            self._set_footer_status("Connection failed", "error", detail=str(error))

    def show_status(self, status: HeadsetStatus, *, automatic=False):
        self._last_status_error = None
        if self._power_state == "requested":
            self._power_state = "idle"
        was_connected = self.connected_to is not None
        use_default = self.destination.text().strip() in (DEFAULT_DESTINATION, self._default_destination)
        self._default_destination = status.downloads
        if use_default:
            self.destination.setText(status.downloads)
        self.connected_to = tuple(entry.text().strip() for entry in self.entries)
        self._monitor_connection = self.connected_to
        self._update_transfer_controls()
        if not automatic or (not was_connected and not self.busy):
            self.transfer_detail.setText("Ready. Drop files here or choose files to send.")
        self.connection.setText(f"Connected to {status.hostname}")
        self._set_footer_status("Headset connected", "success", detail=f"Connected to {status.hostname}")
        self._last_checked = f"{datetime.now():%H:%M:%S}"
        self.detail.setText(f"Updated at {self._last_checked}.")
        self.os_version.setText(status.os_version or "Unavailable")
        readings = {controller.hand: controller for controller in (status.controllers or ())}
        for hand, widget in self.controllers.items():
            widget.set_status(readings.get(hand), message="Status unavailable")
        self.battery_icon.set_level(None)
        self.battery.setToolTip("")
        if len(status.batteries) == 1:
            battery = status.batteries[0]
            self.battery.setText(f"{battery.percent}%")
            self.battery_icon.set_level(battery.percent, charging=battery.display_state == "Charging")
            source = (
                "Battery estimate reported by Steam" if battery.source == "steam"
                else "Raw system battery percentage · Steam estimate unavailable"
            )
            self.battery.setToolTip(source)
            self.battery_icon.setToolTip(f"{battery.percent}% · {battery.display_state} · {source}")
            self.battery_detail.setText(battery.detail)
        elif status.batteries:
            self.battery.setText("Multiple batteries")
            self.battery_detail.setText("\n".join(
                f"{battery.name}: {battery.percent}% · {battery.display_state}" for battery in status.batteries
            ))
        else:
            self.battery.setText("Unavailable")
            self.battery_detail.setText("The headset did not report a battery reading.")
        self.storage.setText(f"{gib(status.free)} free")
        self.storage_detail.setText(f"{gib(status.used)} used / {gib(status.total)} total\nHome drive")
        self.storage_usage.set_usage(status.used, status.total)
        if status.temperature is None:
            self.temperature.setText("Unavailable")
            self.temperature_detail.setText("No readable temperature sensor.")
        else:
            self.temperature.setText(f"{status.temperature.celsius:.1f} °C")
            self.temperature_detail.setText(f"Highest sensor · {status.temperature.sensor}")
        if status.memory is None:
            self.ram.setText("Unavailable")
            self.ram_detail.setText("The headset did not report RAM usage.")
        else:
            memory = status.memory
            self.ram.setText(ram_amount(memory.used, memory.total))
            self.ram_detail.setText(f"{100 * memory.used / memory.total:.0f}% used · System memory\nUsed / total")
        self._schedule_auto_refresh()

    def choose_files(self):
        if self._can_drop():
            paths, _filter = QFileDialog.getOpenFileNames(self, "Choose files to send to Steam Frame")
            if paths:
                self.start_upload(paths)

    def choose_folder(self):
        if self._can_drop():
            self._set_busy(True)
            self.folder_picker = FolderPicker(self, self.connected_to, self._select_destination, lambda: self._set_busy(False))
            self.folder_picker.open()

    def start_upload(self, paths):
        if not paths or not self._can_drop():
            return False
        self._remember_destination()
        self.uploading = True
        self._set_busy(True)
        self.cancel_upload.clear()
        self.cancel_button.setEnabled(True)
        self.cancel_button.show()
        self.progress.set_progress(0)
        self.transfer_filename.setText(Path(paths[0]).name)
        self.transfer_percent.setText("0%")
        self.transfer_detail.setText("Preparing the destination folder…")
        LOGGER.info("Starting upload of %d file(s) to %s:%s", len(paths), self.connected_to[0], self.destination.text())
        Thread(target=self._upload, args=(self.connected_to, tuple(paths), self.destination.text()), daemon=True).start()
        self._poll_timer.start()
        return True

    def _upload(self, connection, paths, destination):
        def progress(name, completed, total, sent_bytes, total_bytes):
            self.results.put(("progress", (name, completed, total, sent_bytes, total_bytes)))
        def verifying(name, completed, total):
            self.results.put(("verifying", (name, completed, total)))
        try:
            saved = upload_files(*connection, paths, destination, self.cancel_upload, progress, on_verifying=verifying)
            self.results.put(("uploaded", saved))
        except (HeadsetError, OSError, ValueError) as error:
            self.results.put(("upload_error", error))

    def _handle_transfer_result(self, kind, result):
        if kind == "verifying":
            if self.uploading and not self.cancel_upload.is_set():
                name, completed, total = result
                # The final file's bytes are sent, including a zero-byte batch.
                if completed + 1 == total:
                    self.progress.set_progress(1)
                self.progress.start_verifying()
                percent = int(self.progress.fraction * 100)
                self.transfer_filename.setText(name)
                self.transfer_percent.setText(f"{percent}%")
                self.transfer_detail.setText(f"Verifying file · {completed}/{total} complete…")
            return
        if kind == "progress":
            if self.uploading and not self.cancel_upload.is_set():
                name, completed, total, sent_bytes, total_bytes = result
                fraction = sent_bytes / total_bytes if total_bytes else 0
                self.progress.set_progress(fraction)
                percent = int(self.progress.fraction * 100)
                self.transfer_filename.setText(name)
                self.transfer_percent.setText(f"{percent}%")
                self.transfer_detail.setText(f"Sending files · {completed}/{total} complete")
            return
        self.uploading = False
        self._set_busy(False)
        self.cancel_button.setEnabled(False)
        self.cancel_button.hide()
        self.progress.set_progress(self.progress.fraction)
        if kind == "uploaded":
            self.progress.set_progress(0)
            self.transfer_percent.setText("0%")
            self.transfer_filename.setText("")
            folder = str(PurePosixPath(result[0]).parent)
            LOGGER.info("Upload completed: %d file(s) to %s", len(result), folder)
            self.destination.setText(folder)
            self._remember_destination()
            self.transfer_detail.setText(f"Sent {len(result)} file(s) to {folder}.")
        else:
            completed = getattr(result, "completed", 0)
            text = "Cancelled" if isinstance(result, TransferCancelled) else "Transfer failed"
            LOGGER.log(
                logging.INFO if isinstance(result, TransferCancelled) else logging.ERROR,
                "%s; %d file(s) completed: %s", text, completed, result,
            )
            self.transfer_detail.setText(f"{text} · {completed} file(s) uploaded. {result}")
        if self.close_requested:
            self.close()

    def cancel_transfer(self):
        self.cancel_upload.set()
        self.progress.set_progress(self.progress.fraction)
        self.cancel_button.setEnabled(False)
        self.transfer_detail.setText("Cancelling transfer and cleaning up…")

    def closeEvent(self, event):
        self._remember_destination()
        self._closing = True
        self._auto_timer.stop()
        self._monitor_connection = self._status_request = None
        if self._power_request is not None:
            self.close_requested = True
            event.ignore()
        elif self.uploading:
            self.close_requested = True
            self.cancel_transfer()
            event.ignore()
        else:
            self._poll_timer.stop()
            self.settings_dialog.reject()
            event.accept()


def main() -> None:
    install_qt_message_handler()
    application = QApplication(sys.argv)
    application.setApplicationVersion(APP_VERSION)
    application.setWindowIcon(QIcon(str(Path(__file__).with_name("ICO") / "icon.ico")))
    window = FrameConnectApp(preferences=DestinationPreferences())
    screen = application.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        window.resize(min(window.width(), available.width() - 40), min(window.height(), available.height() - 60))
    window.show()
    LOGGER.info("Main window ready")
    exit_code = application.exec()
    LOGGER.info("Application exited with code %d", exit_code)
    sys.exit(exit_code)
