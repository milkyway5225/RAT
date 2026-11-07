from io import BytesIO
from PIL import Image
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QImage, QPixmap


class ScreenMixin:
    def receive_frame(self, client_id, data):
        if client_id != self.selected_client:
            return
        try:
            with Image.open(BytesIO(data)) as source:
                image = source.convert("RGB")
            width, height = image.size
            qimage = QImage(
                image.tobytes(), width, height, width * 3, QImage.Format_RGB888
            ).copy()
            self.last_frame = QPixmap.fromImage(qimage)
            self.render_frame()
        except Exception as exc:
            self.set_status(f"Ошибка изображения: {exc}")

    def render_frame(self):
        if self.last_frame and not self.last_frame.isNull():
            self.screen_label.setText("")
            self.screen_label.setPixmap(self.last_frame.scaled(
                self.screen_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            ))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.render_frame()