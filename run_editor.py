"""DayZ World Editor: стартовое окно + окно над Buldozer (см. README.md).

Запуск: python run_editor.py   (exe собирает build_exe.bat)
"""
import sys

if __name__ == "__main__":
    from terrain_editor.launcher import run
    sys.exit(run(sys.argv))
