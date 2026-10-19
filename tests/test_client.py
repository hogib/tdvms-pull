"""How a bare station code becomes a network and an instrument in a request.

The ledger knows a station only by its code. Everything else in the request --
the network, the instrument -- comes from the portal's own list, so these pin
how that list is read when one code could mean more than one station.
"""
from datetime import datetime

from tdvms.client import Accepted, Client


def station(code, network, *devices):
    return {"code": code, "network": network,
            **{f"device{d}": d in devices for d in "HLN"}}


class FakeSession:
    """The two portal endpoints: the station list, and the request itself."""

    def __init__(self, *stations):
        self.stations = list(stations)
        self.requests = []

    def post(self, url, json=None, timeout=None):
        if "GetStations" in url:
            return Answer(self.stations)
        self.requests.append(json)
        return Answer({"Result": 0})


class Answer:
    status_code, text = 200, ""

    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def test_a_strong_motion_station_picked_on_purpose_is_requested_as_one():
    """TK was not in the station lookup, so a TK station planned deliberately
    was rejected as unlisted at its first submission -- and a permanent
    rejection retires the whole station."""
    session = FakeSession(station("ELBA", "TU", "H"), station("XT01", "TK", "N"))
    out = Client(session=session).submit("XT01", datetime(2024, 1, 1),
                                         datetime(2024, 1, 22), "you+a1@gmail.com")
    assert isinstance(out, Accepted)
    [req] = session.requests
    assert req["networks"] == ["TK"] and req["device_codes"] == ["N"]


def test_a_code_in_two_networks_still_resolves_to_TU():
    """MADM is listed in TB and TU; a bare MADM has always meant the TU one."""
    c = Client(session=FakeSession(station("MADM", "TB", "H"), station("MADM", "TU", "H")))
    assert c.station_network("MADM") == "TU"


def test_a_code_shared_with_strong_motion_resolves_to_the_broadband():
    """None collides today. If one ever does, a bare code in a broadband
    campaign must not quietly start fetching accelerometer data."""
    for order in ((station("XYZ", "TK", "N"), station("XYZ", "KO", "H")),
                  (station("XYZ", "KO", "H"), station("XYZ", "TK", "N"))):
        assert Client(session=FakeSession(*order)).station_network("XYZ") == "KO"
