"""Окно поверх Buldozer: полный круг через файлы моста (скрипт Buldozer изображает тест)."""
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

import shutil

from terrain_editor import buldozer_overlay as bo
from terrain_editor.buldozer_bridge import CMD_FILE, STATE_FILE, PlacementSession
from terrain_editor.settings import DEFAULTS
from terrain_editor.templates import TemplateCatalog
from terrain_editor.thumbcache import ThumbCache
from terrain_editor.thumbloader import ThumbnailLoader

FIXTURES = Path(__file__).parent / "fixtures" / "model_origin" / "source"


def make_project(drive: Path) -> Path:
    """Рабочий диск с моделями и проект карты с TemplateLibs (как P:\\ и P:\\Evelone2666)."""
    models = drive / "evelone_test"
    models.mkdir(parents=True)
    rows = []
    for p3d in sorted(FIXTURES.glob("*.p3d")):
        shutil.copy(p3d, models / p3d.name)
        rows.append(f"<Template><Name>{p3d.stem}</Name><File>evelone_test\\{p3d.name}</File></Template>")
    libs = drive / "Evelone2666" / "source" / "TemplateLibs"
    libs.mkdir(parents=True)
    (libs / "evelone_test.tml").write_text(
        '<?xml version="1.0" ?>\n<Library name="Evelone_test">' + "".join(rows) + "</Library>",
        encoding="utf-8")
    (libs / "rocks.tml").write_text(
        '<Library name="Rocks"><Template><Name>Rock_1</Name><File>dz\\rocks\\rock_1.p3d</File>'
        '</Template></Library>', encoding="utf-8")
    return drive / "Evelone2666"


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    yield from make_overlay(tmp_path, monkeypatch)


def make_overlay(tmp_path, monkeypatch):
    """Окно над Buldozer с проектом и профилем во временной папке (генератор для фикстур)."""
    qt = QApplication.instance() or QApplication([])
    profile = tmp_path / "Buldozer"
    profile.mkdir()
    drive = tmp_path / "P"
    project = make_project(drive)
    settings = dict(DEFAULTS, overlay_profile_dir=str(profile), work_drive=str(drive),
                    project_dir=str(project))
    monkeypatch.setattr(bo, "load_settings", lambda: dict(settings))
    monkeypatch.setattr(bo, "save_settings", lambda s: settings.update(s))
    catalog = TemplateCatalog()
    catalog.load(project)
    loader = ThumbnailLoader(ThumbCache(tmp_path / "thumbs", size=48), str(drive), start_thread=False)
    ov = bo.BuldozerOverlay(catalog, session=PlacementSession(tmp_path / "session.json"), loader=loader)
    ov.profile = profile
    ov.settings = settings
    yield ov
    ov.stop()
    ov.window.close()
    ov.window.deleteLater()
    qt.processEvents()


def write_state(profile, text):
    (profile / STATE_FILE).write_text(text, encoding="utf-8")


def commands(profile):
    return (profile / CMD_FILE).read_text(encoding="utf-8").splitlines()


def test_place_needs_model_and_connection(overlay):
    assert not overlay.place_at_cursor()            # модель не выбрана
    overlay.set_wanted(True)
    overlay.window.models.setCurrentRow(0)
    assert not overlay.place_at_cursor()            # скрипт не отвечает
    assert "F10" in overlay.window.status.text()


def test_full_round_trip(overlay):
    profile = overlay.profile
    overlay.set_wanted(True)
    assert overlay.window.isVisible()
    assert overlay.window.models.count() == 11        # 10 моделей + Rock_1 из второй библиотеки
    overlay.window.models.setCurrentRow(0)
    model = overlay.current_model()
    assert model.startswith("evelone_test\\") and model.endswith(".p3d")

    write_state(profile, "WE_BRIDGE 1\nseq 1\ngen -1\nhit 100500 20000 200250\ncam 0 0 0\ndir 0 0 1000\nend\n")
    overlay._tick()
    assert "Курсор: X 100.5" in overlay.window.status.text()
    ghost = [l for l in commands(profile) if l.startswith("G|")][0]
    assert ghost.split("|")[1] == model             # «призрак» выбранной модели у курсора

    overlay.window.yaw.setValue(90)
    assert overlay.place_at_cursor()
    cmd = commands(profile)
    objects = [l for l in cmd if l.startswith("O|")]
    assert objects == [f"O|1|{model}|100.5|200.25|90|1|0"]
    assert cmd[-1] == "end"
    gen1 = int(cmd[1].split()[1])

    # скрипт создал объект и сообщил высоту
    write_state(profile, "WE_BRIDGE 1\nseq 2\ngen %d\nhit 100500 20000 200250\nY|1|21500|0\nend\n" % gen1)
    overlay._tick()
    assert overlay.session.objects[0].y == 21.5
    assert "#1" in overlay.window.placed.item(0).text()

    overlay.undo()
    assert [l for l in commands(profile) if l.startswith("O|")] == []
    assert int(commands(profile)[1].split()[1]) > gen1

    overlay.set_wanted(False)                         # окно скрыто -> призрак убран
    assert [l for l in commands(profile) if l.startswith("G|")] == ["G|"]


