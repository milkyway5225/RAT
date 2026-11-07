import socket
import json
import struct
import threading

from PyQt5.QtCore import Qt, QEvent

ACTION_PORT = 25758

_QT_KEY_TO_VK = {
    Qt.Key_Escape: 0x1B, Qt.Key_Tab: 0x09, Qt.Key_Backspace: 0x08,
    Qt.Key_Return: 0x0D, Qt.Key_Enter: 0x0D, Qt.Key_Space: 0x20,
    Qt.Key_Left: 0x25, Qt.Key_Up: 0x26, Qt.Key_Right: 0x27, Qt.Key_Down: 0x28,
    Qt.Key_Home: 0x24, Qt.Key_End: 0x23, Qt.Key_PageUp: 0x21, Qt.Key_PageDown: 0x22,
    Qt.Key_Insert: 0x2D, Qt.Key_Delete: 0x2E,
    Qt.Key_Shift: 0x10, Qt.Key_Control: 0x11, Qt.Key_Alt: 0x12,
    Qt.Key_CapsLock: 0x14, Qt.Key_NumLock: 0x90,
    Qt.Key_F1: 0x70, Qt.Key_F2: 0x71, Qt.Key_F3: 0x72, Qt.Key_F4: 0x73,
    Qt.Key_F5: 0x74, Qt.Key_F6: 0x75, Qt.Key_F7: 0x76, Qt.Key_F8: 0x77,
    Qt.Key_F9: 0x78, Qt.Key_F10: 0x79, Qt.Key_F11: 0x7A, Qt.Key_F12: 0x7B,
}


def _qt_key_to_vk(qt_key):
    if Qt.Key_A <= qt_key <= Qt.Key_Z:
        return qt_key
    if Qt.Key_0 <= qt_key <= Qt.Key_9:
        return qt_key
    return _QT_KEY_TO_VK.get(qt_key, 0)


