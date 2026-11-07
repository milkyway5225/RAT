import socket
import struct
import json
import threading
import sounddevice as sd
from PyQt5.QtWidgets import QMessageBox, QInputDialog

MAX_AUDIO_PACKET = 256 * 1024
SAMPLE_RATE = 16000
CHANNELS = 1
BYTES_PER_SAMPLE = 2

ACTION_PORT = 25758
MAX_ACTION_PACKET = 32 * 1024 * 1024


def recv_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


class AudioMixin:
    # ---------- actions: короткая команда без ответа ----------

    def _send_action_command(self, action, value=None):
        host = self.host_input.text().strip()
        password = self.password_input.text().strip()
        if not host or not password:
            return False, "Не заполнены адрес сервера и/или пароль"

        sock = None
        try:
            sock = socket.create_connection((host, ACTION_PORT), timeout=5)
            sock.settimeout(5)
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
                    return False, "Сервер закрыл соединение"
                reply.extend(part)

            try:
                response = json.loads(reply.decode("utf-8").strip())
            except json.JSONDecodeError:
                return False, "Некорректный ответ сервера"

            if not response.get("ok"):
                return False, response.get("error", "Сервер отклонил подключение")

            cmd = {"action": action}
            if value is not None:
                cmd["value"] = value
            payload = json.dumps(cmd, separators=(",", ":")).encode("utf-8")
            sock.sendall(struct.pack("!I", len(payload)) + payload)
            return True, None
        except Exception as exc:
            return False, str(exc)
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

    # ---------- actions: команда с ожиданием ответа ----------

    def _action_request(self, action, value=None, timeout=15):
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

    # ---------- меню «Звук» ----------

    def toggle_mute(self, checked):
        ok, err = self._send_action_command("mute_mic", bool(checked))
        if ok:
            self.mute_action.setText(
                "Включить звук микрофона" if checked
                else "Заглушить звук микрофона"
            )
            self.audio_status_label.setText(
                "Микрофон клиента заглушён" if checked
                else "Микрофон клиента включён"
            )
        else:
            self.audio_status_label.setText(f"Ошибка команды: {err}")
            if self.mute_action:
                self.mute_action.blockSignals(True)
                self.mute_action.setChecked(not checked)
                self.mute_action.blockSignals(False)

    def mute_screen_toggled(self, checked):
        ok, err = self._send_action_command("mute_system", bool(checked))
        if ok:
            self.mute_screen.setText(
                "Включить звук экрана" if checked
                else "Заглушить звук экрана"
            )
            self.audio_status_label.setText(
                "Звук экрана клиента заглушён" if checked
                else "Звук экрана клиента включён"
            )
        else:
            self.audio_status_label.setText(f"Ошибка команды: {err}")
            if self.mute_screen:
                self.mute_screen.blockSignals(True)
                self.mute_screen.setChecked(not checked)
                self.mute_screen.blockSignals(False)

    def reproduce_sound_toggled(self, checked):
        """Диалог выбора mp3 из папки sounds на сервере и воспроизведение у клиента."""
        if not checked:
            return
        try:
            reply, err = self._action_request("list_sounds")
            if err:
                self.audio_status_label.setText(f"Ошибка списка звуков: {err}")
                return
            sounds = (reply or {}).get("sounds", [])
            if not sounds:
                self.audio_status_label.setText("Папка sounds пуста или недоступна")
                return

            name, ok = QInputDialog.getItem(
                self, "Воспроизвести звук", "Файл:", sounds, 0, False,
            )
            if not ok or not name:
                return

            reply2, err2 = self._action_request("play_sound", name)
            if err2:
                self.audio_status_label.setText(f"Ошибка: {err2}")
            elif not (reply2 or {}).get("ok"):
                self.audio_status_label.setText(
                    f"Ошибка: {(reply2 or {}).get('error', 'unknown')}"
                )
            else:
                self.audio_status_label.setText(f"Воспроизведение: {name}")
        finally:
            if self.reproduce_sound:
                self.reproduce_sound.blockSignals(True)
                self.reproduce_sound.setChecked(False)
                self.reproduce_sound.blockSignals(False)

    # ---------- локальное проигрывание аудиопотока ----------

    def toggle_audio(self):
        if self.audio_thread and self.audio_thread.is_alive():
            self.stop_audio()
        else:
            self.start_audio()

    def start_audio(self):
        host = self.host_input.text().strip()
        try:
            port = int(self.audio_port_input.text().strip())
            if not host or not 1 <= port <= 65535:
                raise ValueError("Укажи корректный адрес сервера и порт звука")
        except ValueError as exc:
            QMessageBox.warning(self, "Настройки звука", str(exc))
            return

        self.audio_stop_event.clear()
        self.audio_connect_button.setEnabled(False)
        self.set_audio_status("Подключение звука...")
        self.audio_thread = threading.Thread(
            target=self.audio_loop,
            args=(host, port, self.password_input.text().strip()),
            daemon=True,
        )
        self.audio_thread.start()

    def audio_loop(self, host, port, password):
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.settimeout(5)
            hello = json.dumps(
                {"role": "receiver", "token": password}, separators=(",", ":")
            ).encode("utf-8") + b"\n"
            sock.sendall(hello)

            reply = bytearray()
            while b"\n" not in reply and len(reply) < 2048:
                part = sock.recv(1)
                if not part:
                    raise ConnectionError("Сервер закрыл соединение во время авторизации")
                reply.extend(part)
            response = json.loads(reply.decode("utf-8").strip())
            if not response.get("ok"):
                raise ConnectionError(response.get("error", "Сервер отклонил подключение"))

            sock.settimeout(None)
            self.audio_sock = sock
            self.signals.audio_status.emit("Звук подключён")

            with sd.RawOutputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16"
            ) as output:
                while not self.audio_stop_event.is_set():
                    header = recv_exact(sock, 4)
                    if header is None:
                        raise ConnectionError("Аудиосервер отключился")
                    size = struct.unpack("!I", header)[0]
                    if size <= 0 or size > MAX_AUDIO_PACKET or size % (CHANNELS * BYTES_PER_SAMPLE):
                        raise ValueError(f"Некорректный размер аудиопакета: {size}")
                    payload = recv_exact(sock, size)
                    if payload is None:
                        raise ConnectionError("Аудиопоток завершён")
                    output.write(payload)
        except Exception as exc:
            if not self.audio_stop_event.is_set():
                self.signals.audio_status.emit(f"Ошибка звука: {exc}")
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
            self.audio_sock = None
            self.signals.audio_status.emit("Звук отключён")
            self.audio_stop_event.set()

    def set_audio_status(self, text):
        self.audio_status_label.setText(text)
        if text.startswith("Звук подключён") or text == "Звук заглушён":
            self.audio_connect_button.setText("Отключить звук")
            self.audio_connect_button.setEnabled(True)
        elif text.startswith("Ошибка звука:") or text == "Звук отключён":
            self.audio_connect_button.setText("Подключить звук")
            self.audio_connect_button.setEnabled(True)

    def stop_audio(self):
        self.audio_stop_event.set()
        sock, self.audio_sock = self.audio_sock, None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self.audio_connect_button.setText("Отключение звука...")
        self.audio_connect_button.setEnabled(False)