def test_failed_object_is_marked(overlay):
    profile = overlay.profile
    overlay.set_wanted(True)
    overlay.window.models.setCurrentRow(1)
    write_state(profile, "WE_BRIDGE 1\nseq 1\ngen 0\nhit 1000 0 1000\nend\n")
    overlay._tick()
    assert overlay.place_at_cursor()
    write_state(profile, "WE_BRIDGE 1\nseq 2\ngen 0\nhit 1000 0 1000\nE|1\nend\n")
    overlay._tick()
    assert overlay.session.objects[0].failed
    assert "не смог создать" in overlay.window.warn.text()


def test_filter(overlay):
    overlay.set_wanted(True)
    overlay.window.filter.setText("memory taller")
    overlay.refresh_models()
    names = [overlay.window.models.item(i).text() for i in range(overlay.window.models.count())]
    assert names == ["auto_memory_taller", "memory_taller"]


def _fake_keys(monkeypatch, down, foreground_is_buldozer):
    monkeypatch.setattr(bo.wb, "key_down", lambda vk: vk in down)
    monkeypatch.setattr(bo.wb, "foreground_is", lambda h: foreground_is_buldozer)
    monkeypatch.setattr(bo.wb, "foreground_is_ours", lambda: False)
    monkeypatch.setattr(bo.wb, "foreground_window", lambda: 77)
    monkeypatch.setattr(bo.wb, "window_title", lambda h: "buldozer.c - Блокнот")


def test_f9_in_buldozer_places_object(overlay, monkeypatch):
    profile = overlay.profile
    overlay.set_wanted(True)
    overlay.window.models.setCurrentRow(0)
    write_state(profile, "WE_BRIDGE 1\nseq 1\ngen -1\nhit 100500 20000 200250\ncam 0 0 0\ndir 0 0 1000\nend\n")
    overlay._tick()
    overlay._bz = 12345                               # «нашли» окно Buldozer
    down = set()
    _fake_keys(monkeypatch, down, True)
    overlay._poll_keys()
    down.add(0x78)                                    # F9
    overlay._poll_keys()
    assert len(overlay.session.objects) == 1
    assert any(l.startswith("O|1|") for l in commands(profile))
    overlay._poll_keys()                              # держим F9 — второй объект не ставится
    assert len(overlay.session.objects) == 1


def test_f9_with_other_window_active_explains_why(overlay, monkeypatch):
    overlay.set_wanted(True)
    overlay._bz = 12345
    down = set()
    _fake_keys(monkeypatch, down, False)
    overlay._poll_keys()
    down.add(0x78)
    overlay._poll_keys()
    assert not overlay.session.objects
    text = overlay.window.status.text()
    assert "F9" in text and "Блокнот" in text and "кликните в Buldozer" in text


def test_key_poll_error_is_shown_not_swallowed(overlay, monkeypatch):
    overlay.set_wanted(True)

    def boom(vk):
        raise RuntimeError("сломалось")
    monkeypatch.setattr(bo.wb, "key_down", boom)
    overlay._poll_keys()
    assert "сломалось" in overlay.window.status.text()


def test_right_mouse_drag_rotates_and_alt_lifts(overlay, monkeypatch):
    profile = overlay.profile
    overlay.set_wanted(True)
    overlay.window.models.setCurrentRow(0)
    overlay.window.random_yaw.setChecked(True)
    overlay._bz = 12345
    down = set()
    cursor = [500, 400]
    _fake_keys(monkeypatch, down, True)
    monkeypatch.setattr(bo.wb, "cursor_pos", lambda: tuple(cursor))
    down.add(bo.wb.VK_RBUTTON)
    overlay._poll_keys()                             # начало перетаскивания
    cursor[0] += 60                                  # вправо на 60 px -> +30°
    overlay._poll_keys()
    assert overlay.window.yaw.value() == 30.0 and not overlay.window.random_yaw.isChecked()
    assert overlay._next[0] == 30.0
    cursor[0] -= 100                                 # влево -> через 0 на 340°
    overlay._poll_keys()
    assert overlay.window.yaw.value() == 340.0
    down.add(bo.wb.VK_MENU)                          # Alt: мышь вверх на 50 px -> +0.5 м
    cursor[1] -= 50
    overlay._poll_keys()
    assert abs(overlay.window.lift.value() - 0.5) < 1e-9 and overlay.window.yaw.value() == 340.0
    overlay._tick()
    ghost = [l for l in commands(profile) if l.startswith("G|")][0].split("|")
    assert ghost[2] == "340" and ghost[4] == "0.5"
    down.clear()                                     # отпустили — движение больше не крутит
    overlay._poll_keys()
    cursor[0] += 200
    overlay._poll_keys()
    assert overlay.window.yaw.value() == 340.0


