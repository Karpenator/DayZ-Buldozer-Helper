"""Тесты инструментов разведки канала Buldozer (tools/buldozer_pipe).

Инструменты лежат вне пакета ``terrain_editor`` (это отдельные скрипты для машины с
DayZ), поэтому подгружаем их из файлов. Тестируем только то, что не зависит от Windows:
формат имени канала, разбор строки «что отправить», разбор дампов и поиск строк.
"""

from __future__ import annotations

import importlib.util
import json
import re
import struct
import sys
import time
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "buldozer_pipe"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_pipe_{name}", TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe_pipe = _load("probe_pipe")
analyze_dump = _load("analyze_dump")
strings_scan = _load("strings_scan")
frida_capture = _load("frida_capture")
handle_names = _load("handle_names")
buldozer_channel = _load("buldozer_channel")


def test_pipe_name_matches_the_parameter_tb_passes():
    """Имя канала и параметр -connect= должны совпадать с тем, что пишет TB."""
    assert probe_pipe.build_pipe_name("1A041E") == r"\\.\pipe\1A041E,8EDB"
    assert probe_pipe.build_pipe_name("1A041E", with_magic=False) == r"\\.\pipe\1A041E"
    assert probe_pipe.connect_param("1A041E") == r"pipe\1A041E,8EDB"
    assert probe_pipe.connect_param("1A041E", magic="") == r"pipe\1A041E"


def test_send_spec_accepts_hex_and_text():
    assert probe_pipe.parse_send_spec("hex 00 11 aa") == b"\x00\x11\xaa"
    assert probe_pipe.parse_send_spec("0011aa") == b"\x00\x11\xaa"
    assert probe_pipe.parse_send_spec("text ok") == b"ok"
    with pytest.raises(probe_pipe.PipeProbeError):
        probe_pipe.parse_send_spec("hex zz")


def _make_framed_stream(payloads: list[bytes]) -> bytes:
    return b"".join(struct.pack("<I", len(p)) + p for p in payloads)


def test_framing_detection_finds_the_length_prefix():
    """Если сообщения начинаются с длины (u32 LE), анализатор обязан это увидеть."""
    stream = _make_framed_stream([b"AB", b"CDEF", b"G" * 12, b"HIJK"])
    best = analyze_dump.candidate_framings(stream)[0]
    assert best.header_bytes == 4
    assert best.endian == "little"
    assert best.messages == 4
    assert best.fraction == pytest.approx(1.0)


def test_report_shows_streams_and_payload_preview(tmp_path: Path):
    dump = tmp_path / "capture.log"
    data = _make_framed_stream([struct.pack("<fff", 1.5, 2.5, 3.5), b"tail"])
    records = [
        {"t": 0.1, "proc": "DayZ_x64.exe", "dir": "tx", "path": r"\\.\pipe\ABC,8EDB",
         "handle": 1, "data": data.hex(), "note": "WriteFile"},
        {"t": 0.2, "proc": "DayZ_x64.exe", "dir": "rx", "path": r"\\.\pipe\ABC,8EDB",
         "handle": 2, "data": b"pong".hex(), "note": "ReadFile"},
        {"t": 0.3, "proc": "TerrainBuilder.exe", "dir": "open",
         "path": r"\\.\pipe\ABC,8EDB", "handle": 3, "data": "", "note": "CreateNamedPipeW"},
    ]
    dump.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    parsed = analyze_dump.read_records([dump])
    assert len(parsed) == 3
    report = analyze_dump.format_report(analyze_dump.group_streams(parsed), show=1, hex_bytes=16)
    assert "DayZ_x64.exe" in report
    assert "[принято]" in report and "[отправлено]" in report
    assert "фрейминг?" in report
    assert "1.5" in report                       # f32-превью дошло до отчёта


def test_describe_payload_marks_ascii_and_floats():
    text = analyze_dump.describe_payload(b"buldozer" + struct.pack("<f", 12.5), limit=32)
    assert "buldozer" in text
    assert "12.5" in text


def test_record_from_message_keeps_direction_and_note():
    record = frida_capture.record_from_message(
        {"kind": "tx", "handle": 12, "path": r"\\.\pipe\A,8EDB", "api": "WriteFile",
         "ok": 1, "completed": 4},
        b"\x01\x02\x03\x04", elapsed=1.5, proc="TerrainBuilder.exe")
    assert record is not None
    assert record["dir"] == "tx"
    assert record["data"] == "01020304"
    assert "WriteFile" in record["note"] and "completed=4" in record["note"]
    assert frida_capture.record_from_message({"kind": "warn"}, None, 0.0, "x") is None


