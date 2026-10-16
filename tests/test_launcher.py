"""Стартовое окно: пути, автозаполнение, TemplateLibs, установка buldozer.c (offscreen)."""
import pytest
from PySide6.QtWidgets import QApplication

from terrain_editor import buldozer_overlay as bo
from terrain_editor import launcher as la
from terrain_editor.buldozer_bridge import PlacementSession, script_status
from terrain_editor.settings import DEFAULTS
from terrain_editor.templates import TemplateCatalog
from terrain_editor.thumbcache import ThumbCache
from terrain_editor.thumbloader import ThumbnailLoader

from test_buldozer_overlay import make_project


@pytest.fixture
def win(tmp_path, monkeypatch):
    qt = QApplication.instance() or QApplication([])
    drive = tmp_path / "P"
    project = make_project(drive)
    settings = dict(DEFAULTS, work_drive=str(drive), overlay_profile_dir=str(tmp_path / "Buldozer"))
    for mod in (bo, la):
        monkeypatch.setattr(mod, "load_settings", lambda: dict(settings))
        monkeypatch.setattr(mod, "save_settings", lambda s: settings.update(s))
    game = tmp_path / "Steam" / "steamapps" / "common" / "DayZ"
    game.mkdir(parents=True)
    (game / "DayZ_x64.exe").write_bytes(b"MZ")
    monkeypatch.setattr(la, "find_dayz_dir", lambda: game)
    monkeypatch.setattr(la.dialogs, "info", lambda *a, **k: None)
    catalog = TemplateCatalog()
    loader = ThumbnailLoader(ThumbCache(tmp_path / "thumbs", size=48), str(drive), start_thread=False)
    overlay = bo.BuldozerOverlay(catalog, session=PlacementSession(tmp_path / "s.json"), loader=loader)
    w = la.LauncherWindow(catalog, overlay)
    w.settings, w.tmp, w.project_path, w.game = settings, tmp_path, project, game
    yield w
    w._status_timer.stop()
    overlay.stop()
    overlay.window.close()
    w.close()
    qt.processEvents()


def test_start_autofills_dayz_and_scripts_and_installs_bridge(win, monkeypatch):
    monkeypatch.setattr(la, "default_scripts_dir", lambda drive: str(win.tmp / "P" / "scripts"))
    win.start()
    assert win.dayz.text() == str(win.game) and win.settings["dayz_dir"] == str(win.game)
    assert win.dayz.state.objectName() == "ok"
    assert win.settings["scripts_dir"] == str(win.tmp / "P" / "scripts")
    assert script_status(win.tmp / "P" / "scripts" / "buldozer.c")[0] == "ok"
    assert win.scripts.state.objectName() == "ok"
    assert "TemplateLibs" in win.project.state.text() or win.project.state.objectName() == "warn"
    assert win.overlay.is_running()


def test_project_folder_loads_templates_into_overlay(win):
    win.project.edit.setText(str(win.project_path))
    win._path_edited(win.project, "project_dir")
    assert win.settings["project_dir"] == str(win.project_path)
    assert len(win.catalog.templates) == 11
    assert win.project.state.objectName() == "ok" and "Rocks" in win.project.state.text()
    assert win.overlay.window.models.count() == 11


def test_bad_paths_are_reported(win, tmp_path):
    win.dayz.edit.setText(str(tmp_path))
    win._path_edited(win.dayz, "dayz_dir")
    assert win.dayz.state.objectName() == "bad"
    win.project.edit.setText(str(tmp_path / "nope"))
    win._path_edited(win.project, "project_dir")
    assert win.project.state.objectName() == "bad" and win.catalog.templates == []
    win.project.edit.setText(str(tmp_path / "Buldozer"))
    (tmp_path / "Buldozer").mkdir(exist_ok=True)
    win._path_edited(win.project, "project_dir")
    assert "не найдено" in win.project.state.text()


def test_hotkeys_and_flags_are_saved(win):
    win.key_toggle.setText("ctrl+f7")
    win._key_edited(win.key_toggle, "overlay_toggle_key")
    assert win.settings["overlay_toggle_key"] == "Ctrl+F7" and win.key_toggle.text() == "Ctrl+F7"
    assert win.overlay.key_texts()[0] == "Ctrl+F7"
    win.key_place.setText("абв")
    win._key_edited(win.key_place, "overlay_place_key")
    assert win.key_place.property("bad") is True and win.settings["overlay_place_key"] == "F9"
    win.chk_mouse.setChecked(False)
    assert win.settings["overlay_mouse_adjust"] is False and win.overlay._mouse_adjust is False


def test_live_state_and_overlay_button(win):
    win._update_live_state()
    assert "нет" in win.st_link.text()
    win.btn_overlay.click()
    win._update_live_state()
    assert win.overlay._wanted and win.btn_overlay.text().startswith("Скрыть")
    win._update_thumbs_state()
    assert "Превью в кэше: 0" in win.st_thumbs.text()


