"""Общие настройки тестов: одинаково в Windows (build_exe.bat) и Linux.

* Qt без настоящих окон (platform offscreen): тесты не зависят от фокуса, рамок окон,
  масштаба экрана и не мигают окнами при сборке exe.
* Настоящую систему тесты не трогают: в Windows ``winbuldozer`` иначе по-настоящему искал бы
  окно Buldozer (и «терял» подставленное тестом), читал клавиатуру и двигал курсор мыши
  (``SetCursorPos`` сбивал тесты поворота мышью). Поэтому в тестах всё ведёт себя как не в
  Windows, а нужные функции (клавиши, курсор, активное окно) тесты подменяют сами.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # пакет terrain_editor

import pytest  # noqa: E402

from terrain_editor import winbuldozer  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_windows_api(monkeypatch):
    monkeypatch.setattr(winbuldozer, "IS_WINDOWS", False)
    monkeypatch.setattr(winbuldozer, "set_cursor_pos", lambda x, y: False)
