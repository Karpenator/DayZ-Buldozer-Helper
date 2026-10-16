"""Диалоги (вопросы, сообщения, выбор файла/папки) — по центру экрана и поверх всех окон.

Окно над Buldozer висит «поверх всех» (Qt.WindowStaysOnTopHint). Диалог, привязанный к нему,
Qt ставит по центру этого узкого окна — и обычный диалог оказывается *под* ним. Поэтому
все диалоги программы создаются без родителя, сами держатся поверх всех окон и
центрируются на экране, где находится окно-«источник» (Buldozer/наше окно), иначе — где курсор.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QCursor, QGuiApplication, QScreen
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from .dayzpaths import DEFAULT_WORK_DRIVE

YES_TEXT, NO_TEXT, OK_TEXT, CANCEL_TEXT = "Да", "Нет", "ОК", "Отмена"


def target_screen(ref: QWidget | None = None) -> QScreen | None:
    """Экран для диалога: где видимое окно-источник, иначе где курсор, иначе основной."""
    if ref is not None and ref.isVisible() and ref.screen() is not None:
        return ref.screen()
    return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()


def center_on_screen(widget: QWidget, ref: QWidget | None = None) -> None:
    screen = target_screen(ref)
    if screen is None:
        return
    area = screen.availableGeometry()
    size = widget.frameGeometry().size() if widget.isVisible() else widget.size()
    x = area.x() + max(0, (area.width() - size.width()) // 2)
    y = area.y() + max(0, (area.height() - size.height()) // 2)
    widget.move(x, y)


class _Centered:
    """Примесь: окно без родителя, поверх всех, центрируется при показе.

    Центрируем в showEvent *после* базового класса: QMessageBox подгоняет свой размер именно
    там, а на экран окно попадает уже после showEvent — поэтому «прыжка» не видно.
    """

    def _setup_centered(self, ref: QWidget | None) -> None:
        self._ref = ref
        # Порядок важен: QMessageBox.setWindowModality() пересоздаёт флаги окна и сбрасывает
        # «поверх всех», поэтому модальность — сначала, флаг — после.
        self.setWindowModality(Qt.ApplicationModal)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

    def showEvent(self, event) -> None:          # noqa: N802 (Qt)
        super().showEvent(event)
        center_on_screen(self, self._ref)
        # Рамку с заголовком Windows добавляет уже после показа — подправим по ней.
        QTimer.singleShot(0, self._recenter)
        self.raise_()
        self.activateWindow()


    def _recenter(self) -> None:
        try:
            if self.isVisible():
                center_on_screen(self, self._ref)
        except RuntimeError:                     # окно уже закрыто и удалено
            pass


class CenteredMessageBox(_Centered, QMessageBox):
    def __init__(self, icon: QMessageBox.Icon, title: str, text: str,
                 buttons: QMessageBox.StandardButton, ref: QWidget | None = None) -> None:
        QMessageBox.__init__(self, icon, title, text, buttons, None)
        self._setup_centered(ref)
        for button, label in ((QMessageBox.Yes, YES_TEXT), (QMessageBox.No, NO_TEXT),
                              (QMessageBox.Ok, OK_TEXT), (QMessageBox.Cancel, CANCEL_TEXT)):
            b = self.button(button)
            if b is not None:
                b.setText(label)


class CenteredFileDialog(_Centered, QFileDialog):
    """Диалог Qt (не системный): только его можно гарантированно поставить по центру и поверх."""

    def __init__(self, title: str, start: str = "", ref: QWidget | None = None) -> None:
        QFileDialog.__init__(self, None, title, start)
        self._setup_centered(ref)
        self.setOption(QFileDialog.DontUseNativeDialog, True)
        self.setLabelText(QFileDialog.LookIn, "Папка:")
        self.setLabelText(QFileDialog.FileName, "Имя:")
        self.setLabelText(QFileDialog.FileType, "Тип файлов:")
        self.setLabelText(QFileDialog.Reject, CANCEL_TEXT)
        self.setSidebarUrls(_sidebar_urls())
        screen = target_screen(ref)
        area = screen.availableGeometry() if screen is not None else None
        self.resize(min(860, int(area.width() * 0.9)) if area else 860,
                    min(520, int(area.height() * 0.8)) if area else 520)


def _sidebar_urls() -> list[QUrl]:
    places = [DEFAULT_WORK_DRIVE]
    for loc in (QStandardPaths.DesktopLocation, QStandardPaths.DocumentsLocation,
                QStandardPaths.HomeLocation):
        places.append(QStandardPaths.writableLocation(loc))
    out, seen = [], set()
    for p in places:
        if p and Path(p).is_dir() and p.lower() not in seen:
            seen.add(p.lower())
            out.append(QUrl.fromLocalFile(p))
    return out


# ------------------------------------------------------------------ короткие функции
def ask(ref: QWidget | None, title: str, text: str, default_yes: bool = True) -> bool:
    box = CenteredMessageBox(QMessageBox.Question, title, text, QMessageBox.Yes | QMessageBox.No, ref)
    box.setDefaultButton(QMessageBox.Yes if default_yes else QMessageBox.No)
    return box.exec() == QMessageBox.Yes


def info(ref: QWidget | None, title: str, text: str) -> None:
    CenteredMessageBox(QMessageBox.Information, title, text, QMessageBox.Ok, ref).exec()


def error(ref: QWidget | None, title: str, text: str) -> None:
    CenteredMessageBox(QMessageBox.Critical, title, text, QMessageBox.Ok, ref).exec()


def save_file(ref: QWidget | None, title: str, start: str, name_filter: str,
              suffix: str = "txt") -> str:
    """Путь для сохранения или ''."""
    dlg = CenteredFileDialog(title, start, ref)
    dlg.setAcceptMode(QFileDialog.AcceptSave)
    dlg.setFileMode(QFileDialog.AnyFile)
    dlg.setNameFilter(name_filter)
    dlg.setDefaultSuffix(suffix)
    dlg.setLabelText(QFileDialog.Accept, "Сохранить")
    if Path(start).suffix:
        dlg.selectFile(start)
    if dlg.exec() and dlg.selectedFiles():
        return dlg.selectedFiles()[0]
    return ""


def pick_folder(ref: QWidget | None, title: str, start: str) -> str:
    """Выбранная папка или ''."""
    dlg = CenteredFileDialog(title, start, ref)
    dlg.setFileMode(QFileDialog.Directory)
    dlg.setOption(QFileDialog.ShowDirsOnly, True)
    dlg.setLabelText(QFileDialog.Accept, "Выбрать")
    if dlg.exec() and dlg.selectedFiles():
        return dlg.selectedFiles()[0]
    return ""
