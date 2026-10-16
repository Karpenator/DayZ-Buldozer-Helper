import logging

from terrain_editor import debuglog
from terrain_editor.settings import DEFAULTS, load_settings, save_settings


def test_settings_default_and_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    assert load_settings(path) == DEFAULTS
    save_settings({"debug": True}, path)
    expected = dict(DEFAULTS)
    expected["debug"] = True
    assert load_settings(path) == expected


def test_settings_bad_file_gives_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"debug": "yes"}', encoding="utf-8")   # wrong type is ignored
    assert load_settings(path) == DEFAULTS
    path.write_text("[1, 2", encoding="utf-8")
    assert load_settings(path) == DEFAULTS


def test_enable_writes_log_and_disable_stops_it(tmp_path):
    assert not debuglog.is_enabled()
    path = debuglog.enable(tmp_path / "logs")
    try:
        assert debuglog.is_enabled() and debuglog.current_log_path() == path
        assert path.parent == tmp_path / "logs" and path.name.startswith("debug_")
        debuglog.get_logger("test").info("hello %s", "world")
        assert debuglog.enable(tmp_path / "logs") == path      # idempotent
    finally:
        debuglog.disable()
    text = path.read_text(encoding="utf-8")
    assert "debug mode ON" in text
    assert "terrain_editor.test" in text and "hello world" in text
    assert "debug mode OFF" in text
    assert not debuglog.is_enabled()


def test_nothing_is_written_when_disabled(tmp_path):
    before = logging.getLogger(debuglog.ROOT).level
    debuglog.get_logger("test").info("should not appear anywhere")
    assert not (tmp_path / "logs").exists()
    assert logging.getLogger(debuglog.ROOT).level == before
