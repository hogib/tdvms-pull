"""How much room the downloads have, and whether to keep going.

Not a runnable script -- imported only.

A full disk does not fail politely. A download that runs out of room dies
half-written, and the mail carrying its link has to be left unread or the link
is gone with it. So the loop checks before it downloads and before it submits,
and stops doing either while the free space is below a reserve:

* by default, twice the largest archive this campaign has fetched -- archives
  run from a few MB to ~900 MB, so a fixed figure is either too timid for one
  station or too loose for another -- and never less than `FLOOR_GB`;
* or a fixed `--min-free-gb`, where 0 turns the guard off.

Mail that arrives meanwhile stays unread and is downloaded once there is room;
nothing is lost unless the links expire first, and an expired link requeues its
window anyway.
"""
import pathlib
import shutil

FLOOR_GB = 2.0


def free_bytes(path):
    """Free space on the filesystem `path` is (or would be) on."""
    p = pathlib.Path(path).absolute()
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def reserve_bytes(rows, min_free_gb=None):
    """The free space to keep. `min_free_gb`, or twice the largest archive."""
    if min_free_gb is not None:
        return min_free_gb * 1e9
    largest = max((r.get("bytes") or 0 for r in rows if r["state"] == "fetched"),
                  default=0)
    return max(2 * largest, FLOOR_GB * 1e9)


def check(out_dir, rows, min_free_gb=None):
    """None while there is room; otherwise a sentence saying why not."""
    if min_free_gb == 0:
        return None
    need, free = reserve_bytes(rows, min_free_gb), free_bytes(out_dir)
    if free >= need:
        return None
    return (f"{free / 1e9:.1f} GB free for {out_dir}, below the {need / 1e9:.1f} GB "
            f"kept in reserve -- not downloading or submitting until there is room")