def test_right_mouse_drag_needs_buldozer_active(overlay, monkeypatch):
    overlay.set_wanted(True)
    overlay._bz = 12345
    down = {bo.wb.VK_RBUTTON}
    cursor = [500, 400]
    _fake_keys(monkeypatch, down, False)              # активна другая программа
    monkeypatch.setattr(bo.wb, "cursor_pos", lambda: tuple(cursor))
    overlay._poll_keys()
    cursor[0] += 100
    overlay._poll_keys()
    assert overlay.window.yaw.value() == 0.0


def test_library_combo_filters_and_is_remembered(overlay):
    combo = overlay.window.library
    assert [combo.itemText(i) for i in range(combo.count())][1:] == ["Evelone_test", "Rocks"]
    combo.setCurrentIndex(combo.findData("Rocks"))
    names = [overlay.window.models.item(i).text() for i in range(overlay.window.models.count())]
    assert names == ["Rock_1"] and overlay.settings["overlay_library"] == "Rocks"
    overlay.catalog.load(None)
    overlay.set_catalog(overlay.catalog)
    assert overlay.window.models.count() == 0 and "TemplateLibs" in overlay.window.models_count.text()


def test_icon_sizes_switch_view_and_persist(overlay):
    lw = overlay.window.models
    assert overlay.window.size_buttons.checkedButton().property("size_key") == "medium"
    for key, (_label, px, names) in bo.ICON_SIZES.items():
        overlay.set_icon_size(key)
        assert lw.iconSize().width() == px
        assert lw.viewMode() == bo.QListWidget.IconMode and lw.isWrapping()   # всегда сетка
        assert isinstance(lw.itemDelegate(), bo.IconOnlyDelegate) == (not names)
        assert overlay.settings["overlay_icon_size"] == key
    # заглушка не меньше крупных плиток, иначе Qt считает плитки по ней и подписи пропадают
    assert overlay._placeholder.availableSizes()[0].width() >= max(v[1] for v in bo.ICON_SIZES.values())
    overlay.window.size_buttons.buttons()[1].click()             # «Средние» кнопкой
    assert overlay.settings["overlay_icon_size"] == "medium"


def test_small_icons_are_a_grid_without_names(overlay):
    from PySide6.QtWidgets import QStyleOptionViewItem
    lw = overlay.window.models
    overlay.window.resize(420, 700)
    overlay.window.show()
    overlay.set_icon_size("small")
    QApplication.processEvents()
    assert lw.count() >= 2 and lw.iconSize() == bo.QSize(40, 40)          # размер — как раньше
    assert lw.gridSize() == bo.QSize(40 + bo.SMALL_CELL_PAD, 40 + bo.SMALL_CELL_PAD)
    first, second = lw.visualItemRect(lw.item(0)), lw.visualItemRect(lw.item(1))
    assert second.top() == first.top() and second.left() > first.left()   # в ряд, а не строками
    opt = QStyleOptionViewItem()
    lw.itemDelegate().initStyleOption(opt, lw.model().index(0, 0))
    assert opt.text == "" and not (opt.features & QStyleOptionViewItem.HasDisplay)   # без имени
    assert lw.item(0).text() and lw.item(0).text() in lw.item(0).toolTip()        # имя — в подсказке
    overlay.set_icon_size("medium")
    opt = QStyleOptionViewItem()
    lw.itemDelegate().initStyleOption(opt, lw.model().index(0, 0))
    assert opt.text == lw.item(0).text()                                   # средние — с именами
    overlay.window.hide()


def test_previews_render_in_background_and_come_from_cache(overlay, tmp_path):
    overlay.set_wanted(True)
    overlay.window.resize(360, 900)
    QApplication.instance().processEvents()
    overlay._load_visible_icons()
    loader = overlay.thumbs
    while loader.process_one():
        pass
    QApplication.instance().processEvents()
    assert loader.rendered >= 3
    item = overlay.window.models.item(0)
    assert not item.icon().pixmap(96, 96).isNull()
    count, size = loader.cache.stats()
    assert count >= 3 and size > 0
    assert overlay.clear_thumbnails() == count and loader.cache.stats() == (0, 0)


def test_export_uses_centered_dialogs_and_remembers_folder(overlay, tmp_path, monkeypatch):
    overlay.session.add("evelone_test\\basic.p3d", 10.0, 20.0)
    calls = {}

    def fake_save(ref, title, start, flt, suffix="txt"):
        calls["start"] = start
        return str(tmp_path / "out" / "objs.txt")

    monkeypatch.setattr(bo.dialogs, "save_file", fake_save)
    monkeypatch.setattr(bo.dialogs, "ask", lambda *a, **k: calls.setdefault("asked", True))
    (tmp_path / "out").mkdir()
    overlay.export_for_tb()
    assert calls["start"].endswith("buldozer_objects.txt")
    assert calls["start"].startswith(overlay.settings["project_dir"])    # первый раз — папка проекта
    assert (tmp_path / "out" / "objs.txt").read_text(encoding="utf-8").startswith('"basic";')
    assert overlay.settings["last_export_dir"] == str(tmp_path / "out")
    assert calls["asked"] and overlay.session.objects == []              # «Да» — сеанс очищен


