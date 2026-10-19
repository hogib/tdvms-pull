"""What every campaign so far has learnt about each station.

Not a runnable script -- imported only.

`run` retires a station that keeps answering no-data, but only within one
campaign, and only after spending `--nodata-streak` requests learning it. BLKS
answered no-data 26 times out of 26; the next campaign to include it would have
paid another eight requests to find that out again. The ledgers already know,
so `plan` and the TUI ask them first.

No-data is a fact about a station *and a period* -- a station can be silent for
a year and fine since -- so this warns and never refuses, and it says which
period the silence covers.
"""
from dataclasses import dataclass, field


@dataclass
class Record:
    code: str
    fetched: int = 0
    nodata: int = 0
    failed: int = 0
    retired: int = 0
    silent_from: str | None = None     # the span its no-data windows cover
    silent_to: str | None = None
    campaigns: set = field(default_factory=set)

    @property
    def answered(self):
        return self.fetched + self.nodata

    @property
    def kind(self):
        """`"dead"`, `"patchy"`, or None when there is nothing to warn about."""
        if self.fetched == 0 and (self.nodata >= 3 or self.retired):
            return "dead"
        if self.nodata >= 4 and self.nodata / self.answered >= 0.5:
            return "patchy"
        return None

    def warning(self):
        kind = self.kind
        if kind is None:
            return None
        where = ", ".join(sorted(self.campaigns))
        span = (f" for {self.silent_from}..{self.silent_to}"
                if self.silent_from else "")
        if kind == "dead":
            what = (f"{self.nodata} no-data answer(s){span} and nothing ever fetched"
                    if self.nodata else "retired without ever returning data")
            return f"{self.code}: {what} ({where})"
        return (f"{self.code}: no data for {self.nodata} of {self.answered} windows "
                f"answered{span} ({where})")


def gather(campaigns):
    """`{code: Record}` across every campaign's ledger."""
    out = {}
    for c in campaigns:
        for r in c.ledger.rows():
            state = r["state"]
            if state not in ("fetched", "nodata", "failed", "retired"):
                continue          # no outcome yet, so nothing learnt
            rec = out.setdefault(r["station"], Record(r["station"]))
            if state == "fetched":
                rec.fetched += 1
            elif state == "nodata":
                rec.nodata += 1
                start, end = r["start"][:10], (r.get("end") or r["start"])[:10]
                rec.silent_from = min(filter(None, (rec.silent_from, start)))
                rec.silent_to = max(filter(None, (rec.silent_to, end)))
            elif state == "failed":
                rec.failed += 1
            else:
                rec.retired += 1
            rec.campaigns.add(c.name)
    return out


def around(ledger_path):
    """Every campaign near a ledger: beside it, and in the project it sits in."""
    import pathlib

    from tdvms.campaign import CAMPAIGNS, discover
    p = pathlib.Path(ledger_path).absolute()
    roots = {p.parent, pathlib.Path.cwd()}
    if p.parent.parent.name == CAMPAIGNS:
        roots.add(p.parent.parent.parent)
    seen, out = set(), []
    for root in roots:
        for c in discover(root):
            key = c.path.resolve()
            if key not in seen:
                seen.add(key)
                out.append(c)
    return out
