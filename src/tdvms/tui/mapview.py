"""A station map drawn in braille: 2x4 dots per terminal cell.

Borders and the earthquake radius are dots; stations are whole cells, so a
marker is always legible and always clickable. The projection is
equirectangular with longitude scaled by cos(39 deg), the middle of Turkey,
which keeps shapes honest across the whole country without a dependency.
"""
import json
import math

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget

from tdvms.catalog import DATA, STRONG_MOTION

COS = math.cos(math.radians(39.0))
HOME = (35.2, 39.0)                    # lon, lat: the view a reset returns to
SPAN = (25.5, 45.0, 35.5, 42.5)        # W, E, S, N that the home view fits
BRAILLE = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (1, 0): 0x08,
           (1, 1): 0x10, (1, 2): 0x20, (0, 3): 0x40, (1, 3): 0x80}

NETWORK_STYLE = {"TU": "#4ea8ff", "KO": "#ffa94d", "TB": "#cc8cff", "TK": "#69db7c"}
OUTLINE_STYLE = "#5c6370"
RADIUS_STYLE = "#ff6b6b"


def load_outline():
    return json.loads((DATA / "outline.json").read_text(encoding="utf-8"))["lines"]


class StationMap(Widget, can_focus=True):
    """Cursor, zoom and selection over a list of `catalog.Station`s."""

    DEFAULT_CSS = """
    StationMap { height: 1fr; min-height: 10; }
    """

    BINDINGS = [
        Binding("left,h", "move(-1, 0)", "←", show=False),
        Binding("right,l", "move(1, 0)", "→", show=False),
        Binding("up,k", "move(0, -1)", "↑", show=False),
        Binding("down,j", "move(0, 1)", "↓", show=False),
        Binding("shift+left,H", "move(-6, 0)", show=False),
        Binding("shift+right,L", "move(6, 0)", show=False),
        Binding("shift+up,K", "move(0, -3)", show=False),
        Binding("shift+down,J", "move(0, 3)", show=False),
        Binding("plus,equals_sign", "zoom(1)", "Zoom in"),
        Binding("minus", "zoom(-1)", "Zoom out"),
        Binding("0", "home", "Whole map"),
        Binding("space,enter", "toggle", "Pick"),
    ]

    class Toggled(Message):
        def __init__(self, station):
            super().__init__()
            self.station = station

    class Hovered(Message):
        def __init__(self, station, lon, lat):
            super().__init__()
            self.station, self.lon, self.lat = station, lon, lat

    def __init__(self, **kw):
        super().__init__(**kw)
        self.outline = load_outline()
        self.stations = []          # what is drawn; filtering is the caller's
        self.selected = set()       # Station.key
        self.planned = set()        # bare codes already in the target ledger
        self.flags = {}             # bare code -> history.Record.kind
        self.epicentre = None       # (lat, lon, radius_km)
        self.center = HOME
        self.cursor = HOME
        self.k = None               # degrees of latitude per dot; None = fit
        self.hovered = None

    # --- projection -----------------------------------------------------------

    def _dims(self):
        return max(self.size.width, 1) * 2, max(self.size.height, 1) * 4

    def _fit(self):
        w, h = self._dims()
        west, east, south, north = SPAN
        return max((east - west) * COS / w, (north - south) / h) * 1.04

    def to_dot(self, lon, lat):
        w, h = self._dims()
        lon0, lat0 = self.center
        return ((lon - lon0) * COS / self.k + w / 2, (lat0 - lat) / self.k + h / 2)

    def to_cell(self, lon, lat):
        x, y = self.to_dot(lon, lat)
        return int(x // 2), int(y // 4)

    def from_cell(self, cx, cy):
        w, h = self._dims()
        lon0, lat0 = self.center
        x, y = cx * 2 + 1, cy * 4 + 2
        return lon0 + (x - w / 2) * self.k / COS, lat0 - (y - h / 2) * self.k

    # --- interaction ----------------------------------------------------------

    def on_resize(self):
        if self.k is None or self.center == HOME:
            self.k = self._fit()
        self._hover()

    def action_move(self, dx, dy):
        self.k = self.k or self._fit()
        lon, lat = self.cursor
        self.cursor = (lon + dx * 2 * self.k / COS, lat - dy * 4 * self.k)
        cx, cy = self.to_cell(*self.cursor)
        if not (1 <= cx < self.size.width - 1 and 0 <= cy < self.size.height):
            self.center = self.cursor
        self._hover()
        self.refresh()

    def action_zoom(self, step, at=None):
        self.k = self.k or self._fit()
        lon, lat = at or self.cursor
        f = 0.7 if step > 0 else 1 / 0.7
        k = min(max(self.k * f, 0.0015), self._fit() * 1.5)
        lon0, lat0 = self.center
        # The point under the cursor stays where it is on screen.
        self.center = (lon - (lon - lon0) * k / self.k, lat - (lat - lat0) * k / self.k)
        self.k = k
        self._hover()
        self.refresh()

    def action_home(self):
        self.center, self.k = HOME, self._fit()
        self._hover()
        self.refresh()

    def action_toggle(self):
        if self.hovered is not None:
            self.post_message(self.Toggled(self.hovered))

    def go(self, lon, lat, k=None):
        """Puts the cursor on a point, re-centring only if it is off screen."""
        self.k = k or self.k or self._fit()
        self.cursor = (lon, lat)
        cx, cy = self.to_cell(lon, lat)
        if k or not (1 <= cx < self.size.width - 1 and 0 <= cy < self.size.height):
            self.center = (lon, lat)
        self._hover()
        self.refresh()

    def frame_radius(self, lat, lon, km):
        """Zooms so a circle of `km` around a point fills most of the view."""
        w, h = self._dims()
        deg = km / 111.2
        self.go(lon, lat, k=max(2.4 * deg * COS / w, 2.4 * deg / h, 0.0015))

    def on_click(self, event):
        self.focus()
        self.cursor = self.from_cell(event.x, event.y)
        self._hover()
        self.action_toggle()
        self.refresh()

    def on_mouse_scroll_up(self, event):
        self.action_zoom(1, at=self.from_cell(event.x, event.y))
        event.stop()

    def on_mouse_scroll_down(self, event):
        self.action_zoom(-1, at=self.from_cell(event.x, event.y))
        event.stop()

    def _hover(self):
        """The station nearest the cursor, within a couple of cells."""
        if self.k is None:
            return
        cx, cy = self.to_dot(*self.cursor)
        best, near = None, 9.0 ** 2
        for s in self.stations:
            x, y = self.to_dot(s.longitude, s.latitude)
            # Cells are 2x4 dots; weigh so "two cells away" means the same
            # distance across as down.
            d = ((x - cx) / 2) ** 2 + ((y - cy) / 4 * 2) ** 2
            if d < near:
                best, near = s, d
        if best is not self.hovered:
            self.hovered = best
        self.post_message(self.Hovered(best, *self.cursor))

    # --- drawing --------------------------------------------------------------

    def render(self):
        width, height = self.size.width, self.size.height
        if width < 2 or height < 2:
            return Text("")
        self.k = self.k or self._fit()
        w, h = width * 2, height * 4
        bits = [[0] * width for _ in range(height)]
        style = [[None] * width for _ in range(height)]

        def plot(x, y, s=None):
            if 0 <= x < w and 0 <= y < h:
                cx, cy = x // 2, y // 4
                bits[cy][cx] |= BRAILLE[(x % 2, y % 4)]
                if s:
                    style[cy][cx] = s

        for line in self.outline:
            pts = [self.to_dot(lon, lat) for lon, lat in line]
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                if max(x0, x1) < 0 or min(x0, x1) >= w or max(y0, y1) < 0 or min(y0, y1) >= h:
                    continue
                _segment(plot, x0, y0, x1, y1)

        if self.epicentre:
            lat, lon, km = self.epicentre
            dlat, dlon = km / 111.2, km / (111.2 * math.cos(math.radians(lat)))
            steps = 240
            for i in range(steps):
                t = 2 * math.pi * i / steps
                x, y = self.to_dot(lon + dlon * math.sin(t), lat + dlat * math.cos(t))
                plot(int(x), int(y), RADIUS_STYLE)

        cells = [[chr(0x2800 + b) if b else " " for b in row] for row in bits]
        styles = [[s or OUTLINE_STYLE for s in row] for row in style]

        # Stations: lowest priority first, so a selected marker is never hidden
        # under an unselected one sharing its cell.
        def rank(s):
            return (s.key in self.selected, s.code in self.planned,
                    s.network not in STRONG_MOTION, self.flags.get(s.code) != "dead")
        labels = []
        for s in sorted(self.stations, key=rank):
            cx, cy = self.to_cell(s.longitude, s.latitude)
            if not (0 <= cx < width and 0 <= cy < height):
                continue
            colour = NETWORK_STYLE.get(s.network, "white")
            if s.key in self.selected:
                cells[cy][cx], styles[cy][cx] = "◉", f"bold {colour}"
                labels.append((cx, cy, s.code, f"bold {colour}"))
            elif s.code in self.planned:
                cells[cy][cx], styles[cy][cx] = "◆", f"{colour}"
            elif not s.requestable:
                cells[cy][cx], styles[cy][cx] = "×", "#666666"
            elif self.flags.get(s.code) == "dead":
                cells[cy][cx], styles[cy][cx] = "⊘", f"dim {colour}"
            else:
                glyph = "·" if s.network in STRONG_MOTION else "•"
                cells[cy][cx], styles[cy][cx] = glyph, colour

        if self.epicentre:
            cx, cy = self.to_cell(self.epicentre[1], self.epicentre[0])
            if 0 <= cx < width and 0 <= cy < height:
                cells[cy][cx], styles[cy][cx] = "✱", f"bold {RADIUS_STYLE}"

        # Labels only where they fit over empty map, and only for what matters:
        # the picked stations once zoomed in, and the hovered one always.
        if self.k >= self._fit() / 2.5:
            labels = []
        if self.hovered is not None:
            s = self.hovered
            cx, cy = self.to_cell(s.longitude, s.latitude)
            labels.append((cx, cy, f"{s.code} {s.network}", "bold white"))
        for cx, cy, text, st in labels:
            x = cx + 1
            if not (0 <= cy < height) or x + len(text) >= width:
                continue
            if all(_blank(cells[cy][x + i]) for i in range(len(text))):
                for i, ch in enumerate(text):
                    cells[cy][x + i], styles[cy][x + i] = ch, st

        cx, cy = self.to_cell(*self.cursor)
        if 0 <= cx < width and 0 <= cy < height:
            if self.hovered is not None:
                hx, hy = self.to_cell(self.hovered.longitude, self.hovered.latitude)
                if 0 <= hx < width and 0 <= hy < height:
                    styles[hy][hx] = f"reverse {styles[hy][hx]}"
            if _blank(cells[cy][cx]):
                cells[cy][cx], styles[cy][cx] = "+", "bold white" if self.has_focus else "white"

        out = Text(no_wrap=True, overflow="crop")
        for y in range(height):
            run, run_style = [], None
            for x in range(width):
                if styles[y][x] != run_style and run:
                    out.append("".join(run), run_style)
                    run = []
                run_style = styles[y][x]
                run.append(cells[y][x])
            if run:
                out.append("".join(run), run_style)
            if y < height - 1:
                out.append("\n")
        return out


def _blank(ch):
    """Empty map: a space or any braille cell, which a label may cover."""
    return ch == " " or 0x2800 <= ord(ch) < 0x2900


def _segment(plot, x0, y0, x1, y1):
    """Integer line between two dot positions, clipped by `plot` itself."""
    x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    for _ in range(dx - dy + 1):
        plot(x0, y0)
        if x0 == x1 and y0 == y1:
            return
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy
