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

    def __init__(self, timeout=180, session=None):
        self.timeout = timeout
        self.session = session or requests.Session()
        self._devices = {}
        self._codes = None

    def station_codes(self):
        """Every station code the portal lists, for validating a plan.

        Cached with the device codes and for the same reason: one round trip a
        process, not one a chunk.
        """
        if self._codes is None:
            r = self.session.post(STATIONS_URL,
                                  json={"netcodes": ["TU"], "deviceCode": "",
                                        "component": ""}, timeout=30)
            r.raise_for_status()
            self._codes = [s["code"] for s in r.json()]
        return self._codes

    def device_code(self, station):
        """The instrument code the portal will accept for this station.

        Cached for the life of the process: the station list is a 30-second
        round trip and does not change during a campaign, and paying it once
        per submission made a full pool refill take minutes of pure waiting.
        """
        if station in self._devices:
            return self._devices[station]
        r = self.session.post(STATIONS_URL,
                              json={"netcodes": ["TU"], "deviceCode": "", "component": ""},
                              timeout=30)
        r.raise_for_status()
        for s in r.json():
            if s["code"] == station:
                for flag, code in (("deviceH", "H"), ("deviceL", "L"), ("deviceN", "N")):
                    if s.get(flag):
                        self._devices[station] = code
                        return code
                raise LookupError(f"{station}: listed but carries no H/L/N device")
        raise LookupError(f"{station}: not in the TDVMS station list")

    def submit(self, station, start, end, email):
        """Requests one window for one address.

        Args:
            station: Bare code, e.g. "ELBA". The network is always TU here.
            start, end: `datetime`, the window bounds.
            email: The plus-address whose slot this consumes.

        Returns:
            Outcome. `Rejected(permanent=True)` for a station the portal does
            not list, which is the one failure a retry cannot fix.
        """
        try:
            device = self.device_code(station)
        except LookupError as e:
            return Rejected(str(e), permanent=True)
        except requests.exceptions.RequestException as e:
            return Rejected(f"station list unreachable ({type(e).__name__})")

        payload = {
            "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
            "data_type": "mseed", "instrument": False,
            "networks": ["TU"], "stations": [station], "location": [None],
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
