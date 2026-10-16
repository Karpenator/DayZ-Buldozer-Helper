"""TemplateLibs (.tml), поиск DayZ/рабочего диска, кэш превью, фоновая загрузка превью."""
import shutil
from pathlib import Path

import pytest

from terrain_editor import dayzpaths
from terrain_editor.buldozer_bridge import (MARK_BEGIN, SCRIPT_REVISION, PlacedObject,
                                            install_script, profile_candidates, script_path_in,
                                            script_status)
from terrain_editor.templates import (TemplateCatalog, find_template_libs, model_key, parse_tml,
                                      parse_tml_text)
from terrain_editor.thumbcache import ThumbCache

FIXTURES = Path(__file__).parent / "fixtures" / "model_origin" / "source"

TML = """<?xml version="1.0" ?>
<Library name="Evelone_modular" shape="rectangle" default_fill="-65536" default_outline="-16777216" tex="0">
    <Template>
        <Name>EV_10</Name>
        <File>evelone_modular\\1\\ev_10.p3d</File>
        <Date>01/14/26 16:01:52</Date>
        <Archive></Archive>
        <Fill>-65536</Fill>
        <Outline>-16777216</Outline>
        <Scale>1.000000</Scale>
        <Hash>3758194946</Hash>
        <ScaleRandMin>0.000000</ScaleRandMin>
        <BoundingMin X="-4.1" Y="-0.5" Z="-2.0" />
        <BoundingMax X="4.1" Y="3.5" Z="2.0" />
        <Height>0.000000</Height>
    </Template>
    <Template>
        <Name>EV_2</Name>
        <File>evelone_modular/1/EV_2.p3d</File>
    </Template>
    <Template>
        <Name>EV_empty</Name>
        <File></File>
    </Template>
</Library>
"""


# ------------------------------------------------------------------ templates
def test_parse_tml_reads_names_files_and_bbox():
    lib = parse_tml_text(TML, "fallback")
    assert lib.name == "Evelone_modular" and not lib.error
    assert [(t.name, t.file) for t in lib.templates] == [
        ("EV_10", "evelone_modular\\1\\ev_10.p3d"), ("EV_2", "evelone_modular/1/EV_2.p3d")]
    assert lib.templates[0].bbox_min == (-4.1, -0.5, -2.0)
    assert lib.templates[1].key == "evelone_modular\\1\\ev_2.p3d"


def test_broken_tml_is_read_with_regex():
    lib = parse_tml_text(TML.replace("</Library>", "<Template><Name>x"), "fb")
    assert lib.error and lib.name == "Evelone_modular"
    assert [t.name for t in lib.templates] == ["EV_10", "EV_2"]


def test_tml_in_cp1251_and_without_library_name(tmp_path):
    p = tmp_path / "Дома.tml"
    p.write_bytes("<Library><Template><Name>Дом</Name><File>a\\дом.p3d</File></Template></Library>"
                  .encode("cp1251"))
    lib = parse_tml(p)
    assert lib.name == "Дома" and lib.templates[0].name == "Дом" and lib.path == p


def test_find_template_libs_anywhere_in_project(tmp_path):
    project = tmp_path / "Evelone2666"
    (project / "source" / "TemplateLibs").mkdir(parents=True)
    (project / "source" / "TemplateLibs" / "b.tml").write_text(TML, encoding="utf-8")
    (project / "source" / "TemplateLibs" / "a.TML").write_text(TML, encoding="utf-8")
    (project / "source" / "TemplateLibs" / "readme.txt").write_text("x", encoding="utf-8")
    (project / "data" / "TemplateLibs").mkdir(parents=True)          # data/ пропускаем... кроме самой TemplateLibs
    (project / "data" / "TemplateLibs" / "c.tml").write_text(TML, encoding="utf-8")
    (project / "layers" / "deep").mkdir(parents=True)
    names = [p.name for p in find_template_libs(project)]
    assert names[:2] == ["a.TML", "b.tml"] and "readme.txt" not in names
    assert [p.name for p in find_template_libs(project / "source" / "TemplateLibs")] == ["a.TML", "b.tml"]
    assert find_template_libs(tmp_path / "nope") == []


