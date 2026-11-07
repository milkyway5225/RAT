from PyQt5.QtWidgets import QMessageBox


class MachineControlMixin:
    def _machine_send(self, action, confirm_text):
        reply = QMessageBox.question(
            self,
            "Подтверждение",
            confirm_text,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return False

        ok, err = self._send_action_command(action, True)
        if ok:
            self.audio_status_label.setText(f"Команда отправлена: {action}")
        else:
            self.audio_status_label.setText(f"Ошибка команды: {err}")
        return ok

    def machine_restart_toggled(self, checked):
        if checked:
            self._machine_send(
                "machine_restart",
                "Перезагрузить компьютер клиента?\n"
                "Это разорвёт все соединения.",
            )
        if self.machine_restart:
            self.machine_restart.blockSignals(True)
            self.machine_restart.setChecked(False)
            self.machine_restart.blockSignals(False)

    def machine_shutdown_toggled(self, checked):
        if checked:
            self._machine_send(
                "machine_shutdown",
                "Выключить компьютер клиента?\n"
                "После этого подключиться можно будет только после ручного включения.",
            )
        if self.machine_shutdown:
            self.machine_shutdown.blockSignals(True)
            self.machine_shutdown.setChecked(False)
            self.machine_shutdown.blockSignals(False)