class ControlMixin:
    def _ctl_ensure_state(self):
        if not hasattr(self, "_ctl_sock"):
            self._ctl_sock = None
        if not hasattr(self, "_ctl_lock"):
            self._ctl_lock = threading.Lock()

    def _ctl_connect(self):
        self._ctl_ensure_state()
        if self._ctl_sock is not None:
            return True
        host = self.host_input.text().strip()
        password = self.password_input.text().strip()
        if not host or not password:
            return False
        try:
            sock = socket.create_connection((host, ACTION_PORT), timeout=5)
            sock.settimeout(5)
            sock.sendall(json.dumps(
                {"role": "admin", "token": password},
                separators=(",", ":"),
            ).encode("utf-8") + b"\n")
            reply = bytearray()
            while b"\n" not in reply and len(reply) < 2048:
                part = sock.recv(1)
                if not part:
                    sock.close()
                    return False
                reply.extend(part)
            resp = json.loads(reply.decode("utf-8").strip())
            if not resp.get("ok"):
                sock.close()
                return False
            sock.settimeout(None)
            self._ctl_sock = sock
            return True
        except Exception:
            return False

    def _ctl_disconnect(self):
        self._ctl_ensure_state()
        sock = self._ctl_sock
        self._ctl_sock = None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _ctl_send(self, cmd):
        self._ctl_ensure_state()
        with self._ctl_lock:
            if self._ctl_sock is None and not self._ctl_connect():
                return False
            try:
                payload = json.dumps(cmd, separators=(",", ":")).encode("utf-8")
                self._ctl_sock.sendall(struct.pack("!I", len(payload)) + payload)
                return True
            except Exception:
                self._ctl_disconnect()
                return False

    def _ctl_sync_filter(self):
        mouse_on = bool(self.contol_mouse and self.contol_mouse.isChecked())
        kbd_on = bool(self.control_keyboard and self.control_keyboard.isChecked())
        if mouse_on or kbd_on:
            self.screen_label.installEventFilter(self)
            self.screen_label.setMouseTracking(True)
            self.screen_label.setFocusPolicy(Qt.StrongFocus)
        else:
            self.screen_label.removeEventFilter(self)
            self.screen_label.setMouseTracking(False)
            self.screen_label.setFocusPolicy(Qt.NoFocus)

    def control_keyboard_toggled(self, checked):
        self._ctl_ensure_state()
        if checked:
            if not self._ctl_connect():
                if self.control_keyboard:
                    self.control_keyboard.blockSignals(True)
                    self.control_keyboard.setChecked(False)
                    self.control_keyboard.blockSignals(False)
                return
            self.screen_label.setFocus()
        self._ctl_sync_filter()
        if not self.contol_mouse.isChecked() and not self.control_keyboard.isChecked():
            self._ctl_disconnect()

    def control_mouse_toggled(self, checked):
        self._ctl_ensure_state()
        if checked:
            if not self._ctl_connect():
                if self.contol_mouse:
                    self.contol_mouse.blockSignals(True)
                    self.contol_mouse.setChecked(False)
                    self.contol_mouse.blockSignals(False)
                return
            self.screen_label.setCursor(Qt.CrossCursor)
        else:
            self.screen_label.unsetCursor()
        self._ctl_sync_filter()
        if not self.contol_mouse.isChecked() and not self.control_keyboard.isChecked():
            self._ctl_disconnect()

    def eventFilter(self, obj, event):
        if obj is getattr(self, "screen_label", None):
            et = event.type()
            mouse_on = bool(self.contol_mouse and self.contol_mouse.isChecked())
            kbd_on = bool(self.control_keyboard and self.control_keyboard.isChecked())
            if mouse_on:
                if et == QEvent.MouseMove:
                    self._send_mouse_move(event.pos())
                    return False
                if et == QEvent.MouseButtonPress:
                    self._send_mouse_click(event.pos(), event.button(), True)
                    return True
                if et == QEvent.MouseButtonRelease:
                    self._send_mouse_click(event.pos(), event.button(), False)
                    return True
                if et == QEvent.Wheel:
                    self._send_mouse_wheel(event.angleDelta().y())
                    return True
            if kbd_on:
                if et == QEvent.KeyPress:
                    if not event.isAutoRepeat():
                        vk = event.nativeVirtualKey()
                        if not vk:
                            vk = _qt_key_to_vk(event.key())
                        self._send_key(vk, True)
                    return True
                if et == QEvent.KeyRelease:
                    if not event.isAutoRepeat():
                        vk = event.nativeVirtualKey()
                        if not vk:
                            vk = _qt_key_to_vk(event.key())
                        self._send_key(vk, False)
                    return True
        return False

    def _widget_to_remote(self, pos):
        pm = self.screen_label.pixmap()
        if pm is None or pm.isNull():
            return None
        lw = self.screen_label.width()
        lh = self.screen_label.height()
        pw = pm.width()
        ph = pm.height()
        if lw <= 0 or lh <= 0 or pw <= 0 or ph <= 0:
            return None
        ox = (lw - pw) // 2
        oy = (lh - ph) // 2
        rx = pos.x() - ox
        ry = pos.y() - oy
        if 0 <= rx < pw and 0 <= ry < ph:
            return rx, ry
        return None

    def _send_mouse_move(self, pos):
        xy = self._widget_to_remote(pos)
        if xy is None:
            return
        self._ctl_send({"action": "mouse_move", "x": xy[0], "y": xy[1]})

    def _send_mouse_click(self, pos, button, pressed):
        xy = self._widget_to_remote(pos)
        if xy is None:
            return
        if button == Qt.LeftButton:
            btn = "left"
        elif button == Qt.RightButton:
            btn = "right"
        elif button == Qt.MiddleButton:
            btn = "middle"
        else:
            return
        self._ctl_send({
            "action": "mouse_click",
            "x": xy[0], "y": xy[1],
            "button": btn, "pressed": bool(pressed),
        })

    def _send_mouse_wheel(self, delta):
        if not delta:
            return
        self._ctl_send({"action": "mouse_wheel", "delta": int(delta)})

    def _send_key(self, vk, pressed):
        if not vk:
            return
        self._ctl_send({"action": "key", "vk": int(vk), "pressed": bool(pressed)})