def test_catalog_filter_libraries_and_tb_names(tmp_path):
    libs = tmp_path / "proj" / "TemplateLibs"
    libs.mkdir(parents=True)
    (libs / "modular.tml").write_text(TML, encoding="utf-8")
    (libs / "rocks.tml").write_text('<Library name="Rocks"><Template><Name>Rock_1</Name>'
                                    '<File>dz\\rocks\\rock_1.p3d</File></Template></Library>',
                                    encoding="utf-8")
    cat = TemplateCatalog()
    rev = cat.revision
    assert cat.load(tmp_path / "proj") == 3 and cat.revision > rev
    assert cat.library_names() == ["Evelone_modular", "Rocks"]
    assert [t.name for t in cat.templates] == ["EV_2", "EV_10", "Rock_1"]          # естественный порядок
    assert [t.name for t in cat.filtered("ev_10")] == ["EV_10"]
    assert [t.name for t in cat.filtered("rock")] == ["Rock_1"]
    assert [t.name for t in cat.filtered("", "Rocks")] == ["Rock_1"]
    assert cat.by_model("/Evelone_Modular/1/ev_10.p3d").name == "EV_10"
    assert cat.tb_names()[model_key("dz/rocks/rock_1.p3d")] == "Rock_1"
    assert cat.load(None) == 0 and cat.templates == []


def test_tb_export_uses_template_names():
    obj = PlacedObject(1, "evelone_modular\\1\\ev_10.p3d", 100.0, 200.0)
    names = {model_key("evelone_modular/1/ev_10.p3d"): "EV_10_custom"}
    assert obj.tb_object(names=names).model == "EV_10_custom"
    assert obj.tb_object().model == "ev_10"                       # не нашли — имя файла
    assert obj.tb_object(template_names=False, names=names).model == "evelone_modular\\1\\ev_10.p3d"


# ------------------------------------------------------------------ dayz / work drive
def test_steam_libraries_and_find_dayz(tmp_path):
    vdf = '"libraryfolders"\n{\n "0" { "path" "C:\\\\Program Files (x86)\\\\Steam" }\n' \
          f' "1" {{ "path" "{str(tmp_path / "lib").replace(chr(92), chr(92) * 2)}" }}\n}}'
    libs = dayzpaths.steam_libraries([vdf], roots=[])
    assert libs[0] == Path("C:\\Program Files (x86)\\Steam") and libs[1] == tmp_path / "lib"
    game = tmp_path / "lib" / "steamapps" / "common" / "DayZ"
    game.mkdir(parents=True)
    assert dayzpaths.find_dayz_dir(libs) is None
    (game / "DayZ_x64.exe").write_bytes(b"MZ")
    assert dayzpaths.find_dayz_dir(libs) == game
    assert dayzpaths.dayz_exe_in(game) == game / "DayZ_x64.exe"
    assert dayzpaths.dayz_exe_in(game / "DayZ_x64.exe") == game / "DayZ_x64.exe"
    assert dayzpaths.dayz_exe_in("") is None


def test_work_drive_rules():
    assert dayzpaths.work_drive_of(None, "", r"P:\scripts", r"D:\x") == "P:\\"
    assert dayzpaths.work_drive_of("/tmp/x") == "P:\\"
    assert dayzpaths.resolve_work_drive("", r"Q:\Evelone2666") == "Q:\\"
    assert dayzpaths.resolve_work_drive("W:", r"P:\scripts") == "W:\\"
    assert dayzpaths.resolve_work_drive("/tmp/P") == "/tmp/P/"
    assert dayzpaths.default_scripts_dir("P:\\") == r"P:\scripts"
    assert dayzpaths.default_profile_dir("Q:") == r"Q:\Buldozer"


def test_model_file_and_texture_resolver(tmp_path):
    (tmp_path / "evelone_modular" / "data").mkdir(parents=True)
    (tmp_path / "evelone_modular" / "data" / "wall_co.paa").write_bytes(b"x")
    p = dayzpaths.model_file(str(tmp_path), "\\evelone_modular/1\\ev_1.p3d")
    assert p == tmp_path / "evelone_modular" / "1" / "ev_1.p3d"
    resolve = dayzpaths.texture_resolver(str(tmp_path))
    assert resolve("evelone_modular\\data\\wall_co.paa") == tmp_path / "evelone_modular" / "data" / "wall_co.paa"
    assert resolve("evelone_modular\\data\\missing.paa") is None


