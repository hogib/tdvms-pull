"""Download one link and decide what actually arrived.

Not a runnable script -- imported only.

Every check here was bought with lost data, and each is a distinct outcome
rather than an exit code, because the supervisor has to tell them apart:

* **HTML with HTTP 200.** An expired link returns AFAD's homepage, not a 404.
  Checking the content type before streaming avoids writing a few KB of markup
  into a `.zip` -- and, on a link that is merely wrong, avoids streaming 800 MB
  of something that is not an archive.
* **A 22-byte zip.** A bare end-of-central-directory: the portal built an
  archive and put nothing in it. This is TRANSIENT. GCAM 2024-09-04..09-25 came
  back this way while both neighbouring windows hold ~700 MB, so the station was
  plainly recording; retiring the window as permanent threw away real data. The
  ELBA requeue later recovered one of these at 0.82 GB.
* **A well-formed zip holding one 82-byte notice.** "No data" does not arrive
  as an empty archive. It arrives as a valid zip carrying
  "Secilen istasyon icerisinde gecerli zaman araliginda veri bulunamamistir.",
  which passes every emptiness check and was banked as a successful fetch for
  three windows before anyone looked at the byte counts.
* **An archive that matches no chunk.** Never guessed at. The old fallback was
  "the oldest submitted chunk", which files one window's data under another
  window's name; the corrected download then overwrote 825 MB of good data.
"""
import os
import pathlib
import re
import zipfile
from datetime import datetime

import requests

# The portal names members TU_<STA>_<DDMMYYYY>_<HHMMSS>_<DDMMYYYY>_<HHMMSS>_<CH>.
# The channel suffix is NOT required: a no-data notice is named for the same
# window but ends after the second timestamp, and demanding the trailing "_"
# made every notice match no chunk at all -- so the one answer that needs no
# download was the one answer that could not be recorded.
MEMBER_RE = re.compile(r"TU_([A-Z0-9]+)_(\d{8})_(\d{6})_(\d{8})_(\d{6})")


class Result:
    """What a link turned out to be. `frees_slot` drives the refill."""
    frees_slot = True
    ok = False

    def __init__(self, detail=""):
        self.detail = detail

    def __repr__(self):
        return f"{type(self).__name__}({self.detail!r})"


class Fetched(Result):
    """Waveform data, verified and filed under its station."""
    ok = True

    def __init__(self, path, size, members, station, start):
        super().__init__(f"{size/1e6:.1f} MB, {members} file(s)")
        self.path, self.size, self.members = path, size, members
        self.station, self.start = station, start


class NoDataNotice(Result):
    """A valid archive whose only member is the portal's no-data notice."""

    def __init__(self, notice, station, start, size):
        super().__init__(notice)
        self.station, self.start, self.size = station, start, size


class EmptyArchive(Result):
    """A 22-byte zip. Transient -- requeue the window, do not retire it."""

    def __init__(self, size, station=None, start=None):
        super().__init__(f"{size} B, no members")
        self.station, self.start = station, start


class Expired(Result):
    """The link returned HTML, or was not a zip at all."""


class Unmatched(Result):
    """A real archive naming a window this ledger does not hold.

    Kept on disk under the name of what it actually contains. It frees no slot,
    because no slot of ours was holding it.
    """
    frees_slot = False

    def __init__(self, path, member):
        super().__init__(f"{member} matches no chunk; kept as {path.name}")
        self.path = path


class AlreadyBanked(Result):
    """This URL is already recorded as fetched -- an old mail resurfacing.

    Frees nothing. Treating a duplicate as a fresh fetch made the old poller
    refill a slot that was never occupied, and the portal answered BUSY.
    """
    frees_slot = False


def window_from_member(name):
    """`(station, start, end)` from an archive member's name, or `(None,)*3`.

    Matching on the archive's own contents rather than on ledger order is what
    makes several slots safe: with eight addresses in flight the links arrive
    interleaved, and "the oldest submitted chunk" is then simply the wrong
    answer.
    """
    m = MEMBER_RE.match(pathlib.Path(name).name)
    if not m:
        return None, None, None
    station, d1, t1, d2, _ = m.groups()
    return (station,
            datetime.strptime(d1 + t1, "%d%m%Y%H%M%S"),
            datetime.strptime(d2 + "000000", "%d%m%Y%H%M%S"))


def download(url, out_dir, session=None, timeout=120):
    """Streams a link to a scratch file. Returns `(path, size)` or `(None, Expired)`.

    The scratch name carries the pid: several links are in flight at once, one
    per slot, and a shared name would have them overwrite each other.
    """
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"incoming.{os.getpid()}.{abs(hash(url)) % 10**6}.zip.part"
    get = (session or requests).get
    with get(url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if "html" in ctype.lower():
            return None, Expired(
                f"link returned {ctype}, not an archive — it has expired")
        with open(path, "wb") as fh:
            for block in resp.iter_content(1 << 16):
                fh.write(block)
    return path, None


def inspect(path):
    """Opens the archive and says what it holds, without filing anything."""
    size = path.stat().st_size
    try:
        with zipfile.ZipFile(path) as zf:
            members = [n for n in zf.namelist() if not n.endswith("/")]
    except zipfile.BadZipFile:
        return None, size, Expired(f"{size} bytes and not a valid zip")
    if not members:
        return None, size, EmptyArchive(size)
    return members, size, None


def file_archive(path, row, dest_root, members, size, force=False):
    """Moves a verified archive to `<dest_root>/<STATION>/<STATION>_<date>.zip`.

    Staged at the root and filed only once the window is known: taking the
    station from the first ledger row filed every station's data under whichever
    station happened to be listed first, so one campaign wrote GCAM archives
    into `afad_raw/MANT/`.

    Never replaces an existing archive silently. A mislabelled earlier download
    once left the wrong window under this name, and the corrected download
    overwrote 825 MB of good data that had to be fetched again.
    """
    dest = pathlib.Path(dest_root) / row["station"]
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / f"{row['station']}_{row['start'][:10]}.zip"
    if final.exists() and not force:
        keep = dest / f"{row['station']}_{row['start'][:10]}.dup{os.getpid()}.zip"
        path.replace(keep)
        return Unmatched(keep, final.name)
    path.replace(final)
    return Fetched(final, size, len(members), row["station"], row["start"])
