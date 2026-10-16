import sys

if __name__ == "__main__":                 # python -m terrain_editor
    from .launcher import run
    sys.exit(run(sys.argv))
