"""Where a campaign's files live, for any command that needs to find them.

Not a runnable script -- imported only.

Two layouts. The folder layout the TUI creates:

    campaigns/<name>/ledger.jsonl   campaigns/<name>/raw/   campaigns/<name>/run.log

and the flat one campaigns run by hand grew first:

    <name>_ledger.jsonl   <name>_raw/   <name>_run.log

`tdvms_ledger.jsonl`, the CLI's default ledger, downloads to `afad_raw`.

Downloads kept on another disk are reached through `raw` as a symlink, or --
where the OS will not make one -- through a `raw.path` file naming the folder.
"""
import pathlib
import re
from dataclasses import dataclass

from tdvms.ledger import Ledger

# New campaigns get a folder each: `campaigns/<name>/ledger.jsonl` beside
# `campaigns/<name>/raw`, and `run.log` if the operator sends the loop there.
CAMPAIGNS = "campaigns"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# Where a campaign's downloads went when `raw` could not be a symlink.
POINTER = "raw.path"


@dataclass
class Campaign:
    name: str
    ledger: Ledger
    raw: pathlib.Path
    log: pathlib.Path

    @property
    def path(self):
        return self.ledger.path


def discover(root="."):
    """Every ledger under `root`: `campaigns/*/` first, then the flat layout.

    The flat layout is `<name>_ledger.jsonl` beside `<name>_raw`, which is what
    campaigns run by hand before the folder layout look like.
    `tdvms_ledger.jsonl` is the CLI's default and downloads to `afad_raw`.
    """
    root = pathlib.Path(root)
    found = []
    for p in sorted((root / CAMPAIGNS).glob("*/ledger.jsonl")):
        found.append(for_ledger(p))
    for p in sorted(root.glob("*_ledger.jsonl")):
        name = p.name[:-len("_ledger.jsonl")]
        raw = root / ("afad_raw" if name == "tdvms" else f"{name}_raw")
        found.append(Campaign(name, Ledger(p), raw, root / f"{name}_run.log"))
    return found


def new_campaign(root, name, raw_target=None):
    """Creates `campaigns/<name>/` and its `raw`, ready for `plan`.

    `raw_target` puts the downloads elsewhere -- another disk, usually -- and
    links `raw` to it, so the campaign folder still reads the same either way
    and `--out-dir campaigns/<name>/raw` is always the right flag.
    """
    if not NAME_RE.match(name or ""):
        raise ValueError("a campaign name is lowercase letters, digits, - and _")
    if any(c.name == name for c in discover(root)):
        raise ValueError(f"a campaign called {name!r} already exists")
    home = pathlib.Path(root) / CAMPAIGNS / name
    home.mkdir(parents=True, exist_ok=True)
    raw = home / "raw"
    if raw_target:
        target = pathlib.Path(raw_target).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        if not raw.exists():
            try:
                raw.symlink_to(target, target_is_directory=True)
            except OSError:
                # Windows lets only administrators and Developer Mode make
                # links. A one-line pointer does the same job for tdvms.
                (home / POINTER).write_text(str(target) + "\n", encoding="utf-8")
    else:
        raw.mkdir(exist_ok=True)
    return for_ledger(home / "ledger.jsonl")


def _raw(home):
    """A campaign folder's downloads: `raw/`, or wherever `raw.path` points."""
    pointer = home / POINTER
    if pointer.is_file():
        text = pointer.read_text(encoding="utf-8").strip()
        if text:
            return pathlib.Path(text)
    return home / "raw"


def for_ledger(path):
    """The campaign a ledger path belongs to, by layout; never None."""
    p = pathlib.Path(path)
    if p.name == "ledger.jsonl" and p.parent.parent.name == CAMPAIGNS:
        return Campaign(p.parent.name, Ledger(p), _raw(p.parent), p.parent / "run.log")
    if p.name.endswith("_ledger.jsonl"):
        name = p.name[:-len("_ledger.jsonl")]
        raw = p.parent / ("afad_raw" if name == "tdvms" else f"{name}_raw")
        return Campaign(name, Ledger(p), raw, p.parent / f"{name}_run.log")
    return Campaign(p.stem, Ledger(p), p.parent / "afad_raw", p.with_suffix(".log"))


def find(spec, root="."):
    """A campaign named on the command line: a ledger path, or a name.

    Raises:
        LookupError: Neither a ledger file nor the name of a known campaign.
    """
    p = pathlib.Path(spec)
    if p.suffix == ".jsonl" or p.is_file():
        return for_ledger(p)
    for c in discover(root):
        if c.name == spec:
            return c
    names = ", ".join(c.name for c in discover(root)) or "none"
    raise LookupError(f"no campaign called {spec!r} under {pathlib.Path(root).resolve()} "
                      f"(found: {names})")