def test_strings_scan_finds_ascii_and_utf16():
    data = b"\x00\x00hello buldozer\x00\x00" + "канал pipe".encode("utf-16le")
    ascii_hits = strings_scan.scan_bytes(data, strings_scan.compile_filter("buld"))
    assert ascii_hits and "buldozer" in ascii_hits[0][1]
    utf16_hits = strings_scan.scan_bytes(data, strings_scan.compile_filter("pipe"),
                                         encoding=strings_scan.UNICODE)
    assert utf16_hits and "pipe" in utf16_hits[0][1]


def test_bad_filter_fails_with_a_clear_message():
    with pytest.raises(SystemExit):
        strings_scan.compile_filter("buld(")


def test_frida_hint_explains_which_python_is_used():
    """Частая ошибка: frida стоит в venv, а скрипт запущен системным `py -3`."""
    hint = frida_capture._missing_frida_hint()
    assert "frida-tools" in hint
    assert sys.executable in hint                      # видно, какой питон используется
    assert "py -3" in hint and ".venv" in hint


TASKLIST_SAMPLE = (
    '"System Idle Process","0","Services","0","8 КБ"\n'
    '"TerrainBuilder.exe","12900","Console","1","121 340 КБ"\n'
    '"DayZ_x64.exe","15544","Console","1","1 234 567 КБ"\n'
    '"steamwebhelper.exe","6632","Console","1","90 000 КБ"\n'
)


def test_tasklist_csv_is_parsed_even_with_localised_memory_column():
    """В CSV от tasklist последний столбец на русской Windows содержит пробелы ('121 340 КБ')."""
    procs = frida_capture.parse_tasklist_csv(TASKLIST_SAMPLE)
    assert procs == [(0, "System Idle Process"), (12900, "TerrainBuilder.exe"),
                     (15544, "DayZ_x64.exe"), (6632, "steamwebhelper.exe")]


def test_resolve_target_accepts_pid_name_and_substring():
    frida_procs = [(6632, "steamwebhelper.exe"), (15544, "DayZ_x64.exe")]
    win_procs = frida_capture.parse_tasklist_csv(TASKLIST_SAMPLE)

    pid, why = frida_capture.resolve_target("12900", frida_procs, win_procs)
    assert pid == 12900 and "вручную" in why

    pid, why = frida_capture.resolve_target("DayZ_x64.exe", frida_procs, win_procs)
    assert pid == 15544 and "точное имя" in why

    pid, why = frida_capture.resolve_target("dayz", frida_procs, win_procs)
    assert pid == 15544 and "содержит" in why


def test_resolve_target_uses_windows_pid_when_frida_cannot_see_the_process():
    """Terrain Builder запущен от администратора: Frida его не видит, Windows видит."""
    frida_procs = [(6632, "steamwebhelper.exe")]
    win_procs = frida_capture.parse_tasklist_csv(TASKLIST_SAMPLE)
    pid, why = frida_capture.resolve_target("TerrainBuilder.exe", frida_procs, win_procs)
    assert pid == 12900
    assert "Frida его не видит" in why and "администратора" in why


def test_resolve_target_explains_what_to_do_when_nothing_found():
    with pytest.raises(frida_capture.CaptureError) as exc:
        frida_capture.resolve_target("Buldozer.exe", [(1, "a.exe")], [])
    text = str(exc.value)
    assert "--list" in text and "--attach" in text and "администратора" in text


def test_filter_pipe_names_finds_the_buldozer_channel():
    """Канал TB узнаётся по хвосту ,8EDB — это самый простой способ найти его без Frida."""
    names = ["lsass", "1A041E,8EDB", "sql\\query", "MOJO_1234", "a3b17,8edb"]
    ours = probe_pipe.filter_pipe_names(names, magic="8EDB")
    assert ours == ["1A041E,8EDB", "a3b17,8edb"]          # регистр не важен

    # со своим filter ("нужен") возвращаются и совпадения по магии, и по подстроке
    assert probe_pipe.filter_pipe_names(names, needle="mojo", magic="") == ["MOJO_1234"]
    assert probe_pipe.filter_pipe_names(names, needle="mojo") == ["1A041E,8EDB",
                                                                  "MOJO_1234", "a3b17,8edb"]
    assert probe_pipe.filter_pipe_names(names, needle="", magic="") == []


