import struct
import json
import socket
from PyQt5.QtWidgets import QInputDialog

ACTION_PORT = 25758
MAX_ACTION_PACKET = 96 * 1024 * 1024


def recv_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


class MediaMixin:
    def _media_request(self, action, value=None, timeout=60):
        host = self.host_input.text().strip()
        password = self.password_input.text().strip()
        if not host or not password:
            return None, "Не заполнены адрес сервера и/или пароль"

        sock = None
        try:
            sock = socket.create_connection((host, ACTION_PORT), timeout=5)
            sock.settimeout(timeout)
            sock.sendall(
                json.dumps(
                    {"role": "admin", "token": password},
                    separators=(",", ":"),
                ).encode("utf-8") + b"\n"
            )

            reply = bytearray()
            while b"\n" not in reply and len(reply) < 2048:
                part = sock.recv(1)
                if not part:
                    return None, "Сервер закрыл соединение"
                reply.extend(part)

            try:
                auth = json.loads(reply.decode("utf-8").strip())
            except json.JSONDecodeError:
                return None, "Некорректный ответ сервера"
            if not auth.get("ok"):
                return None, auth.get("error", "Сервер отклонил подключение")

            cmd = {"action": action}
            if value is not None:
                cmd["value"] = value
            payload = json.dumps(cmd, separators=(",", ":")).encode("utf-8")
            sock.sendall(struct.pack("!I", len(payload)) + payload)

            header = recv_exact(sock, 4)
            if header is None:
                return None, "Сервер не ответил"
            size = struct.unpack("!I", header)[0]
            if size <= 0 or size > MAX_ACTION_PACKET:
                return None, "Некорректный размер ответа"
            body = recv_exact(sock, size)
            if body is None:
                return None, "Сервер не ответил"
            try:
                return json.loads(body.decode("utf-8")), None
            except json.JSONDecodeError:
                return None, "Некорректный JSON в ответе"
        except Exception as exc:
            return None, str(exc)
        finally:
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

    def _open_media_impl(self, checked, fullscreen):
        if not checked:
            return
        try:
            reply, err = self._media_request("list_media")
            if err:
                self.audio_status_label.setText(f"Ошибка списка медиа: {err}")
                return
            files = (reply or {}).get("media", [])
            if not files:
                self.audio_status_label.setText("Папка media пуста или недоступна")
                return

            title = "Открыть на весь экран" if fullscreen else "Открыть медиа"
            name, ok = QInputDialog.getItem(
                self, title, "Файл:", files, 0, False,
            )
            if not ok or not name:
                return

            reply2, err2 = self._media_request(
                "play_media",
                {"name": name, "fullscreen": bool(fullscreen)},
                timeout=120,
            )
            if err2:
                self.audio_status_label.setText(f"Ошибка: {err2}")
            elif not (reply2 or {}).get("ok"):
                self.audio_status_label.setText(
                    f"Ошибка: {(reply2 or {}).get('error', 'unknown')}"
                )
            else:
                suffix = " (F11)" if fullscreen else ""
                self.audio_status_label.setText(f"Отправлено: {name}{suffix}")
        finally:
            target = self.fullsreen_media if fullscreen else self.open_media
            if target:
                target.blockSignals(True)
                target.setChecked(False)
                target.blockSignals(False)

    def open_media_toggled(self, checked):
        self._open_media_impl(checked, fullscreen=False)

    def fullscreen_media_toggled(self, checked):
        self._open_media_impl(checked, fullscreen=True)