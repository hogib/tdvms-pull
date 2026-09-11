"""File an archive already on disk against its window.

    tdvms ingest --file afad_raw/incoming.280371.zip.part

The recovery path. When a transfer succeeded but verification crashed, the bytes
are already here, and re-downloading 800 MB to re-run a regex is pure waste. The
file is matched by the window encoded in its own members, exactly as a fresh
download is, and it is never deleted on a failed parse -- that would destroy the
one thing this command exists to salvage.
"""
import argparse
import pathlib
import sys

from tdvms import fetch as fetching
from tdvms.ledger import Ledger, chunk_id, now

NAME = "ingest"
HELP = "file an already-downloaded archive against its ledger chunk"


def add_args(p):
    p.add_argument("--file", required=True, dest="path")
    p.add_argument("--out-dir", default="afad_raw")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing archive for this window")
    return p


def run(args):
    path = pathlib.Path(args.path)
    if not path.exists():
        sys.exit(f"[ERROR] {path} does not exist")
    ledger = Ledger(args.ledger)
    size = path.stat().st_size
    print(f"inspecting {path} ({size/1e6:.1f} MB)")

    members, size, problem = fetching.inspect(path)
    if problem is not None:
        # Kept, always. This is the operator's own file.
        sys.exit(f"[ERROR] {problem.detail} — {path} left where it is")
    station, start, _ = fetching.window_from_member(members[0])
    if start is None:
        sys.exit(f"[ERROR] '{members[0]}' does not carry a TDVMS window; "
                 f"{path} left where it is")
    want = start.strftime("%Y-%m-%d")
    row = next((r for r in ledger.rows()
                if r["station"] == station and r["start"][:10] == want), None)
    if row is None:
        sys.exit(f"[ERROR] {station} {want} is not in {ledger.path}; "
                 f"{path} left where it is")
    if not any(n.lower().endswith(".mseed") for n in members):
        import zipfile
        with zipfile.ZipFile(path) as zf:
            notice = zf.read(members[0])[:200].decode("utf-8", errors="replace").strip()
        cid = chunk_id(row["station"], row["start"])
        ledger.update(cid, state="nodata", fetched_at=now(), bytes=size, note=notice)
        print(f"[NODATA] {cid} — {notice}")
        return 0

    result = fetching.file_archive(path, row, args.out_dir, members, size, args.force)
    cid = chunk_id(row["station"], row["start"])
    if isinstance(result, fetching.Fetched):
        ledger.update(cid, state="fetched", fetched_at=now(), bytes=size,
                      note=f"{len(members)} file(s), ingested from disk")
        print(f"[OK] {cid} — {result.detail} -> {result.path}")
        return 0
    print(f"[WARN] {result.detail}")
    return 1


def main():
    p = argparse.ArgumentParser(prog="tdvms ingest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ledger", default="tdvms_ledger.jsonl")
    add_args(p)
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
