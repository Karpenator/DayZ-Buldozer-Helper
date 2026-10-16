import json

from terrain_editor.settings import DEFAULTS, load_settings, save_settings


def test_paths_roundtrip_and_types(tmp_path):
    p = tmp_path / "s.json"
    s = load_settings(p)
    assert s == DEFAULTS and s["auto_install_script"] is True and s["overlay_icon_size"] == "medium"
    s.update(dayz_dir=r"E:\Steam\steamapps\common\DayZ", project_dir=r"P:\Evelone2666",
             scripts_dir=r"P:\scripts", overlay_icon_size="large")
    save_settings(s, p)
    again = load_settings(p)
    assert again["project_dir"] == r"P:\Evelone2666" and again["overlay_icon_size"] == "large"


def test_wrong_types_fall_back_and_ints_are_floats(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"overlay_mouse_yaw_per_px": 1, "overlay_autohide": "no",
                             "project_dir": 5, "unknown": 1}), encoding="utf-8")
    s = load_settings(p)
    assert s["overlay_mouse_yaw_per_px"] == 1.0 and isinstance(s["overlay_mouse_yaw_per_px"], float)
    assert s["overlay_autohide"] is True and s["project_dir"] == "" and "unknown" not in s


def test_old_editor_dayz_exe_becomes_dayz_dir(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"dayz_exe": r"E:\Steam\steamapps\common\DayZ\DayZ_x64.exe",
                             "mouse_sensitivity": 2.0}), encoding="utf-8")
    s = load_settings(p)
    assert s["dayz_dir"].endswith("DayZ") and "mouse_sensitivity" not in s