def test_list_pipes_is_empty_off_windows():
    """На не-Windows списка каналов нет — важно, что скрипт не падает, а живёт."""
    assert probe_pipe.list_pipes() == [] or sys.platform == "win32"


def test_hidden_list_shows_our_processes_even_when_it_is_long():
    """Скрытых процессов бывает 150+: наши (DayZ, TB, Steam) обязаны быть в списке."""
    missing = [(pid, "svchost.exe") for pid in range(1000, 1100)]
    missing += [(18444, "DayZ_x64.exe"), (12900, "TerrainBuilder.exe"), (20992, "steam.exe")]
    shown, total, left = frida_capture.order_hidden(missing, limit=10)
    assert total == len(missing)
    names = [name for _, name in shown]
    assert shown[0][1] == "DayZ_x64.exe" or "DayZ_x64.exe" in names
    assert "TerrainBuilder.exe" in names and "steam.exe" in names
    assert len(shown) == 10
    assert left == len(missing) - 10


def test_hidden_list_marks_only_our_processes():
    missing = [(1, "lsass.exe"), (2, "DayZDiag_x64.exe")]
    shown, _, _ = frida_capture.order_hidden(missing, limit=5)
    assert shown[0][1] == "DayZDiag_x64.exe"          # наши — первыми
    assert shown[1][1] == "lsass.exe"


def test_named_later_pipe_gets_its_name_in_the_report(tmp_path: Path):
    """Имя канала выясняется позже (NtQueryObject): старые записи должны получить его."""
    dump = tmp_path / "late_name.log"
    records = [
        {"t": 0.1, "proc": "DayZ_x64.exe", "dir": "tx", "path": "", "handle": 777,
         "data": "01020304", "note": "WriteFile"},
        {"t": 0.2, "proc": "DayZ_x64.exe", "dir": "open", "path": r"\\.\pipe\1A041E,8EDB",
         "handle": 777, "data": "", "note": "NtQueryObject"},
        {"t": 0.3, "proc": "DayZ_x64.exe", "dir": "tx", "path": r"\\.\pipe\1A041E,8EDB",
         "handle": 777, "data": "05060708", "note": "WriteFile"},
    ]
    dump.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    streams = analyze_dump.group_streams(analyze_dump.read_records([dump]))
    report = analyze_dump.format_report(streams, show=1)
    assert "1A041E,8EDB" in report
    assert "handle" not in report.lower()          # отдельного «безымянного» потока нет
    # потоков два, потому что направления (трафик и открытие) считаются раздельно
    with_name = [s for s in streams if "1A041E" in s.path]
    assert len(with_name) == 2
    traffic = [s for s in with_name if s.direction == "tx"]
    assert len(traffic) == 1 and len(traffic[0].records) == 2   # обе старые записи нашли имя


def test_name_records_are_recognised_as_channel_opens():
    """Агент шлёт kind='name' с путём канала — это тоже открытие, а не трафик."""
    record = frida_capture.record_from_message(
        {"kind": "name", "handle": 512, "api": "NtQueryObject",
         "name": r"\\.\pipe\1A041E,8EDB"}, None, 0.5, "DayZ_x64.exe")
    assert record is not None
    assert record["dir"] == "open"
    assert record["path"] == r"\\.\pipe\1A041E,8EDB"
    assert "NtQueryObject" in record["note"]


def test_message_strings_are_found_with_bi_length_rule():
    """Длина строки у BI не включает завершающий нуль — на этом легко ошибиться."""
    messages_tool = _load("messages")
    text = b"P:\\evelone2666\\data\\layers\\P_000-000_L01_L13.rvmat"      # 50 символов
    data = len(text).to_bytes(4, "little") + text + b"\x00"
    found = messages_tool.find_strings(data)
    assert found and found[0][2] == text.decode("ascii")

    # и наоборот: длина вместе с нулём тоже должна приниматься
    data2 = (len(text) + 1).to_bytes(4, "little") + text + b"\x00"
    assert messages_tool.find_strings(data2)[0][2] == text.decode("ascii")

    # «строка» из одних цифр — это почти наверняка случайные числа, а не путь
    assert messages_tool.find_strings(b"\x05\x00\x00\x0012345\x00") == []
    # путь в формате BI находится
    assert messages_tool.find_strings(b"\x0f\x00\x00\x00dz\\weapons\\ak.p3d\x00")


