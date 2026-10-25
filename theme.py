"""Qt palette and the CSS-like stylesheet for Frame Connect."""

from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget


COLORS = {
    "background": "#171d25", "surface": "#232c38", "border": "#344354",
    "text": "#e6edf5", "muted": "#a7b6c8", "disabled": "#8192a8",
    "input": "#121b27", "button": "#30455d", "accent": "#66c0f4",
    "primary": "#28638c", "dock": "#1c2e44",
}


def apply_dark_theme(app: QApplication) -> None:
    if app.property("frameConnectTheme"):
        return
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, "background"), (QPalette.ColorRole.WindowText, "text"),
        (QPalette.ColorRole.Base, "input"), (QPalette.ColorRole.AlternateBase, "surface"),
        (QPalette.ColorRole.Text, "text"), (QPalette.ColorRole.Button, "button"),
        (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.Highlight, "primary"),
        (QPalette.ColorRole.HighlightedText, "text"), (QPalette.ColorRole.PlaceholderText, "muted"),
        (QPalette.ColorRole.ToolTipBase, "surface"), (QPalette.ColorRole.ToolTipText, "text"),
    ):
        palette.setColor(role, QColor(COLORS[color]))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(COLORS["disabled"]))
    app.setPalette(palette)
    app.setStyleSheet(Path(__file__).with_name("styles.qss").read_text(encoding="utf-8"))
    app.setProperty("frameConnectTheme", True)


class BatteryIcon(QWidget):
    """A vector battery outline with a fill proportional to reported charge."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(48, 32)
        self.setAccessibleName("Battery charge")
        self.set_level(None)

    def set_level(self, percent: int | None, *, charging=False):
        self.percent = percent
        self.charging = percent is not None and charging
        description = "Battery level unavailable" if percent is None else f"{percent}% battery remaining"
        if self.charging:
            description += " · Charging"
        self.setAccessibleDescription(description)
        self.setToolTip(description)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        outline = QColor(COLORS["muted" if self.percent is None else "text"])
        painter.setPen(QPen(outline, 1.7))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(1.5, 5.5, 38, 21), 4, 4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(outline)
        painter.drawRoundedRect(QRectF(40, 12, 4.5, 8), 1.5, 1.5)
        if self.percent is None:
            painter.setPen(QPen(outline, 1.7))
            painter.drawLine(QPointF(15, 16), QPointF(26, 16))
        elif self.percent > 0:
            painter.setBrush(QColor(COLORS["accent"]))
            painter.drawRoundedRect(QRectF(5.5, 9.5, 30 * self.percent / 100, 13), 2, 2)
        if self.charging:
            bolt = QPainterPath()
            bolt.moveTo(22.5, 8.5)
            for x, y in ((15.5, 17), (20, 17), (18.5, 23.5), (26, 14), (21.5, 14)):
                bolt.lineTo(x, y)
            bolt.closeSubpath()
            painter.setPen(QPen(QColor(COLORS["text"]), 1, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(QColor(COLORS["input"]))
            painter.drawPath(bolt)


class ControllerDots(QWidget):
    """Four quarter-capacity indicators underneath the official controller icon."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(64, 10)
        self.set_level(None)

    def set_level(self, percent):
        self.filled = (percent + 24) // 25 if percent is not None else 0
        self.setAccessibleName("Controller battery level")
        self.setAccessibleDescription("Battery unavailable" if percent is None else f"{self.filled} of 4 battery dots · {percent}%")
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(4):
            painter.setBrush(QColor("#78c78e" if index < self.filled else COLORS["border"]))
            painter.drawEllipse(QPointF(14 + index * 12, 5), 3, 3)


