"""Сочетания: русские названия кнопок мыши, «удерживаемые» сочетания, поле записи сочетания."""
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from terrain_editor import winbuldozer as wb
from terrain_editor.hotkeyedit import HotkeyEdit, qt_key_to_vk


def test_mouse_buttons_parse_and_show_in_russian():
    assert wb.parse_hotkey("ПКМ") == wb.Hotkey(0x02)
    assert wb.parse_hotkey("alt+rmb") == wb.parse_hotkey("Alt+ПКМ") == wb.Hotkey(0x02, alt=True)
    assert wb.parse_hotkey("мышь4").text() == "Мышь4"
    assert wb.parse_hotkey("Ctrl+СКМ").text() == "Ctrl+СКМ"
    assert wb.parse_hotkey("pageup").text() == "PageUp" and wb.parse_hotkey("num5").text() == "Num5"
    for text in ("F8", "Shift+F9", "Ctrl+Alt+P", "Insert", "Alt+ПКМ", "Мышь5"):
        assert wb.parse_hotkey(wb.parse_hotkey(text).text()).text() == text


def test_hold_match_prefers_more_specific_combo():
    combos = (("lift", wb.parse_hotkey("Alt+ПКМ")), ("rotate", wb.parse_hotkey("ПКМ")))
    assert wb.hold_match(combos, lambda vk: vk == 0x02) == "rotate"
    assert wb.hold_match(combos, lambda vk: vk in (0x02, wb.VK_MENU)) == "lift"
    assert wb.hold_match(combos, lambda vk: vk == wb.VK_MENU) is None
    assert wb.hold_match((("x", None),), lambda vk: True) is None


def _edit(default="F9"):
    QApplication.instance() or QApplication([])
    e = HotkeyEdit(default)
    e.setText(default)
    e.show()
    got = []
    e.hotkeyChanged.connect(got.append)
    return e, got


def test_hotkey_edit_records_keys():
    e, got = _edit()
    e.setFocus()
    QTest.keyClick(e, Qt.Key_F7, Qt.ControlModifier)
    assert e.text() == "Ctrl+F7" and got == ["Ctrl+F7"]
    e.setFocus()
    QTest.keyClick(e, Qt.Key_Escape)
    assert e.text() == "Ctrl+F7" and got == ["Ctrl+F7"]       # Esc — без изменений
    e.setFocus()
    QTest.keyClick(e, Qt.Key_Backspace)
    assert e.text() == "F9" and got[-1] == "F9"               # Backspace — по умолчанию
    e.setFocus()
    QTest.keyClick(e, Qt.Key_P, Qt.ShiftModifier | Qt.AltModifier)
    assert e.text() == "Shift+Alt+P"
    assert qt_key_to_vk(Qt.Key_5, keypad=True) == 0x65 and qt_key_to_vk(Qt.Key_F24) == 0x87


def test_hotkey_edit_records_mouse_buttons():
    e, got = _edit("ПКМ")
    QTest.mouseClick(e, Qt.MiddleButton, Qt.AltModifier, QPoint(5, 5))
    assert e.text() == "Alt+СКМ" and got == ["Alt+СКМ"]
    QTest.mouseClick(e, Qt.RightButton, Qt.NoModifier, QPoint(5, 5))
    assert e.text() == "ПКМ"
    QTest.mouseClick(e, Qt.XButton1, Qt.NoModifier, QPoint(5, 5))
    assert e.text() == "Мышь4"
    QTest.mouseClick(e, Qt.LeftButton, Qt.NoModifier, QPoint(5, 5))   # левая — только начать ввод
    assert got[-1] == "Мышь4"


def _mod_tap(widget, key, mod):
    """Нажать и отпустить один модификатор — как приходит с клавиатуры.

    Qt сам переворачивает флаг модификатора у события его же клавиши: передаём состояние «до»
    (при нажатии — без Shift), а ``modifiers()`` вернёт «после» (Shift зажат).
    """
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    QApplication.sendEvent(widget, QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
    QApplication.sendEvent(widget, QKeyEvent(QEvent.KeyRelease, key, mod))


def test_hotkey_edit_records_single_modifier_and_tab_for_camera():
    e = HotkeyEdit("Ctrl", allow_modifier=True)
    got = []
    e.hotkeyChanged.connect(got.append)
    e.show()
    e.setFocus()
    _mod_tap(e, Qt.Key_Shift, Qt.ShiftModifier)
    assert e.text() == "Shift" and got == ["Shift"]
    e.setFocus()
    QTest.keyClick(e, Qt.Key_Tab)
    assert e.text() == "Tab" and got[-1] == "Tab"
    e.setFocus()                                   # Ctrl+F7 — модификатор не записывается сам
    QTest.keyClick(e, Qt.Key_F7, Qt.ControlModifier)
    _mod_tap(e, Qt.Key_Control, Qt.ControlModifier)
    assert e.text() == "Ctrl" and got[-2:] == ["Ctrl+F7", "Ctrl"]
    plain = HotkeyEdit("F8")                      # обычные поля: модификатор один не пишется
    plain_got = []
    plain.hotkeyChanged.connect(plain_got.append)
    plain.show()
    plain.setFocus()
    _mod_tap(plain, Qt.Key_Shift, Qt.ShiftModifier)
    assert plain_got == [] and plain.text() != "Shift"
    e.close()
    plain.close()


def test_hotkey_edit_records_left_button_only_for_look_field():
    look = HotkeyEdit("ЛКМ", allow_modifier=True, allow_left=True)
    got = []
    look.hotkeyChanged.connect(got.append)
    look.setText("Мышь4")
    look.show()
    QTest.mouseClick(look, Qt.LeftButton, Qt.NoModifier, QPoint(5, 5))   # первый — начать ввод
    assert got == []
    QTest.mouseClick(look, Qt.LeftButton, Qt.NoModifier, QPoint(5, 5))   # второй — «ЛКМ»
    assert look.text() == "ЛКМ" and got == ["ЛКМ"]
    assert wb.parse_hotkey("ЛКМ") == wb.Hotkey(0x01) and wb.parse_hotkey("lmb").text() == "ЛКМ"
    plain, plain_got = _edit("F9")
    QTest.mouseClick(plain, Qt.LeftButton, Qt.NoModifier, QPoint(5, 5))
    QTest.mouseClick(plain, Qt.LeftButton, Qt.NoModifier, QPoint(5, 5))
    assert plain_got == []                                    # обычные поля ЛКМ не записывают
    look.close()
    plain.close()