def _ghost(profile):
    return [l for l in commands(profile) if l.startswith("G|")][0].split("|")


def _drag_env(overlay, monkeypatch, foreground=True):
    profile = overlay.profile
    overlay.set_wanted(True)
    overlay.window.models.setCurrentRow(0)
    write_state(profile, "WE_BRIDGE 1\nseq 1\ngen -1\nhit 100500 20000 200250\ncam 0 0 0\ndir 0 0 1000\nend\n")
    overlay._tick()
    overlay._bz = 12345
    down = set()
    cursor = [500, 400]
    warps = []

    def warp(x, y):                       # как SetCursorPos: курсор возвращается на место
        warps.append((x, y))
        cursor[:] = [x, y]
        return True

    _fake_keys(monkeypatch, down, foreground)
    monkeypatch.setattr(bo.wb, "cursor_pos", lambda: tuple(cursor))
    monkeypatch.setattr(bo.wb, "set_cursor_pos", warp)
    return profile, down, cursor, warps


def test_ghost_is_pinned_and_cursor_held_while_rotating(overlay, monkeypatch):
    profile, down, cursor, warps = _drag_env(overlay, monkeypatch)
    overlay.window.multi.setChecked(True)                 # «Множество объектов»: модель остаётся
    assert overlay.settings["overlay_multi_place"] is True
    assert len(_ghost(profile)) == 5                      # обычно — у курсора
    down.add(bo.wb.VK_RBUTTON)
    overlay._poll_keys()                                  # нажали ПКМ — модель закреплена сразу
    g = _ghost(profile)
    assert g[5:] == ["100.5", "20", "200.25"]
    assert "закреплена" in overlay.window.status.text()
    cursor[0] += 40                                       # мышь вправо: +20°
    overlay._poll_keys()
    assert overlay.window.yaw.value() == 20.0 and cursor == [500, 400] and warps[-1] == (500, 400)
    cursor[0] += 40
    overlay._poll_keys()
    assert overlay.window.yaw.value() == 40.0             # дельта от точки, а не накопленная
    # курсор Buldozer «уехал» (новое состояние) — F9 ставит туда, где модель закреплена
    write_state(profile, "WE_BRIDGE 1\nseq 2\ngen -1\nhit 900000 1000 900000\ncam 0 0 0\ndir 0 0 1000\nend\n")
    overlay._tick()
    assert overlay.place_at_cursor()
    obj = overlay.session.objects[-1]
    assert (obj.x, obj.z, obj.yaw) == (100.5, 200.25, 40.0)
    down.clear()
    overlay._poll_keys()                                  # отпустили — снова у курсора
    assert len(_ghost(profile)) == 5 and overlay._pin is None


def test_lift_combo_wins_over_rotate_and_switches_on_the_fly(overlay, monkeypatch):
    profile, down, cursor, _warps = _drag_env(overlay, monkeypatch)
    down |= {bo.wb.VK_RBUTTON, bo.wb.VK_MENU}
    overlay._poll_keys()
    cursor[1] -= 30                                       # вверх 30 px -> +0.3 м
    overlay._poll_keys()
    assert abs(overlay.window.lift.value() - 0.3) < 1e-9 and overlay.window.yaw.value() == 0.0
    down.discard(bo.wb.VK_MENU)                           # Alt отпустили — уже поворот
    cursor[0] += 20
    overlay._poll_keys()
    assert overlay.window.yaw.value() == 10.0 and len(_ghost(profile)) == 8


def test_custom_adjust_keys_and_no_cursor_lock(overlay, monkeypatch):
    overlay.settings.update(overlay_rotate_key="Мышь4", overlay_lift_key="Ctrl+СКМ",
                            overlay_lock_cursor=False)
    overlay.reload_settings()
    assert overlay.adjust_texts() == ("Мышь4", "Ctrl+СКМ")
    assert "Мышь4" in overlay.window.hint.text()
    profile, down, cursor, warps = _drag_env(overlay, monkeypatch)
    down.add(bo.wb.VK_RBUTTON)                            # ПКМ больше ничего не делает
    overlay._poll_keys()
    cursor[0] += 40
    overlay._poll_keys()
    assert overlay.window.yaw.value() == 0.0 and len(_ghost(profile)) == 5
    down.clear()
    overlay._poll_keys()
    down.add(0x05)                                        # Мышь4 — поворот
    overlay._poll_keys()
    cursor[0] += 40
    overlay._poll_keys()
    assert overlay.window.yaw.value() == 20.0 and warps == []   # курсор не держим
    cursor[0] += 40
    overlay._poll_keys()
    assert overlay.window.yaw.value() == 40.0
    down.clear()
    overlay._poll_keys()
    down |= {0x04, bo.wb.VK_CONTROL}                      # Ctrl+СКМ — подъём
    overlay._poll_keys()
    cursor[1] -= 10
    overlay._poll_keys()
    assert abs(overlay.window.lift.value() - 0.1) < 1e-9