def test_profile_candidates_follow_work_drive():
    c = profile_candidates({"overlay_profile_dir": "", "scripts_dir": r"Q:\scripts"}, home=Path("/home/u"))
    assert str(c[0]) == r"Q:\Buldozer"
    c = profile_candidates({"overlay_profile_dir": r"D:\prof", "work_drive": "P:"}, home=Path("/home/u"))
    assert [str(p) for p in c[:2]] == [r"D:\prof", r"P:\Buldozer"]


# ------------------------------------------------------------------ buldozer.c state
def test_script_status_lifecycle(tmp_path):
    path = script_path_in(tmp_path, "Buldozer")
    assert path.name == "buldozer.c"
    assert script_status(path) == ("missing", 0)
    path.write_text("static void BuldozerMain()\n{\n}\n", encoding="utf-8")
    assert script_status(path) == ("no_block", 0)
    install_script(path, "Buldozer")
    assert script_status(path) == ("ok", SCRIPT_REVISION)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(f"{MARK_BEGIN} v{SCRIPT_REVISION}", f"{MARK_BEGIN} v1"), encoding="utf-8")
    assert script_status(path) == ("old", 1)
    path.write_text(text.replace(f"{MARK_BEGIN} v{SCRIPT_REVISION}", f"{MARK_BEGIN} v99"), encoding="utf-8")
    assert script_status(path) == ("newer", 99)


# ------------------------------------------------------------------ thumbnail cache
@pytest.fixture
def model(tmp_path):
    dst = tmp_path / "drive" / "evelone_test" / "basic.p3d"
    dst.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "basic.p3d", dst)
    return dst


def test_thumb_cache_renders_once_and_survives_restart(tmp_path, model):
    cache = ThumbCache(tmp_path / "thumbs", size=48)
    assert cache.lookup(model)[0] == "todo"
    png = cache.render(model)
    assert png is not None and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    again = ThumbCache(tmp_path / "thumbs", size=48)                   # «перезапуск программы»
    assert again.lookup(model) == ("ok", png, png.stem)
    assert again.stats()[0] == 1 and again.stats()[1] > 0
    assert ThumbCache(tmp_path / "thumbs", size=96).lookup(model)[0] == "todo"   # другой размер — другой ключ
    model.write_bytes(model.read_bytes() + b"\0")                      # модель изменилась
    assert again.lookup(model)[0] == "todo"
    assert again.clear() == 1 and again.stats() == (0, 0)


def test_thumb_cache_remembers_failures(tmp_path):
    bad = tmp_path / "odol.p3d"
    bad.write_bytes(b"ODOL" + b"\0" * 64)
    cache = ThumbCache(tmp_path / "thumbs", size=48)
    assert cache.render(bad) is None
    assert cache.lookup(bad)[0] == "fail"
    assert cache.lookup(tmp_path / "missing.p3d") == ("nofile", None, None)


def test_thumbnail_loader_queue(tmp_path, model):
    from PySide6.QtWidgets import QApplication
    from terrain_editor.thumbloader import ThumbnailLoader
    QApplication.instance() or QApplication([])
    loader = ThumbnailLoader(ThumbCache(tmp_path / "thumbs", size=48), str(tmp_path / "drive"),
                             start_thread=False)
    ready = []
    loader.ready.connect(ready.append)
    model2 = model.with_name("missing.p3d")
    assert loader.pixmap("evelone_test\\basic.p3d", model) is None          # в очередь
    assert loader.pixmap("evelone_test\\missing.p3d", model2) is None       # файла нет — сразу «нет»
    assert loader.known(model2) and not loader.known(model)
    assert loader.process_one() and not loader.process_one()
    assert ready == ["evelone_test\\basic.p3d"] and loader.rendered == 1
    pm = loader.pixmap("evelone_test\\basic.p3d", model)
    assert pm is not None and not pm.isNull() and pm.width() == 48
    fresh = ThumbnailLoader(ThumbCache(tmp_path / "thumbs", size=48), start_thread=False)
    assert fresh.pixmap("evelone_test\\basic.p3d", model) is not None       # с диска, без рендера
    assert fresh.rendered == 0 and not fresh.process_one()
    loader.forget()
    assert not loader.known(model2)