def test_write_code_reads_the_message_code():
    messages_tool = _load("messages")
    assert messages_tool.write_code(b"\x06\x00\x00\x00" + b"\x00" * 68) == 6
    assert messages_tool.write_code(b"\x19\x00\x00\x00") == 25
    assert messages_tool.write_code(b"") is None


# ------------------------------------------------------------------ -connect= в --launch
# Раньше скрипт дописывал свой -connect= только если его не было вообще: строку TB
# (там уже -connect=pipe\F414A8,8EDB) он оставлял как есть — движок уходил в канал TB,
# наш pipe пуст, скрипт вис. Теперь чужой токен заменяется, а зависание закрыто таймаутом.

TB_LAUNCH = (r"E:\Steam\steamapps\common\DayZ\DayZ_x64.exe -buldozer -window -exThreads=0 "
             r"-mod= -noAsserts -nologs -profiles=P:\Buldozer -cfg=P:\Buldozer\Buldozer.cfg "
             r"-name=Buldozer -connect=pipe\F414A8,8EDB")


def test_apply_connect_replaces_the_tb_connect_parameter():
    """Командную строку TB можно вставлять целиком: чужой токен обязан замениться."""
    new_command, replaced = probe_pipe.apply_connect(TB_LAUNCH, r"pipe\1A2B3C,8EDB")
    assert replaced == 1
    assert "F414A8" not in new_command
    assert new_command.count("-connect=") == 1
    assert new_command.endswith(r"-connect=pipe\1A2B3C,8EDB")
    assert new_command.startswith(r"E:\Steam\steamapps\common\DayZ\DayZ_x64.exe -buldozer")


def test_apply_connect_appends_when_there_is_no_connect():
    new_command, replaced = probe_pipe.apply_connect("game.exe -buldozer", r"pipe\1A2B3C,8EDB")
    assert replaced == 0
    assert new_command == r"game.exe -buldozer -connect=pipe\1A2B3C,8EDB"
    # без пробела в конце — разделитель добавляется
    new_command, _ = probe_pipe.apply_connect("game.exe", r"pipe\1A2B3C,8EDB")
    assert new_command == r"game.exe -connect=pipe\1A2B3C,8EDB"


def test_apply_connect_does_not_treat_token_backslashes_as_regex_groups():
    """Замена строкой regex ломается: \0 и \1 из токена читаются как группы."""
    new_command, _ = probe_pipe.apply_connect("game.exe -connect=old", r"pipe\0AB12,8EDB")
    assert new_command.endswith(r"-connect=pipe\0AB12,8EDB")
    new_command, _ = probe_pipe.apply_connect("game.exe -connect=old", r"pipe\1ZZ,8EDB")
    assert new_command.endswith(r"-connect=pipe\1ZZ,8EDB")


def test_apply_connect_is_case_insensitive_and_replaces_only_the_first():
    new_command, replaced = probe_pipe.apply_connect(
        r"game.exe -CONNECT=pipe\AAAAAA,8EDB -connect=pipe\BBBBBB,8EDB", r"pipe\CCCCCC,8EDB")
    assert replaced == 1
    assert new_command.count("-connect=") == 2
    assert r"-connect=pipe\CCCCCC,8EDB" in new_command
    assert "BBBBBB" in new_command                       # второй остался — его ловит проверка


def test_find_foreign_connect_ignores_our_parameter():
    assert probe_pipe.find_foreign_connect(r"game.exe -CONNECT=pipe\1A2B3C,8EDB",
                                           r"pipe\1A2B3C,8EDB") == []
    assert probe_pipe.find_foreign_connect("game.exe -buldozer", r"pipe\1A2B3C,8EDB") == []
    assert probe_pipe.find_foreign_connect(r"game.exe -connect=pipe\F414A8,8EDB",
                                           r"pipe\1A2B3C,8EDB") == [r"-connect=pipe\F414A8,8EDB"]