def test_cursor_lock_falls_back_when_windows_refuses(overlay, monkeypatch):
    _profile, down, cursor, _warps = _drag_env(overlay, monkeypatch)
    monkeypatch.setattr(bo.wb, "set_cursor_pos", lambda x, y: False)
    down.add(bo.wb.VK_RBUTTON)
    overlay._poll_keys()
    cursor[0] += 40
    overlay._poll_keys()
    cursor[0] += 40
    overlay._poll_keys()
    assert overlay.window.yaw.value() == 40.0             # не 60: дельты не задваиваются


# ------------------------------------------------------------ объекты карты: выделение и правки (v10)

def _map_model(overlay):
    t = next(t for t in overlay.catalog.templates if t.file.startswith("evelone_test\\"))
    return t.name, t.file


def _pick_line(seq, model, ref=3, own=-1, x=60.0, z=880.0, yaw=90.0, sess=42):
    mm = lambda v: str(int(round(v * 1000)))                          # noqa: E731
    return (f"P|{seq}|{sess}|{ref}|{own}|{model}|{mm(x)}|5000|{mm(z)}|{mm(yaw)}|0|0|1000|"
            f"{mm(60.0)}|{mm(880.0)}|{mm(90.0)}|0")


def _state(seq, *extra, sess=42):
    return "\n".join(["WE_BRIDGE 1", f"seq {seq}", "gen -1", f"bs {sess}", "hit 60000 5000 880000",
                      "cam 0 0 0", "dir 0 0 1000", *extra, "end", ""])


def _select_map_object(overlay, monkeypatch):
    profile = overlay.profile
    overlay.set_wanted(True)
    write_state(profile, _state(1))
    overlay._tick()
    assert overlay.select_at_cursor()
    seq = overlay._pick_seq
    assert f"K|{seq}" in commands(profile)
    _name, model = _map_model(overlay)
    write_state(profile, _state(2, _pick_line(seq, model)))
    overlay._tick()
    overlay._bz = 12345
    down, cursor = set(), [500, 400]
    _fake_keys(monkeypatch, down, True)
    monkeypatch.setattr(bo.wb, "cursor_pos", lambda: tuple(cursor))
    monkeypatch.setattr(bo.wb, "set_cursor_pos", lambda x, y: cursor.__setitem__(slice(None), [x, y]) or True)
    return profile, model, down, cursor


def test_select_rotate_move_and_revert_map_object(overlay, monkeypatch):
    profile, model, down, cursor = _select_map_object(overlay, monkeypatch)
    sel = overlay._sel
    assert sel is not None and sel.is_map and (sel.session, sel.ref) == (42, 3)
    assert "объект карты" in overlay.window.sel_label.text()
    assert "S|42|3" in commands(profile)
    assert not any(l.startswith("G|") and len(l) > 2 for l in commands(profile))   # призрака нет
    assert len(overlay.session.samples) == 1
    # ПКМ + мышь вправо — поворот объекта карты, а не модели для расстановки
    down.add(bo.wb.VK_RBUTTON)
    overlay._poll_keys()
    cursor[0] += 40
    overlay._poll_keys()
    edit = sel.edit
    assert edit.yaw == 110.0 and overlay.window.yaw.value() == 0.0
    overlay._write_commands()
    assert "T|42|3|60|880|110|0" in commands(profile)
    down.clear()
    overlay._poll_keys()
    # Shift+ПКМ — перенос: скрипт везёт объект (D), отпустили — берём, куда довёз (DP)
    down |= {bo.wb.VK_SHIFT, bo.wb.VK_RBUTTON}
    overlay._poll_keys()
    assert "D|42|3" in commands(profile)
    write_state(profile, _state(3, _pick_line(overlay._pick_seq, model), "DP|42|3|65500|877250"))
    overlay._tick()
    overlay._bz = 12345
    down.clear()
    overlay._poll_keys()
    assert (edit.x, edit.z) == (65.5, 877.25) and edit.changed
    assert "D|42|3" not in commands(profile) and "T|42|3|65.5|877.25|110|0" in commands(profile)
    assert overlay.window.save_layers.isEnabled() and "(1)" in overlay.window.save_layers.text()
    # правка пережила перезапуск окна (сеанс на диске)
    again = bo.PlacementSession(overlay.session.path)
    again.load()
    assert again.map_edits[0].x == 65.5
    overlay.revert_selection()
    assert not edit.changed and not any(l.startswith("T|") for l in commands(profile))
    # Esc в Buldozer — снять выделение
    down.add(0x1B)
    overlay._poll_keys()
    assert overlay._sel is None and "S|42|3" not in commands(profile)


