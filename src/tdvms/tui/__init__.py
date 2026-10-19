"""`tdvms-tui` -- watch and repair campaigns, and plan new ones off a map.

Textual is an optional extra, so a missing or broken install can never stop a
campaign: `run` and the other commands do not import anything under here.
"""
import sys


def main(argv=None):
    try:
        from tdvms.tui.app import main as app_main
    except ImportError as e:
        if (e.name or "").split(".")[0] not in ("textual", "rich"):
            raise
        print("tdvms-tui needs the `tui` extra:\n"
              "    uv sync --extra tui        # in a checkout\n"
              "    pip install 'tdvms[tui]'   # elsewhere", file=sys.stderr)
        return 1
    return app_main(argv)