def test_prepare_launch_explains_a_foreign_connect_left_in_the_line():
    """Остался чужой -connect= (был не один) — понятная ошибка, а не висение на пустом pipe."""
    command = r"game.exe -connect=pipe\AAAAAA,8EDB -connect=pipe\BBBBBB,8EDB"
    with pytest.raises(probe_pipe.PipeProbeError) as exc:
        probe_pipe.prepare_launch(command, r"pipe\CCCCCC,8EDB")
    text = str(exc.value)
    assert "BBBBBB" in text                              # чужой параметр назван
    assert r"pipe\CCCCCC,8EDB" in text                   # и наш тоже
    assert "пуст" in text                                # объяснено, чем это грозит


def test_prepare_launch_accepts_the_tb_line():
    new_command, replaced = probe_pipe.prepare_launch(TB_LAUNCH, r"pipe\1A2B3C,8EDB")
    assert replaced == 1
    assert probe_pipe.find_foreign_connect(new_command, r"pipe\1A2B3C,8EDB") == []


def test_wait_connection_times_out_instead_of_hanging(monkeypatch):
    """ConnectNamedPipe блокируется навсегда — скрипт обязан жить по таймауту."""
    class BlockingPipe:
        def ConnectNamedPipe(self, handle, overlapped):
            time.sleep(1.5)                              # «движок не подключается»
            return False

    monkeypatch.setattr(probe_pipe, "_k32", BlockingPipe())
    started = time.monotonic()
    assert probe_pipe._wait_connection(object(), timeout=0.2) is False
    assert time.monotonic() - started < 1.2


def test_wait_connection_reports_a_client(monkeypatch):
    class OkPipe:
        def ConnectNamedPipe(self, handle, overlapped):
            return True

    monkeypatch.setattr(probe_pipe, "_k32", OkPipe())
    assert probe_pipe._wait_connection(object(), timeout=5.0) is True


def test_wait_connection_accepts_a_fast_client(monkeypatch):
    """Клиент мог успеть раньше ConnectNamedPipe: ошибка 535 = уже подключён."""
    class BusyPipe:
        def ConnectNamedPipe(self, handle, overlapped):
            return False

    monkeypatch.setattr(probe_pipe, "_k32", BusyPipe())
    monkeypatch.setattr(probe_pipe, "_last_error",
                        lambda: probe_pipe.ERROR_PIPE_CONNECTED)
    assert probe_pipe._wait_connection(object(), timeout=5.0) is True


def test_wait_connection_zero_timeout_waits_forever(monkeypatch):
    class OkPipe:
        def ConnectNamedPipe(self, handle, overlapped):
            return True

    monkeypatch.setattr(probe_pipe, "_k32", OkPipe())
    assert probe_pipe._wait_connection(object(), timeout=0) is True


# ------------------------------------------------------------------ свежесть копии
def test_tool_version_marks_the_copy_date():
    """Пользователь сверяет строку «инструменты от …» с датой архива (см. README)."""
    for module in (frida_capture, probe_pipe, handle_names, buldozer_channel):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", module.TOOL_VERSION)
    assert frida_capture.TOOL_VERSION == probe_pipe.TOOL_VERSION   # копии не расходятся
    banner = frida_capture.tool_banner()
    assert "инструменты от" in banner and frida_capture.TOOL_VERSION in banner


def test_list_and_watch_print_the_tool_version(capsys):
    """Метка видна и в --list, и в --watch — как раз в тех режимах, что видит пользователь."""
    frida_capture.main(["--list"])            # без frida: метка уже напечатана, выход с ошибкой
    assert f"инструменты от {frida_capture.TOOL_VERSION}" in capsys.readouterr().out

    frida_capture.main(["--watch", "zzz-we-no-such-process",
                        "--watch-timeout", "0.01", "--seconds", "0.01"])
    assert f"инструменты от {frida_capture.TOOL_VERSION}" in capsys.readouterr().out


# ------------------------------------------------------------------ handle_names
def test_classify_name_tells_named_pipe_from_sockets():
    """Анонимный канал и сокет имени не имеют — это отдельная строка-подсказка."""
    assert handle_names.classify_name(r"\Device\NamedPipe\6914C2,8EDB") == "именованный канал"
    assert handle_names.classify_name(r"\device\namedpipe\someName") == "именованный канал"
    assert handle_names.classify_name(r"\Device\Afd\Endpoint") == "сокет"
    assert handle_names.classify_name("") == "без имени (анонимный канал или сокет)"
    assert handle_names.classify_name(r"\Device\HarddiskVolume3\logs") == "устройство"