def test_selecting_own_object_moves_it(overlay, monkeypatch):
    profile = overlay.profile
    overlay.set_wanted(True)
    overlay.window.models.setCurrentRow(0)
    write_state(profile, _state(1))
    overlay._tick()
    assert overlay.place_at_cursor()
    obj = overlay.session.objects[0]
    overlay.window.models.setCurrentRow(-1)
    overlay.window.models.clearSelection()
    overlay.select_at_cursor()
    write_state(profile, _state(2, _pick_line(overlay._pick_seq, obj.model, ref=1, own=obj.id,
                                              x=obj.x, z=obj.z)))
    overlay._tick()
    assert overlay._sel is not None and overlay._sel.own == obj.id and not overlay._sel.is_map
    assert not overlay.session.map_edits                     # своё — не правка карты
    overlay._adjust("rotate", 30, 0)
    assert obj.yaw == 15.0
    overlay._bz = 12345
    down, cursor = {bo.wb.VK_SHIFT, bo.wb.VK_RBUTTON}, [500, 400]
    _fake_keys(monkeypatch, down, True)
    monkeypatch.setattr(bo.wb, "cursor_pos", lambda: tuple(cursor))
    overlay._poll_keys()
    write_state(profile, _state(3, "DP|42|1|70000|890000"))
    overlay._tick()
    overlay._bz = 12345
    down.clear()
    overlay._poll_keys()
    assert (obj.x, obj.z) == (70.0, 890.0)
    assert any(l.startswith(f"O|{obj.id}|") and "|70|890|" in l for l in commands(profile))
    overlay.undo()                                           # «Отменить» — вернуть как было
    assert (overlay.session.objects[0].x, overlay.session.objects[0].z) == (60.0, 880.0)


def test_picking_a_model_ends_selection_and_nothing_under_cursor(overlay, monkeypatch):
    profile, model, _down, _cursor = _select_map_object(overlay, monkeypatch)
    overlay.window.models.setCurrentRow(0)                   # выбрали модель — снова расстановка
    assert overlay._sel is None
    overlay.select_at_cursor()
    write_state(profile, _state(4, f"P|{overlay._pick_seq}|42|-1"))
    overlay._tick()
    assert overlay._sel is None and "нет объекта" in overlay.window.status.text()


def test_pick_without_v10_script_times_out(overlay, monkeypatch):
    profile = overlay.profile
    overlay.set_wanted(True)
    write_state(profile, _state(1))
    overlay._tick()
    now = [1000.0]
    monkeypatch.setattr(bo.time, "monotonic", lambda: now[0])
    overlay.select_at_cursor()
    write_state(profile, _state(2))
    overlay._tick()
    assert overlay._pick_pending is not None
    now[0] += bo.PICK_TIMEOUT + 0.5
    write_state(profile, _state(3))
    overlay._tick()
    assert overlay._pick_pending is None and "v10" in overlay.window.status.text()


def test_save_to_layers_writes_copies_and_remembers_offset(overlay, monkeypatch, tmp_path):
    from test_tv4l import make_layer
    from terrain_editor.tv4l import Tv4lLayer, sdbm
    profile, model, down, cursor = _select_map_object(overlay, monkeypatch)
    name, _file = _map_model(overlay)
    layers = tmp_path / "Proj.Layers"
    layers.mkdir()
    original = make_layer([(sdbm(name), [(100.0, 900.0, 90.0, 7)])])  # Buldozer 60/880 + сдвиг 40/20
    (layers / "Walls.tv4l").write_bytes(original)
    overlay.settings["overlay_layers_dir"] = str(layers)
    edit = overlay._sel.edit
    edit.x, edit.z, edit.yaw = 62.0, 881.0, 100.0
    shown = []
    monkeypatch.setattr(bo.dialogs, "info", lambda ref, title, text: shown.append(text))
    monkeypatch.setattr(bo.dialogs, "error", lambda ref, title, text: shown.append("ERR " + text))
    overlay.save_to_layers()
    assert shown and "Записаны копии слоёв (1" in shown[0], shown
    out = tmp_path / "WE_edited_layers" / "current" / "Walls.tv4l"
    o = Tv4lLayer.load(out).find(7)
    assert (o.x, o.z) == (102.0, 901.0) and abs(o.yaw - 100.0) < 1e-4
    assert (layers / "Walls.tv4l").read_bytes() == original             # оригинал не тронут
    assert overlay.settings["overlay_layer_offset"] == [40.0, 20.0]
    assert edit.saved


def test_export_uses_project_map_frame(overlay, tmp_path, monkeypatch):
    from test_tbproject import tiny_tv4p
    tb = Path(overlay.settings["project_dir"]) / "TerrainBuilder"
    tb.mkdir(parents=True, exist_ok=True)
    (tb / "Demo.tv4p").write_bytes(tiny_tv4p(frame=(500000.0, 442127.5, 505120.0, 447247.5)))
    overlay.session.add("evelone_test\\basic.p3d", 10.0, 20.0)
    said = {}
    monkeypatch.setattr(bo.dialogs, "save_file", lambda *a, **k: str(tmp_path / "objs.txt"))
    monkeypatch.setattr(bo.dialogs, "ask", lambda ref, title, text, *a, **k: said.setdefault("t", text) and False)
    overlay.export_for_tb()
    line = (tmp_path / "objs.txt").read_text(encoding="utf-8").splitlines()[0].split(";")
    assert float(line[1]) == 500010.0 and float(line[2]) == 442147.5
    assert "Demo.tv4p" in said["t"] and "442127.50" in said["t"]


