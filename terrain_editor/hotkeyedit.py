"""Поле «нажмите сочетание»: запоминает клавишу или кнопку мыши с Ctrl/Shift/Alt.

* щёлкнуть по полю и нажать сочетание (F8, Shift+F9, Ctrl+Alt+P…);
* кнопки мыши: щёлкнуть по полю **нужной кнопкой** (правой, средней, боковыми), можно с
  зажатым Alt/Ctrl/Shift — получится «ПКМ», «Alt+ПКМ», «Мышь4»;
* Backspace/Delete — вернуть значение по умолчанию, Esc — отменить ввод;
* ``allow_modifier=True`` (клавиши камеры): нажать и отпустить один Ctrl/Shift/Alt — запишется
  сам модификатор; Tab тоже можно записать (обычно он переключает поля);
* ``allow_left=True`` (обзор камеры): левая кнопка по уже ждущему полю — «ЛКМ» (первый щелчок
  левой — как обычно, начать ввод).

Текст поля — в том же виде, что понимает ``winbuldozer.parse_hotkey`` (и хранится в настройках).
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QLineEdit

from . import winbuldozer as wb

PROMPT = "нажмите сочетание…"

_MOUSE_VK = {Qt.LeftButton: 0x01, Qt.RightButton: 0x02, Qt.MiddleButton: 0x04, Qt.XButton1: 0x05, Qt.XButton2: 0x06}
_MODIFIER_KEYS = {Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_AltGr}
_MODIFIER_VK = {Qt.Key_Shift: 0x10, Qt.Key_Control: 0x11, Qt.Key_Alt: 0x12}
_QT_KEY_VK = {
    Qt.Key_Insert: 0x2D, Qt.Key_Delete: 0x2E, Qt.Key_Home: 0x24, Qt.Key_End: 0x23,
    Qt.Key_PageUp: 0x21, Qt.Key_PageDown: 0x22, Qt.Key_Space: 0x20, Qt.Key_Tab: 0x09,
    Qt.Key_Return: 0x0D, Qt.Key_Enter: 0x0D, Qt.Key_Pause: 0x13, Qt.Key_QuoteLeft: 0xC0,
    Qt.Key_AsciiTilde: 0xC0,
}


def qt_key_to_vk(key: int, keypad: bool = False) -> int | None:
    """Код клавиши Qt -> виртуальный код Windows (для не-Windows и тестов)."""
    if Qt.Key_F1 <= key <= Qt.Key_F24:
        return 0x70 + (key - Qt.Key_F1)
    if Qt.Key_0 <= key <= Qt.Key_9:
        return (0x60 if keypad else 0x30) + (key - Qt.Key_0)
    if Qt.Key_A <= key <= Qt.Key_Z:
        return 0x41 + (key - Qt.Key_A)
    return _QT_KEY_VK.get(Qt.Key(key) if not isinstance(key, Qt.Key) else key)


def _mods(modifiers) -> tuple[bool, bool, bool]:
    return (bool(modifiers & Qt.ControlModifier), bool(modifiers & Qt.ShiftModifier),
            bool(modifiers & Qt.AltModifier))


class HotkeyEdit(QLineEdit):
    hotkeyChanged = Signal(str)

    def __init__(self, default: str, parent=None, allow_modifier: bool = False,
                 allow_left: bool = False) -> None:
        super().__init__(parent)
        self.default = default
        self.allow_modifier = allow_modifier
        self.allow_left = allow_left
        self._armed = False                         # левая уже щёлкнула — следующая = «ЛКМ»
        self._pending_mod = None                    # нажат один модификатор, ждём отпускания
        self._before = ""
        self.setReadOnly(True)                      # печатать нельзя — только нажимать
        self.setContextMenuPolicy(Qt.NoContextMenu)  # правая кнопка = записать «ПКМ»
        self.setAlignment(Qt.AlignCenter)
        self.setPlaceholderText(PROMPT)
        self.setToolTip("Щёлкните и нажмите сочетание (можно с Ctrl/Shift/Alt).\n"
                        "Кнопка мыши — щёлкните по полю нужной кнопкой (можно с Alt/Ctrl/Shift).\n"
                        f"Backspace — по умолчанию ({default}), Esc — отмена."
                        + ("\nЛевая кнопка: щёлкните левой ещё раз по ждущему полю." if allow_left else ""))

    # ---- запись
    def commit(self, vk: int, ctrl: bool, shift: bool, alt: bool) -> None:
        self._pending_mod = None
        self._armed = False
        text = wb.Hotkey(vk, ctrl, shift, alt).text()
        if wb.parse_hotkey(text) is None:           # неизвестная клавиша — не записываем
            self.cancel()
            return
        self._before = text
        self.setText(text)
        self.clearFocus()
        self.hotkeyChanged.emit(text)

    def cancel(self) -> None:
        self._armed = False
        self.setText(self._before)
        self.clearFocus()

    # ---- события
    def focusInEvent(self, event) -> None:          # noqa: N802
        self._before = self.text()
        self.setText("")
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:         # noqa: N802
        self._armed = False
        if not self.text() or self.text().endswith("…"):
            self.setText(self._before)
        super().focusOutEvent(event)

    def event(self, event) -> bool:                 # noqa: D401 - Qt
        # Tab/Shift+Tab Qt отдаёт переключению полей до keyPressEvent — забираем сами
        if (self.allow_modifier and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Tab, Qt.Key_Backtab)):
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:         # noqa: N802
        key = event.key()
        ctrl, shift, alt = _mods(event.modifiers())
        if key in _MODIFIER_KEYS:                   # пока только модификаторы — подсказка
            held = [n for n, on in (("Ctrl", ctrl), ("Shift", shift), ("Alt", alt)) if on]
            self.setText("+".join(held) + "+…" if held else "")
            self._pending_mod = key if (self.allow_modifier and len(held) == 1) else None
            return
        self._pending_mod = None
        if key == Qt.Key_Backtab:                   # Shift+Tab
            key = Qt.Key_Tab
        if key == Qt.Key_Escape:
            self.cancel()
            return
        if key in (Qt.Key_Backspace, Qt.Key_Delete) and not (ctrl or shift or alt):
            vk_default = wb.parse_hotkey(self.default)
            self.commit(vk_default.vk, vk_default.ctrl, vk_default.shift, vk_default.alt)
            return
        vk = None
        if sys.platform.startswith("win"):
            vk = event.nativeVirtualKey() or None   # раскладка не важна: «Ф» = клавиша A
        if vk is None:
            vk = qt_key_to_vk(key, bool(event.modifiers() & Qt.KeypadModifier))
        if vk is None:
            self.setText("эта клавиша не подходит")
            return
        self.commit(vk, ctrl, shift, alt)

    def keyReleaseEvent(self, event) -> None:       # noqa: N802
        if (self.allow_modifier and self._pending_mod is not None
                and event.key() == self._pending_mod and self._pending_mod in _MODIFIER_VK):
            self.commit(_MODIFIER_VK[self._pending_mod], False, False, False)   # один Ctrl/Shift/Alt
            return
        if event.key() in _MODIFIER_KEYS and self.hasFocus():
            ctrl, shift, alt = _mods(event.modifiers())
            held = [n for n, on in (("Ctrl", ctrl), ("Shift", shift), ("Alt", alt)) if on]
            self.setText("+".join(held) + "+…" if held else "")
        super().keyReleaseEvent(event)

    def mousePressEvent(self, event) -> None:       # noqa: N802
        vk = _MOUSE_VK.get(event.button())
        if vk == 0x01:
            if not (self.allow_left and self._armed):  # левая — просто начать ввод
                self._armed = self.allow_left
                super().mousePressEvent(event)
                return
        if vk is None:
            super().mousePressEvent(event)
            return
        self._before = self._before if self.hasFocus() else self.text()
        self.commit(vk, *_mods(event.modifiers()))