def test_match_process_finds_all_dayz_tools():
    """DayZ_x64, DayZTerrainEditor и DayZToolsLauncher ловятся одним словом dayz."""
    assert handle_names.match_process("DayZ_x64.exe", ["dayz"])
    assert handle_names.match_process("DayZTerrainEditor.exe", ["dayz"])
    assert handle_names.match_process("DayZToolsLauncher.exe", ["dayz"])
    assert not handle_names.match_process("explorer.exe", ["dayz"])
    assert handle_names.match_process("anything.exe", [])     # без фильтра — все


def test_group_by_object_pairs_both_ends_of_a_channel():
    """Два хэндла с одним объектом ядра = два конца одного канала (пусть без имён)."""
    e1 = handle_names.HandleEntry(pid=1, process="DayZ_x64.exe", handle=3152,
                                  object=0xFFFF8880, file_type=3, scanned=True)
    e2 = handle_names.HandleEntry(pid=2, process="DayZTerrainEditor.exe", handle=268,
                                  object=0xFFFF8880, file_type=3, scanned=True)
    e3 = handle_names.HandleEntry(pid=1, process="DayZ_x64.exe", handle=1228,
                                  object=0xFFFF9990, file_type=3, scanned=True)
    groups = handle_names.group_by_object([e1, e2, e3])
    assert len(groups) == 2
    assert groups[0].object == 0xFFFF8880            # пара раньше одиночки
    assert len(groups[0].entries) == 2
    assert groups[1].entries == [e3]


def test_format_report_shows_both_sides_in_one_block():
    e1 = handle_names.HandleEntry(pid=1, process="DayZ_x64.exe", handle=3152,
                                  object=7, name=r"\Device\NamedPipe\6914C2,8EDB",
                                  file_type=3, scanned=True)
    e2 = handle_names.HandleEntry(pid=2, process="DayZTerrainEditor.exe", handle=268,
                                  object=7, name="", file_type=3, scanned=True)
    report = handle_names.format_report(handle_names.group_by_object([e1, e2]))
    assert "концы одного канала" in report
    assert r"\Device\NamedPipe\6914C2,8EDB" in report
    assert "DayZTerrainEditor.exe (PID 2)" in report
    assert "без имени" in report                      # второй конец без имени
    only_files = handle_names.format_report(
        handle_names.group_by_object([e1]), only_pipes=True, find="qqq")
    assert "ничего не найдено" in only_files


def test_connect_passes_integer_timeout_to_waitnamedpipe(monkeypatch, tmp_path):
    """Баг приёма правок: args.wait float -> WaitNamedPipeW должен получить int."""
    seen = {}

    class NoPipe:
        def WaitNamedPipeW(self, name, timeout_ms):
            seen.setdefault("timeout", timeout_ms)
            return 0

        def CloseHandle(self, handle):
            seen["closed"] = True

    monkeypatch.setattr(probe_pipe, "_k32", NoPipe())
    monkeypatch.setattr(probe_pipe, "_require_windows", lambda: None)
    args = probe_pipe.argparse.Namespace(token="6914C2", magic=probe_pipe.MAGIC_DEFAULT,
                                         wait=0.5, seconds=0.0,
                                         out=tmp_path / "probe.log")
    assert probe_pipe.cmd_connect(args) == 0
    assert isinstance(seen["timeout"], int), f"таймаут ушёл как {type(seen['timeout'])}"
    assert seen["timeout"] == 500


# ------------------------------------------------------------------ buldozer_channel
def test_encode_message_is_u32_code_plus_payload():
    assert buldozer_channel.encode_message(29, struct.pack("<I", 5)) == \
        bytes.fromhex("1d00000005000000")
    assert buldozer_channel.encode_message(7) == bytes.fromhex("07000000")
    msg, rest = buldozer_channel.next_message(buldozer_channel.encode_message(6, b"\xAA" * 68))
    assert rest == b"" and msg.code == 6 and msg.payload == b"\xAA" * 68


