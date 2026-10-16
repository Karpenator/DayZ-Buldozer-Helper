"""Диалоги: без родителя, поверх всех окон, по центру экрана (а не под окном над Buldozer)."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from terrain_editor import dialogs


def _app():
    return QApplication.instance() or QApplication([])


def _assert_centered(w, screen):
    area = screen.availableGeometry()
    c = w.frameGeometry().center()
    assert abs(c.x() - area.center().x()) <= 3 and abs(c.y() - area.center().y()) <= 3


def test_message_box_is_topmost_parentless_and_centered():
    app = _app()
    overlay = QWidget(None, Qt.Tool | Qt.WindowStaysOnTopHint)    # узкое окно у правого края
    overlay.setGeometry(1000, 50, 300, 700)
    overlay.show()
    box = dialogs.CenteredMessageBox(QMessageBox.Question, "Экспорт", "Очистить список?\n" * 4,
                                     QMessageBox.Yes | QMessageBox.No, overlay)
    assert box.parent() is None
    assert box.windowFlags() & Qt.WindowStaysOnTopHint
    assert box.windowModality() == Qt.ApplicationModal
    assert box.button(QMessageBox.Yes).text() == "Да" and box.button(QMessageBox.No).text() == "Нет"
    box.show()
    app.processEvents()
    _assert_centered(box, overlay.screen())
    assert box.windowFlags() & Qt.WindowStaysOnTopHint          # не сбросился при показе
    box.close()
    overlay.close()


def test_file_dialog_is_centered_and_russian(tmp_path):
    app = _app()
    dlg = dialogs.CenteredFileDialog("Экспорт", str(tmp_path))
    assert dlg.parent() is None and dlg.testOption(dlg.Option.DontUseNativeDialog)
    assert dlg.windowFlags() & Qt.WindowStaysOnTopHint
    assert dlg.labelText(dlg.DialogLabel.Reject) == "Отмена"
    dlg.show()
    app.processEvents()
    _assert_centered(dlg, dialogs.target_screen(None))
    dlg.close()


def test_ask_returns_bool(monkeypatch):
    _app()
    monkeypatch.setattr(dialogs.CenteredMessageBox, "exec", lambda self: QMessageBox.Yes)
    assert dialogs.ask(None, "t", "x") is True
    monkeypatch.setattr(dialogs.CenteredMessageBox, "exec", lambda self: QMessageBox.No)
    assert dialogs.ask(None, "t", "x") is False