class ControllerStatus(QWidget):
    def __init__(self, hand, parent=None):
        super().__init__(parent)
        self.hand = hand
        self.icon_data = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        picture = QVBoxLayout()
        picture.setSpacing(4)
        self.icon = QLabel("—")
        self.icon.setFixedSize(64, 64)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon.setAccessibleName(f"Official {hand} Steam Frame controller icon")
        picture.addWidget(self.icon)
        self.dots = ControllerDots()
        picture.addWidget(self.dots)
        layout.addLayout(picture)
        text = QVBoxLayout()
        text.setSpacing(4)
        title = QLabel(f"{hand.upper()} CONTROLLER")
        title.setObjectName("eyebrow")
        text.addWidget(title)
        self.value = QLabel("—")
        self.value.setObjectName("metricValue")
        text.addWidget(self.value)
        self.detail = QLabel()
        self.detail.setProperty("muted", True)
        text.addWidget(self.detail)
        layout.addLayout(text, 1)
        self.set_status()

    def set_status(self, reading=None, *, message="Waiting for the headset"):
        connected = reading is not None and reading.connected
        percent = reading.percent if connected else None
        self.value.setText(f"{percent}%" if percent is not None else "—")
        self.detail.setText(("Connected" if percent is not None else "Battery unavailable") if connected
                            else "Not connected" if reading is not None else message)
        self.dots.set_level(percent)
        data = reading.icon if reading is not None else None
        if data != self.icon_data:
            self.icon_data = data
            pixmap = QPixmap()
            if data and pixmap.loadFromData(data, "PNG"):
                self.icon.setPixmap(pixmap.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio,
                                                 Qt.TransformationMode.SmoothTransformation))
            else:
                self.icon.setText("—")


class UploadIcon(QWidget):
    """A clean vector arrow and tray, with no external image file."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(52, 52)
        self.setAccessibleName("Upload files")

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = COLORS["accent" if self.isEnabled() else "muted"]
        painter.setPen(QPen(QColor(color), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        arrow = QPainterPath()
        arrow.moveTo(26, 32)
        arrow.lineTo(26, 9)
        arrow.moveTo(16, 19)
        arrow.lineTo(26, 9)
        arrow.lineTo(36, 19)
        arrow.moveTo(10, 34)
        arrow.lineTo(10, 43)
        arrow.lineTo(42, 43)
        arrow.lineTo(42, 34)
        painter.drawPath(arrow)


class ElidedLabel(QLabel):
    """Keep transfer rows stable even with long filenames or error messages."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setText(text)

    def setText(self, text):
        super().setText(text)
        self.setToolTip(text)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.contentsRect().width())
        painter.drawText(self.contentsRect(), int(self.alignment()), text)


class StorageUsageBar(QWidget):
    """A rounded bar showing the fraction of total storage that is used."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Storage usage")
        self.set_usage()

    def set_usage(self, used: int | None = None, total: int | None = None):
        self.fraction = None if used is None or total is None else used / total
        description = (
            "Storage usage unavailable" if self.fraction is None
            else f"{100 * self.fraction:.1f}% of storage used"
        )
        self.setAccessibleDescription(description)
        self.setToolTip(description)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QPainterPath()
        track.addRoundedRect(QRectF(self.rect()), 4, 4)
        painter.fillPath(track, QColor(COLORS["input"]))
        if self.fraction is not None and self.fraction > 0:
            painter.setClipPath(track)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["accent"]))
            painter.drawRoundedRect(QRectF(0, 0, self.width() * self.fraction, self.height()), 4, 4)


class TransferProgressBar(QWidget):
    """Measured byte progress, with a short moving segment during verification."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.fraction = 0.0
        self.verifying = False
        self._verification_elapsed = QElapsedTimer()
        self._animation_timer = QTimer(self)
        self._animation_timer.setInterval(25)
        self._animation_timer.timeout.connect(self.update)
        self.setAccessibleName("File transfer progress")
        self.set_progress(0)

    def set_progress(self, fraction: float):
        self.verifying = False
        self._animation_timer.stop()
        self.fraction = max(0.0, min(1.0, fraction))
        description = f"{int(self.fraction * 100)}% transferred"
        self.setAccessibleDescription(description)
        self.setToolTip(description)
        self.update()

    def start_verifying(self):
        self.verifying = True
        self._verification_elapsed.start()
        self._animation_timer.start()
        self.setAccessibleDescription("Verifying the transferred file")
        self.setToolTip("Verifying the transferred file")
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QPainterPath()
        track.addRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 2.5, 2.5)
        painter.fillPath(track, QColor(COLORS["input"]))
        elapsed = self._verification_elapsed.elapsed() if self.verifying else 0
        # Briefly show the full track before the final verification animation.
        full_pause = 180 if self.fraction == 1 else 0
        animated = self.verifying and elapsed >= full_pause
        if animated or self.fraction > 0:
            painter.save()
            painter.setClipPath(track)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["accent"]))
            if animated:
                width = self.width() * 0.22
                x = ((elapsed - full_pause) % 1400) / 1400 * (self.width() + width) - width
            else:
                x, width = 0, self.width() * self.fraction
            painter.drawRoundedRect(QRectF(x, 0, width, self.height()), 3, 3)
            painter.restore()
        painter.strokePath(track, QPen(QColor("#435770"), 1))