def test_decode_stream_handles_coalesced_and_partial_messages():
    """Канал байтовый: 152 Б в дампе = два слитых 25, чтение может рваться."""
    two = buldozer_channel.encode_message(25, b"\x01" * 72) * 2
    msgs, rest = buldozer_channel.decode_stream(two)
    assert [m.code for m in msgs] == [25, 25] and rest == b""
    half = two[:40]
    msgs, rest = buldozer_channel.decode_stream(half)
    assert msgs == [] and rest == half           # ждём остальное
    # код 28 переменный: [28][count] + count*16
    terrain = buldozer_channel.encode_message(28, struct.pack("<I", 3) + b"\x00" * 48)
    msgs, rest = buldozer_channel.decode_stream(terrain + buldozer_channel.encode_message(7))
    assert [m.code for m in msgs] == [28, 7]
    assert len(msgs[0].payload) == 4 + 48
    # неизвестный код — стоп, не выбрасываем данные
    msgs, rest = buldozer_channel.decode_stream(b"\x11\x22\x33\x44\x55")
    assert msgs == [] and rest == b"\x11\x22\x33\x44\x55"


def test_build_connect_args_replaces_or_adds_the_marker():
    args = ["-buldozer", "-window", "-connect=pipe\\OLD1,8EDB"]
    out = buldozer_channel.build_connect_args(args, "3011E8")
    assert out == ["-buldozer", "-window", "-connect=pipe\\3011E8,8EDB"]
    out = buldozer_channel.build_connect_args(["-buldozer"], "3011E8")
    assert out == ["-buldozer", "-connect=pipe\\3011E8,8EDB"]


def test_extract_world_packet_from_the_reference_dump():
    """Эталонный дамп в корне репо: мир = rx-сообщение 65 536 Б с кодом 2."""
    dump = Path(__file__).resolve().parents[2] / "capture_DayZ_x64.exe_20261012-210152.log"
    if not dump.is_file():
        pytest.skip("эталонного дампа нет в корне репо")
    packet = buldozer_channel.extract_world_packet(dump)
    assert len(packet) == 65536
    ints, floats = buldozer_channel.world_header(packet)
    assert ints == (2, 28, 128, 128, 1024, 1024)      # код и размеры мира
    assert floats == (40.0, 40.0)                      # размер клетки
    # строки слоёв на месте: 36 rvmat-плиток и «море»
    assert packet.count(b".rvmat") == 36
    assert b"ca\\desert\\data\\--sea--" in packet
    # круглый прогон: разобрать поток мира одним сообщением
    msgs, rest = buldozer_channel.decode_stream(packet)
    assert [m.code for m in msgs] == [2] and rest == b""


def test_handle_names_classifies_empty_namedpipe_name_as_anonymous():
    """\\Device\\NamedPipe\\ без имени после префикса — анонимный канал (CreatePipe)."""
    assert handle_names.classify_name("\\Device\\NamedPipe\\") == \
        "без имени (анонимный канал или сокет)"
    assert handle_names.classify_name("\\Device\\NamedPipe\\3011E8,8EDB") == \
        "именованный канал"


def test_format_report_shows_pipe_end_info():
    e1 = handle_names.HandleEntry(pid=1, process="DayZ_x64.exe", handle=3160,
                                  object=7, name="", file_type=3, scanned=True,
                                  info="конец: клиент; подключён; дуплекс")
    report = handle_names.format_report(handle_names.group_by_object([e1]))
    assert "конец: клиент" in report and "подключён" in report


def test_extract_world_accepts_a_folder_and_reports_missing_files(tmp_path):
    """--dump может быть папкой: ищем дамп с миром; ошибки — без traceback."""
    # пустая папка
    with pytest.raises(buldozer_channel.ChannelError, match="нет файлов"):
        buldozer_channel.extract_world_packet(tmp_path)
    # папка с логом без мира
    (tmp_path / "a.log").write_text('{"data": "07000000"}\n', encoding="utf-8")
    with pytest.raises(buldozer_channel.ChannelError, match="нет пакета мира"):
        buldozer_channel.extract_world_packet(tmp_path)
    # несуществующий файл — подсказка с соседними *.log
    with pytest.raises(buldozer_channel.ChannelError, match="не нашёл дамп"):
        buldozer_channel.extract_world_packet(tmp_path / "zzz.log")
    with pytest.raises(buldozer_channel.ChannelError, match="a.log"):
        buldozer_channel.extract_world_packet(tmp_path / "zzz.log")
    # настоящий дамп из корня репо кладём в папку — находится автоматически
    dump = Path(__file__).resolve().parents[2] / "capture_DayZ_x64.exe_20261012-210152.log"
    if not dump.is_file():
        pytest.skip("эталонного дампа нет в корне репо")
    (tmp_path / "b.log").write_bytes(dump.read_bytes())
    packet = buldozer_channel.extract_world_packet(tmp_path)
    assert len(packet) == 65536
