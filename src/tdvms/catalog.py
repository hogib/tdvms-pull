"""The station and earthquake catalogs, shipped with the package.

    python -m tdvms.catalog            # regenerate both from their sources

Imported by the TUI; nothing in the campaign loop depends on it.

Both live in `tdvms/data/` as CSV and are tracked in git, so a fresh clone can
plan a campaign -- pick stations off a map, centre them on an earthquake --
without a round trip to anything. CSV rather than JSON because a refresh is
then a reviewable diff: a station that appears, moves or loses its broadband
sensor shows up as one changed line.

**Stations** come from the portal's own list (`client.STATIONS_URL`), across
all four networks. Codes are NOT unique across networks -- ALT, KULU and MADM
are each listed twice -- so a station here is `(network, code)`, while a ledger
row is keyed by code alone and `run` resolves a doubled code to TU.

**Events** come from the USGS FDSN service: M5+ since 1990 over the box the
stations occupy. AFAD's own event API is the natural source, but it answers
every query from outside its network with an empty list, and a catalog that
regenerates empty is worse than one from a second agency.
"""
import csv
import io
import math
import pathlib
from dataclasses import dataclass

from tdvms.client import DEFAULT_NETCODES, STRONG_MOTION

DATA = pathlib.Path(__file__).parent / "data"
STATIONS = DATA / "stations.csv"
EVENTS = DATA / "events.csv"

# Every network the portal serves; TK is strong motion (see `client`).
NETWORKS = DEFAULT_NETCODES

# The box every station in the portal's list falls inside, with a margin.
BOX = {"minlatitude": 34.5, "maxlatitude": 43.0,
       "minlongitude": 25.0, "maxlongitude": 45.5}
EVENTS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
EVENTS_SINCE, EVENTS_MINMAG = "1990-01-01", 5.0


@dataclass(frozen=True)
class Station:
    code: str
    network: str
    latitude: float
    longitude: float
    devices: str          # instrument codes the portal lists, of "HLN"

    @property
    def key(self):
        return f"{self.network}.{self.code}"

    @property
    def requestable(self):
        """Carries an instrument `client.device_code` will accept."""
        return bool(self.devices)


@dataclass(frozen=True)
class Event:
    id: str
    time: str             # ISO UTC, seconds
    magnitude: float
    latitude: float
    longitude: float
    depth_km: float
    place: str

    @property
    def date(self):
        return self.time[:10]


def distance_km(lat1, lon1, lat2, lon2):
    """Great-circle distance. Haversine is plenty at these ranges."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def load_stations(path=STATIONS):
    with open(path, newline="") as fh:
        return [Station(r["code"], r["network"], float(r["latitude"]),
                        float(r["longitude"]), r["devices"])
                for r in csv.DictReader(fh)]


def load_events(path=EVENTS):
    with open(path, newline="") as fh:
        return [Event(r["id"], r["time"], float(r["magnitude"]), float(r["latitude"]),
                      float(r["longitude"]), float(r["depth_km"]), r["place"])
                for r in csv.DictReader(fh)]


# --- regeneration ------------------------------------------------------------

def fetch_stations(session=None):
    """The portal's list, every network, as `Station`s sorted by key."""
    import requests

    from tdvms.client import STATIONS_URL
    r = (session or requests).post(STATIONS_URL, json={"netcodes": list(NETWORKS),
                                                       "deviceCode": "", "component": ""},
                                   timeout=60)
    r.raise_for_status()
    out = {}
    for s in r.json():
        if s.get("latitude") is None or s.get("longitude") is None:
            continue
        devices = "".join(c for c in "HLN" if s.get(f"device{c}"))
        st = Station(s["code"], s["network"], round(s["latitude"], 5),
                     round(s["longitude"], 5), devices)
        out[st.key] = st
    return sorted(out.values(), key=lambda s: (s.network, s.code))


def fetch_events(session=None):
    """M5+ since 1990 over the station box, oldest first."""
    import requests
    from datetime import datetime, timezone
    r = (session or requests).get(EVENTS_URL, params={
        "format": "geojson", "starttime": EVENTS_SINCE, "minmagnitude": EVENTS_MINMAG,
        "orderby": "time-asc", **BOX}, timeout=120)
    r.raise_for_status()
    out = []
    for f in r.json()["features"]:
        p, (lon, lat, depth) = f["properties"], f["geometry"]["coordinates"]
        when = datetime.fromtimestamp(p["time"] / 1000, timezone.utc)
        out.append(Event(f["id"], when.strftime("%Y-%m-%dT%H:%M:%S"), p["mag"],
                         round(lat, 4), round(lon, 4), round(depth or 0, 1),
                         p.get("place") or ""))
    return out


def write_stations(stations, path=STATIONS):
    _write(path, ("code", "network", "latitude", "longitude", "devices"),
           ((s.code, s.network, s.latitude, s.longitude, s.devices) for s in stations))


def write_events(events, path=EVENTS):
    _write(path, ("id", "time", "magnitude", "latitude", "longitude", "depth_km", "place"),
           ((e.id, e.time, e.magnitude, e.latitude, e.longitude, e.depth_km, e.place)
            for e in events))


def _write(path, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    tmp = pathlib.Path(path).with_suffix(".tmp")
    tmp.write_text(buf.getvalue())
    tmp.replace(path)


def main():
    stations = fetch_stations()
    write_stations(stations)
    by = {}
    for s in stations:
        by[s.network] = by.get(s.network, 0) + 1
    print(f"{STATIONS}: {len(stations)} stations "
          f"({', '.join(f'{n} {by.get(n, 0)}' for n in NETWORKS)})")
    events = fetch_events()
    write_events(events)
    print(f"{EVENTS}: {len(events)} events, M{EVENTS_MINMAG:g}+ since {EVENTS_SINCE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
