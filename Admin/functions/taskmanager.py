from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLineEdit, QLabel, QMessageBox, QInputDialog,
)
from PyQt5.QtCore import Qt


class TaskManagerMixin:
    def _tm_request_processes(self, timeout=20):
        reply, err = self._action_request("list_processes", None, timeout=timeout)
        if err:
            return None, err
        if not (reply or {}).get("ok"):
            return None, (reply or {}).get("error", "unknown")
        return (reply or {}).get("processes", []), None

    def open_task_manager_toggled(self, checked):
        if not checked:
            return
        try:
            procs, err = self._tm_request_processes()
            if err:
                self.audio_status_label.setText(f"Ошибка процессов: {err}")
                return
            if not procs:
                self.audio_status_label.setText("Список процессов пуст")
                return

            dlg = QDialog(self)
            dlg.setWindowTitle(f"Процессы клиента ({len(procs)})")
            dlg.resize(700, 520)
            layout = QVBoxLayout(dlg)

            filter_row = QHBoxLayout()
            filter_row.addWidget(QLabel("Фильтр:"))
            filter_input = QLineEdit()
            filter_input.setPlaceholderText("например, chrome или 1234")
            filter_row.addWidget(filter_input)
            layout.addLayout(filter_row)

            list_widget = QListWidget()
            for p in procs:
                item = QListWidgetItem(
                    f"{p['pid']:>7}  {p['name']}  {p.get('mem', '')}"
                )
                item.setData(Qt.UserRole, p)
                list_widget.addItem(item)
            layout.addWidget(list_widget)

            def _apply_filter():
                text = filter_input.text().lower().strip()
                for i in range(list_widget.count()):
                    item = list_widget.item(i)
                    item.setHidden(bool(text) and text not in item.text().lower())

            filter_input.textChanged.connect(_apply_filter)

            btn_row = QHBoxLayout()
            btn_kill = QPushButton("Завершить выбранный")
            btn_refresh = QPushButton("Обновить")
            btn_close = QPushButton("Закрыть")
            btn_row.addWidget(btn_kill)
            btn_row.addWidget(btn_refresh)
            btn_row.addWidget(btn_close)
            layout.addLayout(btn_row)

            def _kill_selected():
                item = list_widget.currentItem()
                if item is None:
                    return
                p = item.data(Qt.UserRole)
                name = p.get("name")
                pid = p.get("pid")
                r = QMessageBox.question(
                    dlg, "Завершить процесс",
                    f"Завершить процесс:\n{name} (PID {pid})?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if r != QMessageBox.Yes:
                    return
                ok, err2 = self._send_action_command("kill_process", str(pid))
                if ok:
                    self.audio_status_label.setText(f"Завершён {name} (PID {pid})")
                else:
                    self.audio_status_label.setText(f"Ошибка: {err2}")
                dlg.accept()

            btn_kill.clicked.connect(_kill_selected)
            btn_refresh.clicked.connect(dlg.accept)
            btn_close.clicked.connect(dlg.reject)

            dlg.exec_()
        finally:
            if self.open_task_manager:
                self.open_task_manager.blockSignals(True)
                self.open_task_manager.setChecked(False)
                self.open_task_manager.blockSignals(False)

    def close_task_process_toggled(self, checked):
        if not checked:
            return
        try:
            name, ok = QInputDialog.getText(
                self, "Закрыть процесс",
                "Имя процесса или PID (например, notepad.exe или 1234):",
            )
            if not ok or not name.strip():
                return
            ok2, err = self._send_action_command("kill_process", name.strip())
            if ok2:
                self.audio_status_label.setText(f"Команда kill: {name.strip()}")
            else:
                self.audio_status_label.setText(f"Ошибка: {err}")
        finally:
            if self.close_task_process:
                self.close_task_process.blockSignals(True)
                self.close_task_process.setChecked(False)
                self.close_task_process.blockSignals(False)

    def open_task_process_toggled(self, checked):
        if not checked:
            return
        try:
            name, ok = QInputDialog.getText(
                self, "Открыть процесс",
                "Имя или путь программы (например, notepad.exe):",
            )
            if not ok or not name.strip():
                return
            ok2, err = self._send_action_command("run_process", name.strip())
            if ok2:
                self.audio_status_label.setText(f"Команда run: {name.strip()}")
            else:
                self.audio_status_label.setText(f"Ошибка: {err}")
        finally:
            if self.open_task_process:
                self.open_task_process.blockSignals(True)
                self.open_task_process.setChecked(False)
                self.open_task_process.blockSignals(False)