def test_single_place_clears_model_from_cursor(overlay, monkeypatch):
    profile, down, cursor, warps = _drag_env(overlay, monkeypatch)
    assert not overlay.window.multi.isChecked()           # по умолчанию — одна модель
    assert len(_ghost(profile)) == 5
    assert overlay.place_at_cursor()
    overlay._write_commands()
    assert _ghost(profile) == ["G", ""] and overlay.current_model() == ""   # призрак исчез
    assert not overlay.place_at_cursor()                  # F9 без модели — подсказка
    assert len(overlay.session.objects) == 1


def _wait_layers(overlay):
    """Прогнать фоновую запись слоёв до конца (пауза в изменениях — сразу)."""
    overlay._layers_tick()                                # увидели изменение
    overlay._layers_due = 0.0
    overlay._layers_tick()                                # пауза прошла — запись в фоне
    if overlay._layers_job is not None:
        overlay._layers_job[0].result(timeout=60)
        overlay._layers_tick()                            # забрали результат


def _layers_env(overlay, tmp_path):
    from test_layer_sync import _real_copy
    layers = _real_copy(tmp_path, ("Floors", "Roofs"))
    overlay.settings["overlay_layers_dir"] = str(layers)
    overlay.refresh_layer_list()
    return layers


@pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "Evelone2666").is_dir(),
                    reason="нет проекта Evelone2666")
def test_placed_objects_go_to_chosen_layer_work_copy(overlay, tmp_path, monkeypatch):
    from terrain_editor.tv4l import Tv4lLayer
    layers = _layers_env(overlay, tmp_path)
    w = overlay.window
    assert [w.layer.itemText(i) for i in range(w.layer.count())] == ["Floors", "Roofs"]
    assert w.layers_auto.isChecked()
    w.layer.setCurrentIndex(1)                            # как активный слой в TB
    assert overlay.settings["overlay_target_layer"] == "Roofs" and overlay.target_layer() == "Roofs"
    name, model = _map_model(overlay)
    overlay.session.add(model, 100.0, 200.0, 5.0, 90.0, 1.0, 0.0)
    _wait_layers(overlay)
    work = tmp_path / "TerrainBuilder" / "WE_edited_layers" / "current"
    layer = Tv4lLayer.load(work / "Roofs.tv4l")
    orig = Tv4lLayer.load(layers / "Roofs.tv4l")
    new = layer.find(orig.id_counter)                     # mobjectIDcounter у Roofs
    assert layer.template_names[new.template] == name and len(layer.objects) == len(orig.objects) + 1
    assert "новых в «Roofs»: 1" in w.layers_status.text()
    assert (layers / "Roofs.tv4l").read_bytes() == orig._data                  # оригинал не тронут
    w.layer.setCurrentIndex(0)                            # сменили слой — объект переехал
    _wait_layers(overlay)
    assert sorted(p.name for p in work.glob("*.tv4l")) == ["Floors.tv4l"]
    overlay.session.clear()                               # отменили всё — рабочая копия пуста
    _wait_layers(overlay)
    assert not list(work.glob("*.tv4l"))
    # «Применить в проект»: пока TB запущен — отказ; потом — замена с резервной копией
    overlay.session.add(model, 100.0, 200.0, 5.0, 90.0, 1.0, 0.0)
    said = []
    monkeypatch.setattr(bo.dialogs, "error", lambda ref, title, text: said.append("ERR " + text))
    monkeypatch.setattr(bo.dialogs, "info", lambda ref, title, text: said.append(text))
    monkeypatch.setattr(bo.dialogs, "ask", lambda *a, **k: True)
    monkeypatch.setattr(bo.layer_sync, "tb_running", lambda: True)
    assert not overlay.apply_layers_to_project()
    assert "Terrain Builder запущен" in said[-1]
    monkeypatch.setattr(bo.layer_sync, "tb_running", lambda: False)
    before = (layers / "Floors.tv4l").read_bytes()
    assert overlay.apply_layers_to_project()
    assert len(Tv4lLayer.load(layers / "Floors.tv4l").objects) == 209
    backup = next((tmp_path / "TerrainBuilder" / "WE_layers_backup").glob("*/Floors.tv4l"))
    assert backup.read_bytes() == before
    assert not overlay.session.objects and "Готово" in said[-1]   # в сеансе — не дублировать
    assert not list(work.glob("*.tv4l"))


