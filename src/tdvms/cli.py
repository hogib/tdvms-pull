"""`tdvms` -- a self-supervising AFAD/TDVMS download campaign.

    tdvms                      # this listing
    tdvms run --help           # each command has its own flags

Arguments after the command are passed through untouched, and each command is
exactly the underlying module's `main()`, so a recorded command and the real one
cannot diverge. Every command also runs standalone as `python -m tdvms.run`.
"""
import importlib
import sys

COMMANDS = {
    "plan":   ("tdvms.plan",   "enumerate a station's chunks into the ledger"),
    "adopt":  ("tdvms.adopt",  "import an afad_campaign ledger (source untouched)"),
    "run":    ("tdvms.run",    "the supervised loop: reap, reclaim, retire, submit"),
    "status": ("tdvms.report", "what the campaign has done, and what is stuck"),
    "reset":  ("tdvms.reset",  "requeue one chunk, by station and start"),
    "ingest": ("tdvms.ingest", "file an already-downloaded archive"),
}


def usage():
    print("tdvms -- an AFAD/TDVMS download campaign that supervises itself\n")
    print("usage: tdvms <command> [args...]     (each command has its own --help)\n")
    for name, (_, summary) in COMMANDS.items():
        print(f"  {name:<7} {summary}")
    print("\n`run` is the campaign. It owns the ledger, the mailbox and the portal")
    print("in one process, so submit -> fetch -> free-the-slot happen inside one")
    print("function under one lock. The other commands are for looking at it and")
    print("for repairing by hand what the loop cannot decide on its own.")
    print("\nThe mailbox needs three variables:")
    print("  TDVMS_IMAP_HOST  TDVMS_IMAP_USER  TDVMS_IMAP_PASS")
    print("(AFAD_IMAP_* are accepted too, so a live environment keeps working.)")
    return 0


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        return usage()
    name = argv[0]
    if name not in COMMANDS:
        near = [c for c in COMMANDS if c.startswith(name[:2])]
        print(f"tdvms: unknown command {name!r}" +
              (f" -- did you mean {' or '.join(near)}?" if near else ""),
              file=sys.stderr)
        print("run `tdvms` for the list", file=sys.stderr)
        return 2
    sys.argv = [f"tdvms {name}"] + argv[1:]
    return importlib.import_module(COMMANDS[name][0]).main() or 0


if __name__ == "__main__":
    sys.exit(main())
