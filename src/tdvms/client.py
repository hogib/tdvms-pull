"""The TDVMS portal, as typed outcomes rather than status codes.

Not a runnable script -- imported only.

The portal answers a submission four ways and one non-way, and the non-way is
the one that cost the most time. Each is a class here, so the supervisor
matches on an outcome instead of re-deriving what `Result=111` meant.

**`AcceptedUnconfirmed` is the important one.** A submission under load dies at
roughly 60 seconds with `RemoteDisconnected`, and the request is accepted
anyway -- the link arrives by email some hours later. A whole evening went into
the theory that the sending address had been banned, on the strength of one
paired sample. It was disproved directly: the base address drew a `111` in 0.9 s
while a brand-new address hit the same ~60 s cutoff, and the address that
"failed" received its link. The cutoff is server-side, under load, and
independent of who is asking. Treating it as a failure releases the claim, and
the next cycle hands the same window to another address -- two requests for one
chunk, and the portal refuses the second.
"""
import requests

STATIONS_URL = "https://tdvms.afad.gov.tr/api/Data/GetStations"
REQUEST_URL = "https://tdvmservis.afad.gov.tr/GetData"

# Networks to look in. This was ["TU"] everywhere -- the station list, the
# device lookup and the submission payload -- so three quarters of what the
# portal serves was invisible: 1,537 stations exist across TU, KO, TB and TK,
# and 390 were reachable. CTKS, 11.9 km from ELBA and the shortest possible
# pair in this campaign's network, is one of the ones that could not be asked
# for.
#
# TK is excluded by default. Its 879 stations are strong-motion
# accelerometers, a different instrument class from the broadbands this
# campaign is built on, and mixing sensor types across a station pair is a
# mistake this project has already made once.
DEFAULT_NETCODES = ("TU", "KO", "TB")

# On the three codes that exist in two networks -- ALT, KULU, MADM -- prefer
# this one, so a bare code keeps resolving the way it always did.
PREFERRED_NETWORK = "TU"

# The portal's own result codes, from the web client it ships.
RESULT_OK, RESULT_QUEUED, RESULT_ERROR, RESULT_BUSY = 0, 109, 110, 111


class Outcome:
    """Base for what a submission did. `slot_taken` drives the queue."""
    slot_taken = False

    def __init__(self, detail=""):
        self.detail = detail

    def __repr__(self):
        return f"{type(self).__name__}({self.detail!r})"


class Accepted(Outcome):
    """Confirmed: the portal took it and will email the link."""
    slot_taken = True

    def __init__(self, code, detail=""):
        super().__init__(detail)
        self.code = code


class AcceptedUnconfirmed(Outcome):
    """Timed out or disconnected mid-POST. Almost always accepted -- see above.

    The claim is KEPT. Releasing it is the duplicate-request bug.
    """
    slot_taken = True


class Busy(Outcome):
    """`111`: the portal is still processing this address's previous request.

    Local state said the slot was free; the portal disagrees, and the portal is
    the authority. The chunk goes back to pending and the slot takes a cooldown.
    """


class Rejected(Outcome):
    """`110`, a non-200, or a station the portal does not list.

    `permanent` separates "this window will never work" from "try again": a
    station missing from the portal's own list will not appear on a retry, so
    requeueing it forever holds a slot against nothing. BAKC and IRLI stalled
    two slots for hours that way, looking exactly like lost mail.
    """

    def __init__(self, detail="", permanent=False):
        super().__init__(detail)
        self.permanent = permanent


class Client:
    """Talks to the portal. Every method returns an `Outcome`, never raises."""

    def __init__(self, timeout=180, session=None, netcodes=DEFAULT_NETCODES):
        self.timeout = timeout
        self.session = session or requests.Session()
        self.netcodes = list(netcodes)
        self._stations = None

    def _load(self):
        """The portal's station table, once per process.

        One round trip a process, not one a chunk: paying it per submission
        made a full pool refill minutes of pure waiting.

        Returns:
            Dict of bare code to the portal's record. Where a code exists in
            two networks the preferred one wins, so a bare code resolves as it
            always did.
        """
        if self._stations is None:
            r = self.session.post(STATIONS_URL,
                                  json={"netcodes": self.netcodes,
                                        "deviceCode": "", "component": ""},
                                  timeout=30)
            r.raise_for_status()
            out = {}
            for s in r.json():
                code = s["code"]
                if code in out and out[code].get("network") == PREFERRED_NETWORK:
                    continue
                out[code] = s
            self._stations = out
        return self._stations

    def station_codes(self):
        """Every station code the portal lists, for validating a plan."""
        return list(self._load())

    def station_network(self, station):
        """The network a bare code resolves to.

        Raises:
            LookupError: The portal does not list it.
        """
        rec = self._load().get(station)
        if rec is None:
            raise LookupError(f"{station}: not in the TDVMS station list")
        return rec.get("network", PREFERRED_NETWORK)

    def device_code(self, station):
        """The instrument code the portal will accept for this station.

        Raises:
            LookupError: Not listed, or listed with no usable instrument.
        """
        rec = self._load().get(station)
        if rec is None:
            raise LookupError(f"{station}: not in the TDVMS station list")
        for flag, code in (("deviceH", "H"), ("deviceL", "L"), ("deviceN", "N")):
            if rec.get(flag):
                return code
        raise LookupError(f"{station}: listed but carries no H/L/N device")

    def submit(self, station, start, end, email):
        """Requests one window for one address.

        Args:
            station: Bare code, e.g. "ELBA". Its network is resolved from the
                portal's own listing rather than assumed.
            start, end: `datetime`, the window bounds.
            email: The plus-address whose slot this consumes.

        Returns:
            Outcome. `Rejected(permanent=True)` for a station the portal does
            not list, which is the one failure a retry cannot fix.
        """
        try:
            device = self.device_code(station)
            network = self.station_network(station)
        except LookupError as e:
            return Rejected(str(e), permanent=True)
        except requests.exceptions.RequestException as e:
            return Rejected(f"station list unreachable ({type(e).__name__})")

        payload = {
            "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
            "data_type": "mseed", "instrument": False,
            "networks": [network], "stations": [station], "location": [None],
            "device_codes": [device], "components": [["Z", "N", "E"]],
            "e_mail": email,
        }
        try:
            resp = self.session.post(REQUEST_URL, json=payload, timeout=self.timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            return AcceptedUnconfirmed(f"{type(e).__name__} — no answer, usually accepted")
        if resp.status_code != 200:
            return Rejected(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            result = resp.json().get("Result")
        except ValueError:
            return Rejected(f"unparseable answer: {resp.text[:200]}")
        if result == RESULT_BUSY:
            return Busy("the portal is still processing this address's last request")
        if result == RESULT_ERROR:
            return Rejected(f"portal returned a general error: {resp.text[:200]}")
        return Accepted(result, f"accepted with Result={result}")