def test_layers_auto_off_writes_only_on_button(overlay, tmp_path):
    overlay.window.layers_auto.setChecked(False)
    overlay.settings["overlay_layers_dir"] = str(tmp_path)        # «папка слоёв» есть
    overlay._update_layers_status()
    assert "по кнопке" in overlay.window.layers_status.text()
    assert overlay.settings["overlay_layers_auto"] is False
    overlay.session.add("evelone_test\\basic.p3d", 1.0, 2.0)
    _wait_layers(overlay)
    assert overlay._layers_job is None and overlay._layers_report is None


def test_apply_with_unparsed_layers_explains_why(overlay, tmp_path, monkeypatch):
    from test_tv4l import _broken_sample
    layers = tmp_path / "TerrainBuilder" / "P.Layers"
    layers.mkdir(parents=True)
    bad, _at = _broken_sample()
    (layers / "test.tv4l").write_bytes(bad)
    overlay.settings["overlay_layers_dir"] = str(layers)
    overlay.refresh_layer_list()
    _wait_layers(overlay)
    overlay.session.add("evelone_test\\basic.p3d", 1.0, 2.0)
    _wait_layers(overlay)
    said = []
    monkeypatch.setattr(bo.dialogs, "error", lambda ref, title, text: said.append("ERR " + text))
    monkeypatch.setattr(bo.dialogs, "info", lambda ref, title, text: said.append(text))
    monkeypatch.setattr(bo.dialogs, "ask", lambda *a, **k: True)
    monkeypatch.setattr(bo.layer_sync, "tb_running", lambda: False)
    assert not overlay.apply_layers_to_project()
    assert said[-1].startswith("ERR Не записано: слои не прочитаны: test.tv4l: не разобран")
    assert "Экспорт для Terrain Builder" in said[-1] and "debug_" in said[-1]
    assert (layers / "test.tv4l").read_bytes() == bad            # слой не тронут


def test_placed_object_goes_into_open_tb_and_our_copy_is_dropped_after_confirmation(overlay, tmp_path):
    from terrain_editor.buldozer_bridge import BridgeState
    from test_tblive import FakeApi
    layers = tmp_path / "TB" / "Map.Layers"
    layers.mkdir(parents=True)
    overlay.settings["overlay_layers_dir"] = str(layers)
    api = FakeApi()
    overlay.tblive.api = api
    assert overlay.window.tb_live.isChecked() and overlay.window.tb_save.isChecked()
    name, model = _map_model(overlay)
    o = overlay.session.add(model, 10.0, 20.0, 5.0, 90.0)
    o.tb_rel = 0.0
    overlay._tb_live_tick(BridgeState())
    assert o.tb == "sent" and len(api.drops) == 1
    sent = open(api.drops[0][1][0], "rb").read().decode()
    assert sent.startswith(f'"{name}";') and api.drops[0][1][0].startswith(str(tmp_path / "TB"))
    cmd = overlay._command_text()
    assert f"V|{o.id}|10.000|20.000|" in cmd and f"O|{o.id}|" in cmd
    assert "… в TB" in overlay.window.placed.item(0).text()
    overlay._tb_live_tick(BridgeState(verified={o.id: 1}))
    assert o.tb == "ok"
    cmd = overlay._command_text()
    assert f"O|{o.id}|" not in cmd and f"V|{o.id}|" not in cmd       # показывает сам TB
    assert "✓ в TB" in overlay.window.placed.item(0).text()
    assert "Открытый TB: вставлено 1" in overlay.window.layers_status.text()
    overlay.window.tb_live.setChecked(False)
    assert overlay.settings["overlay_tb_live"] is False and not overlay.tblive.enabled


def test_hotkeys_come_from_bridge_script_when_windows_hides_them(overlay, monkeypatch):
    """Buldozer от администратора: GetAsyncKeyState — нули, F8 сообщает скрипт (KD|…)."""
    from terrain_editor.buldozer_bridge import BridgeState
    monkeypatch.setattr(bo.wb, "key_down", lambda vk: False)
    monkeypatch.setattr(bo.wb, "foreground_is", lambda hwnd: True)
    overlay._bz = 123
    overlay._connected = True
    assert "KW|KC_LSHIFT,KC_RSHIFT,KC_LCONTROL,KC_RCONTROL,KC_LMENU,KC_RMENU,KC_F8,KC_F9,KC_ESCAPE" \
        in overlay._command_text()
    overlay._state = BridgeState(keys_down=set())
    overlay._handle_keys()
    assert not overlay._wanted
    overlay._state = BridgeState(keys_down={"KC_F8"})
    overlay._handle_keys()
    assert overlay._wanted                                   # F8 — окно нужно
    overlay._handle_keys()
    assert overlay._wanted                                   # держат — второй раз не срабатывает
    overlay._state = BridgeState(keys_down={"KC_F8", "KC_LSHIFT"})
    overlay._handle_keys()
    overlay._state = BridgeState(keys_down=set())
    overlay._handle_keys()
    overlay._connected = False                               # связь пропала — старым KD| не верим
    overlay._state = BridgeState(keys_down={"KC_F8"})
    overlay._handle_keys()
    assert overlay._wanted