def test_all_five_hotkeys_editable_and_conflicts_rejected(win):
    assert [getattr(win, a).text() for a, *_ in win.hotkey_specs] == [
        "F8", "F9", "Shift+F9", "F7", "ПКМ", "Alt+ПКМ", "Shift+ПКМ", "Delete",
        "ЛКМ", "W", "S", "A", "D", "Space", "Ctrl", "Shift"]          # + своя камера
    win.key_rotate.commit(0x05, False, False, False)            # «щёлкнули» боковой кнопкой
    assert win.settings["overlay_rotate_key"] == "Мышь4" and win.overlay.adjust_texts()[0] == "Мышь4"
    win.key_lift.commit(0x05, False, False, False)              # то же — занято поворотом
    assert win.key_lift.property("bad") is True and win.settings["overlay_lift_key"] == "Alt+ПКМ"
    assert "уже занято" in win.keys_hint.text()
    win.key_lift.commit(0x05, False, False, True)                # Alt+Мышь4 — можно
    assert win.settings["overlay_lift_key"] == "Alt+Мышь4" and win.key_lift.property("bad") is False


def test_select_and_move_keys_saved_and_applied(win):
    win.key_select.commit(0x76, False, False, False)             # F7 -> F7 (по умолчанию)
    win.key_select.commit(0x75, False, False, False)             # F6
    assert win.settings["overlay_select_key"] == "F6" and win.overlay.select_texts()[0] == "F6"
    win.key_move.commit(0x02, True, False, False)                # Ctrl+ПКМ
    assert win.settings["overlay_move_key"] == "Ctrl+ПКМ" and win.overlay.select_texts()[1] == "Ctrl+ПКМ"
    win.key_move.commit(0x02, False, False, False)               # просто ПКМ — занято поворотом
    assert win.key_move.property("bad") is True and win.settings["overlay_move_key"] == "Ctrl+ПКМ"


def test_sensitivity_and_cursor_lock_saved(win):
    win.yaw_speed.setValue(1.25)
    win.lift_speed.setValue(2.5)                                 # см -> м в настройках
    win.chk_lock.setChecked(False)
    assert win.settings["overlay_mouse_yaw_per_px"] == 1.25
    assert abs(win.settings["overlay_mouse_lift_per_px"] - 0.025) < 1e-9
    assert win.settings["overlay_lock_cursor"] is False and win.overlay._lock_cursor is False
    assert win.overlay._yaw_per_px == 1.25


def test_camera_settings_saved_and_applied(win):
    assert win.chk_camera.isChecked() and win.cam_speed.value() == 30.0
    win.cam_speed.setValue(55.0)
    win.cam_sens.setValue(0.2)
    win.chk_cam_invert.setChecked(True)
    win.chk_cam_view.setChecked(True)
    assert win.settings["overlay_cam_speed"] == 55.0 and win.settings["overlay_cam_sens"] == 0.2
    cam = win.overlay.camera
    assert cam.speed == 55.0 and cam.sens == 0.2 and cam.invert_y and cam.along_view
    win.key_cam_up.commit(0x45, False, False, False)                 # E — вверх
    assert win.settings["overlay_cam_up_key"] == "E" and win.overlay._cam_vks["up"] == 0x45
    win.key_cam_down.commit(0x45, False, False, False)               # то же — занято
    assert win.key_cam_down.property("bad") is True and win.settings["overlay_cam_down_key"] == "Ctrl"
    win.key_cam_forward.commit(0x77, False, False, False)            # F8 — занято окном
    assert win.key_cam_forward.property("bad") is True
    win.chk_camera.setChecked(False)
    assert win.overlay._cam_enabled is False


def test_sections_collapse_and_state_is_remembered(win, tmp_path, monkeypatch):
    titles = [sec.title for sec in win.sections]
    assert titles == ["Папки", "Окно над Buldozer", "Своя камера в Buldozer (пока открыто окно F8)",
                      "Слои Terrain Builder", "Состояние"]
    win.show()
    assert all(sec.is_expanded() and sec.box.isVisible() for sec in win.sections)
    paths, opts, cam, layers, state = win.sections
    assert paths.header.text().startswith("▼") and win.dayz.edit.isVisible()
    paths.header.click()                                   # свернуть щелчком по заголовку
    assert not paths.is_expanded() and not paths.box.isVisible() and not win.dayz.edit.isVisible()
    assert paths.header.text() == "▶  Папки"
    cam.header.click()
    assert win.settings["launcher_collapsed"] == ["paths", "camera"]
    paths.header.click()                                   # развернуть обратно
    assert paths.box.isVisible() and win.dayz.edit.isVisible()
    assert win.settings["launcher_collapsed"] == ["camera"]
    # новое окно открывается с теми же свёрнутыми категориями
    again = la.LauncherWindow(win.catalog, win.overlay)
    try:
        assert [sec.is_expanded() for sec in again.sections] == [True, True, False, True, True]
        assert again.chk_camera.isChecked() == win.chk_camera.isChecked()   # настройки не сбились
    finally:
        again._status_timer.stop()
        again.close()
    win.close()
