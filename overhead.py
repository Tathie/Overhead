"""Overhead: shows the closest aircraft within 3 miles of you.
Run with:  python overhead.py   (or double-click overhead.pyw on Windows)
Optional:  pip install pillow   (enables aircraft photos)
Location:  waits for a refined fix from Windows Location Services (which uses the Wi-Fi adapter when
           available); falls back to your IP address if Windows can't provide one.
Resize:    drag the window edges; everything scales with the window width.
"""
import io, json, math, os, queue, subprocess, threading, time, webbrowser
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timezone
from tkinter import ttk
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from PIL import Image, ImageTk
    LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
except ImportError:
    Image = ImageTk = LANCZOS = None

RADIUS_MI = 3
POLL_MS = 10_000
UI_SCALE = 1.0                       # content size on the very first launch
ZOOM_MIN, ZOOM_MAX = 0.6, 3.0        # content scales with window width between these limits
HOME = os.path.expanduser("~")
CONFIG = os.path.join(HOME, ".overhead.json")
OLD_CONFIG = os.path.join(HOME, ".flight_tracker.json")
LOG_PATH = os.path.join(HOME, ".overhead_log.json")
SIGHTING_GAP_S = 24 * 3600  # an aircraft counts once; it can count again only after 24 hours out of range

# Night palette with gentle blue accents
BG, CARD, BORDER = "#0b1118", "#121b25", "#1f2b38"
TX, MUTED, ACCENT = "#e6edf5", "#7f90a3", "#6fa8dc"
FIELD, BTN, BTN_HOVER = "#0e161e", "#16212c", "#1d2b39"

route_cache, photo_cache, aircraft_cache, wiki_cache = {}, {}, {}, {}


def enable_dpi_awareness():
    """Tells Windows we can handle high-DPI screens ourselves, so it stops stretching the window like a
    bitmap (that's what makes the text look blurry). Tries the newer API first, then the old one, and if
    neither works we just shrug and carry on."""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def load_config():
    """Grabs the saved settings (basically just the window size). Checks the new config file first, then
    the old one from back when this was called Flight Tracker. Nothing there? You get an empty dict."""
    for path in (CONFIG, OLD_CONFIG):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def load_log():
    """Reads the sightings log off disk. You get back two things: how many times we've spotted each
    model, and when each individual aircraft was last nearby. If the file's missing or busted, we just
    start fresh with empty ones."""
    try:
        with open(LOG_PATH) as f:
            d = json.load(f)
        return dict(d.get("models", {})), dict(d.get("seen", {}))
    except Exception:
        return {}, {}


def fmt_utc(epoch):
    """Turns a Unix timestamp into something readable, like 10-14-2026 03:45 PM (in UTC).
    Prefer a 24-hour clock? Just swap %I:%M %p for %H:%M."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%m-%d-%Y %I:%M %p")


def get_bytes(url):
    """Downloads whatever's at the URL and hands back the raw bytes. We send a User-Agent because some
    APIs get grumpy without one, and give up after 10 seconds so nothing hangs forever."""
    req = Request(url, headers={"User-Agent": "Overhead/1.1"})
    with urlopen(req, timeout=10) as r:
        return r.read()


def get_json(url):
    """Same as get_bytes, but for APIs that talk JSON. Fetches it and turns it into Python stuff."""
    return json.loads(get_bytes(url).decode("utf-8"))


# A little PowerShell script that asks Windows where we are. It keeps listening for up to 40 seconds and
# prints a "lat,lon,accuracy" line every time the fix gets better. It bails early once we're within 50 m,
# if things stop improving for 10 seconds, or if nothing shows up at all after 12 seconds.
# (We squash it onto one line so it can be passed straight to powershell -Command.)
LOCATION_SCRIPT = " ".join("""
$ErrorActionPreference = 'SilentlyContinue';
Add-Type -AssemblyName System.Device;
$c = [cultureinfo]::InvariantCulture;
$w = New-Object System.Device.Location.GeoCoordinateWatcher([System.Device.Location.GeoPositionAccuracy]::High);
$null = $w.TryStart($false, [TimeSpan]::FromSeconds(5));
if ($w.Status -eq 'Disabled' -or $w.Permission -eq 'Denied') { exit };
$start = Get-Date; $improved = $start; $best = [double]::MaxValue;
while (((Get-Date) - $start).TotalSeconds -lt 40) {
  $l = $w.Position.Location;
  if (-not $l.IsUnknown) {
    $acc = $l.HorizontalAccuracy;
    if ([double]::IsNaN($acc)) { $acc = 100000 };
    if ($acc -lt $best) {
      $best = $acc; $improved = Get-Date;
      Write-Output ($l.Latitude.ToString($c) + ',' + $l.Longitude.ToString($c) + ',' + $acc.ToString($c))
    };
    if ($best -le 50) { break };
    if (((Get-Date) - $improved).TotalSeconds -gt 10) { break }
  } elseif (((Get-Date) - $start).TotalSeconds -gt 12) { break };
  Start-Sleep -Milliseconds 500
}
""".split())


def windows_location_stream(on_fix):
    """Asks Windows Location Services where we are (it'll use Wi-Fi positioning if there's an adapter).
    Every time the position gets more accurate, we call on_fix(lat, lon, accuracy_m) so the app can
    update right away instead of waiting for the perfect answer. Stops once it's accurate enough or
    has settled down. Returns True if we got at least one position, False if Windows came up empty
    (or we're not on Windows at all)."""
    if os.name != "nt":
        return False
    got, proc, watchdog = False, None, None
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", LOCATION_SCRIPT],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            text=True, creationflags=0x08000000)  # CREATE_NO_WINDOW: no console flash
        watchdog = threading.Timer(60, proc.kill)   # safety net: kill PowerShell if it ever gets stuck
        watchdog.start()
        for line in proc.stdout:
            try:
                lat, lon, acc = (float(x) for x in line.strip().split(","))
            except ValueError:
                continue
            got = True
            on_fix(lat, lon, acc)
    except Exception:
        pass
    finally:
        if watchdog:
            watchdog.cancel()
        if proc and proc.poll() is None:
            proc.kill()
    return got


def wikipedia_page(model):
    """Finds the Wikipedia article that best matches an aircraft model and returns (title, url), or None
    if there just isn't one. First tries the full name (say "Boeing 737-800"), then the name without the
    variant bit ("Boeing 737"). If every attempt failed because Wikipedia was unreachable, it raises
    instead, so callers can tell "no page" apart from "no internet"."""
    names = [model]
    base = model.split("-")[0].strip()
    if base and base != model:
        names.append(base)
    failures = 0
    for name in names:
        try:
            j = get_json("https://en.wikipedia.org/w/api.php?action=opensearch&limit=1&format=json&search="
                         + quote(name))
            if j[1]:
                return j[1][0], j[3][0]
        except Exception:
            failures += 1
    if failures == len(names):
        raise RuntimeError("Wikipedia unreachable")
    return None


def wikipedia_url(model):
    """Gives you a Wikipedia link for a model no matter what. If we can find the actual article, great;
    if not, you get a Wikipedia search page for it instead."""
    try:
        page = wikipedia_page(model)
    except Exception:
        page = None
    return page[1] if page else "https://en.wikipedia.org/w/index.php?search=" + quote(model)


def wikipedia_photo(model):
    """Our backup photo: grabs the main picture from the model's Wikipedia article and returns
    (PIL image, credit), or None if there's no page or no picture. Results get cached so we don't keep
    asking Wikipedia about the same plane. Raises if Wikipedia can't be reached, so the caller knows to
    try again later instead of giving up for good."""
    if Image is None or not model or model == "—":
        return None
    if model in wiki_cache:
        return wiki_cache[model]
    result = None
    page = wikipedia_page(model)
    if page:
        title = page[0]
        j = get_json("https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(title.replace(" ", "_"), safe=""))
        src = (j.get("thumbnail") or {}).get("source")
        if src:
            try:
                result = (fetch_image(src), f"Wikipedia · {title} (model photo)")
            except Exception:
                result = None
    wiki_cache[model] = result
    return result


def dist_mi(lat1, lon1, lat2, lon2):
    """How far apart two points are, in miles, as the crow flies. This is the haversine formula,
    which accounts for the Earth being round (3958.8 is the Earth's radius in miles)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl, dn = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dl / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dn / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def direction(lat1, lon1, lat2, lon2):
    """Which way you'd look to see point 2 from point 1, as a compass direction like "NE" or "W".
    Works out the exact bearing in degrees, then snaps it to the nearest of the 8 compass points."""
    p1, p2, dn = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dn) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dn)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][round(deg / 45) % 8]


def nearby(lat, lon):
    """Asks the live ADS-B feeds for every aircraft around us. We ask for a slightly bigger circle than
    we need and trim it down to the exact radius later. airplanes.live is the main source; if it's down,
    adsb.fi steps in."""
    nm = math.ceil(RADIUS_MI / 1.15078) + 1   # feed radius is in nautical miles; add a little margin
    try:
        return get_json(f"https://api.airplanes.live/v2/point/{lat}/{lon}/{nm}").get("ac") or []
    except Exception:
        return get_json(f"https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{nm}").get("aircraft") or []


def lookup_route(callsign):
    """Figures out where a flight is coming from and going to, based on its callsign (like "UAL123").
    Uses adsbdb and caches the answer, since a flight's route isn't going to change mid-air. Returns
    None if there's no callsign or nobody knows the route."""
    if not callsign:
        return None
    if callsign not in route_cache:
        try:
            resp = get_json(f"https://api.adsbdb.com/v0/callsign/{callsign}").get("response")
            route_cache[callsign] = resp.get("flightroute") if isinstance(resp, dict) else None
        except Exception:
            route_cache[callsign] = None
    return route_cache[callsign]


def lookup_aircraft(hex_id):
    """Looks up the details on this specific plane (manufacturer, type, registration, sometimes a photo)
    using its hex ID from adsbdb. Good answers get cached; if the request fails we don't cache anything,
    so it'll just try again on the next poll."""
    if hex_id not in aircraft_cache:
        try:
            resp = get_json(f"https://api.adsbdb.com/v0/aircraft/{hex_id}").get("response")
            aircraft_cache[hex_id] = resp.get("aircraft") if isinstance(resp, dict) else None
        except Exception:
            return None  # transient failure; try again on the next poll
    return aircraft_cache[hex_id]


def fetch_image(url):
    """Downloads a picture and opens it with Pillow. We cap it at 1200x800 so giant photos don't eat
    memory, but keep it big enough to look crisp once it's scaled down to fit the window."""
    img = Image.open(io.BytesIO(get_bytes(url))).convert("RGB")
    img.thumbnail((1200, 800))  # keep native detail; it's resized to fit the window later
    return img


def lookup_photo(hex_id, model=None):
    """Hunts down a photo of this exact plane and returns (PIL image, credit text), or None.
    Planespotters goes first; if that's missing or kind of small, we also check airport-data.com and
    keep whichever photo is bigger (so it still looks sharp on high-res screens). Still nothing? We fall
    back to a generic shot of the model from Wikipedia."""
    if Image is None:
        return None
    if hex_id in photo_cache:
        return photo_cache[hex_id]
    best, errored = None, False

    def consider(url, credit):
        """Downloads a candidate photo and keeps it if it beats the best one we've got so far."""
        nonlocal best
        img = fetch_image(url)
        if best is None or img.width > best[0].width:
            best = (img, credit)

    try:
        photos = get_json(f"https://api.planespotters.net/pub/photos/hex/{hex_id}").get("photos") or []
        if photos:
            p = photos[0]
            src = (p.get("thumbnail_large") or p.get("thumbnail") or {}).get("src")
            if src:
                consider(src, f"Photo: {p.get('photographer') or 'unknown'} · planespotters.net")
    except Exception:
        errored = True
    if best is None or best[0].width < 700:
        try:
            ac = lookup_aircraft(hex_id)
            src = ac and (ac.get("url_photo") or ac.get("url_photo_thumbnail"))
            if src:
                consider(src, "Photo: airport-data.com")
        except Exception:
            errored = True
    if best is None and model:  # no photo of this exact aircraft: fall back to its model's Wikipedia picture
        try:
            best = wikipedia_photo(model)
        except Exception:
            errored = True
    if best or not errored:  # don't permanently cache a network hiccup
        photo_cache[hex_id] = best
    return best


def model_text(a, ac):
    """Comes up with a nice name for the aircraft model. Prefers the feed's own description, then
    manufacturer + type from adsbdb, then the short type code (like "B738"), and "—" if we've got nothing."""
    if a.get("desc"):
        return str(a["desc"]).title()
    if ac:
        name = " ".join(x for x in (ac.get("manufacturer"), ac.get("type")) if x).strip()
        if name:
            return name
    return a.get("t") or "—"


def altitude_text(alt):
    """Makes the altitude readable: "On ground" if it's parked/taxiing, "12,345 ft" if it's flying,
    or "—" if the feed didn't say."""
    if alt == "ground":
        return "On ground"
    if isinstance(alt, (int, float)):
        return f"{round(alt):,} ft"
    return "—"


def place(p):
    """Turns an airport record into (code, city), like ("SEA", "Seattle"). Uses the familiar 3-letter
    IATA code when there is one, otherwise the 4-letter ICAO code."""
    if not p:
        return "—", "Unknown"
    return p.get("iata_code") or p.get("icao_code") or "—", p.get("municipality") or p.get("name") or ""


def label(parent, **kw):
    """Little shortcut for making a Label that matches its parent's background and uses our normal
    text color, so we don't have to spell that out every single time."""
    kw.setdefault("bg", parent["bg"])
    kw.setdefault("fg", TX)
    return tk.Label(parent, **kw)


class App(tk.Tk):
    """The whole app lives in here: the window, the two tabs, and all the polling."""

    def __init__(self):
        """Sets everything up: figures out the screen scaling, restores the last window size, creates all
        the state we track, builds the UI, and kicks off the timers (location, polling, the clock, etc.)."""
        super().__init__()
        cfg = load_config()
        self.s = self.winfo_fpixels("1i") / 96   # display scaling (e.g. 1.25 at 125%)
        self.base_w = int(458 * self.s)          # window width at 100% content size
        self.min_w = int(self.base_w * ZOOM_MIN)
        self.title("Overhead")
        self.configure(bg=BG)
        # Restore the last window size if there is one; otherwise open fitted to the content.
        self.saved = False
        try:
            if cfg.get("layout") != 3:
                raise KeyError("saved size is from an older layout")
            w = min(int(cfg["w"]), self.winfo_screenwidth())
            h = min(int(cfg["h"]), self.winfo_screenheight())
            if w >= self.min_w and h > 200:
                self.geometry(f"{w}x{h}")
                self.saved = True
        except (KeyError, TypeError, ValueError):
            pass
        self.zoom = self.zoom_for(w) if self.saved else UI_SCALE
        self.pos, self.busy, self.q = None, False, queue.Queue()
        self.tkimg, self.cur_photo, self.has_plane, self._fonts = None, None, False, []
        self.ready, self.auto_h, self._resize_job, self._save_job = False, None, None, None
        self.last_ok = None
        self.sel_hex, self.inrange, self.order = None, [], []   # which aircraft is shown / all in range / cycle order
        self.next_visible, self.again = False, False
        self.flight_url, self.cur_model = None, None
        self.tab, self._saved_at = "main", 0.0
        self.sort_key, self.sort_desc = "last", True   # Log opens sorted by last sighted, most recent first
        self.log, self.seen = load_log()
        self.vars = {k: tk.StringVar(value="—") for k in
                     ("flight", "sub", "origin", "origin_city", "dest", "dest_city",
                      "model", "speed", "altitude", "distance")}
        self.credit = tk.StringVar()
        self.status = tk.StringVar(value="Finding your location…")
        self.updated = tk.StringVar(value="Waiting for first update…")
        self.build()
        self.reset()

        self.bind("<Configure>", self.on_configure)
        self.bind_all("<MouseWheel>", self._wheel)
        self.after(300, self.become_ready)

        self.after(100, self.dark_titlebar)
        self.find_location()
        self.after(200, self.drain)
        self.after(POLL_MS, self.tick_loop)
        self.after(1000, self.clock)

    # ---------- sizing ----------
    def P(self, n):
        """Converts a "design" pixel size into real pixels, taking both the display scaling and the
        current zoom into account. Pretty much every size in the UI goes through this."""
        return int(n * self.s * self.zoom)

    def f(self, size, bold=False, underline=False):
        """Makes a Segoe UI font that's scaled to the current zoom (but never smaller than 6pt so it
        stays readable)."""
        font = tkfont.Font(family="Segoe UI", size=max(6, round(size * self.zoom)),
                           weight="bold" if bold else "normal", underline=underline)
        self._fonts.append(font)  # keep a reference so Tk doesn't discard it
        return font

    def zoom_for(self, width):
        """Works out how zoomed-in everything should be for a given window width, kept between the
        min and max zoom limits."""
        return round(min(ZOOM_MAX, max(ZOOM_MIN, width / self.base_w)), 2)

    def become_ready(self):
        """Runs a moment after startup, once the window has actually appeared. From here on we start
        reacting to resizes. If we didn't restore a saved size, we remember the starting height so we know
        it's still ours to auto-adjust."""
        self.ready = True
        self.auto_h = None if self.saved else self.winfo_height()
        self.fit_height()

    def on_configure(self, e):
        """Fires whenever the window moves or resizes. If the width changed enough to matter, we schedule a
        rescale (waiting a beat so we're not rebuilding constantly mid-drag), and we also schedule saving
        the new size."""
        if str(e.widget) != "." or not self.ready:
            return
        if abs(self.zoom_for(e.width) - self.zoom) >= 0.03:
            if self._resize_job:
                self.after_cancel(self._resize_job)
            self._resize_job = self.after(150, self.apply_resize)
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(600, self.save_config)

    def apply_resize(self):
        """Once you've stopped dragging, recalculates the zoom and rebuilds the UI if it actually changed."""
        self._resize_job = None
        z = self.zoom_for(self.winfo_width())
        if z != self.zoom:
            self.zoom = z
            self.rebuild()

    def fit_height(self):
        """Makes sure the window's always tall enough to show everything. If you haven't dragged the
        height yourself, it also shrink-wraps the window to fit the content snugly."""
        if not self.ready:
            return
        self.update_idletasks()
        need = self.winfo_reqheight()
        self.minsize(self.min_w, need)
        cur_w, cur_h = self.winfo_width(), self.winfo_height()
        if cur_h < need or (self.auto_h is not None and abs(cur_h - self.auto_h) <= 3):
            self.geometry(f"{cur_w}x{need}")
            self.auto_h = need

    def rebuild(self):
        """Tears down the whole UI and builds it again at the new zoom level. Easier than resizing every
        widget one by one. Puts the photo back afterwards too."""
        for w in self.winfo_children():
            w.destroy()
        self._fonts = []
        self.build()
        self.apply_photo()
        self.fit_height()

    def dark_titlebar(self):
        """Switches the window's title bar to dark mode so it doesn't clash with the rest of the app.
        Attribute 20 is the current Windows setting and 19 is the older one, so we just try both."""
        try:  # Windows 10/11 only; silently skipped elsewhere
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            for attr in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(1)), 4)
        except Exception:
            pass

    # ---------- UI ----------
    def build(self):
        """Builds the entire UI from scratch: the tab headers up top, the main card with the flight info
        and photo, the status box, and the Log page. Gets called again any time the zoom changes."""
        P, f = self.P, self.f
        W = P(430)
        inner = W - P(36)
        photo_w, gap = P(190), P(12)
        left_w = inner - photo_w - gap
        pad = {"padx": P(18)}

        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=P(18), pady=(P(14), P(6)))
        self.tab_lbls, self.tab_bars = {}, {}
        for key, text in (("main", "✈  Closest aircraft"), ("log", "Log")):
            cell = tk.Frame(head, bg=BG)
            cell.pack(side="left", padx=(0, P(18)))
            lbl = label(cell, text=text, fg=MUTED, font=f(10, True), cursor="hand2")
            lbl.pack()
            lbl.bind("<Button-1>", lambda e, k=key: self.show_tab(k))
            bar = tk.Frame(cell, bg=BG, height=max(2, P(2)))
            bar.pack(fill="x", pady=(P(3), 0))
            self.tab_lbls[key], self.tab_bars[key] = lbl, bar

        # Both pages share one cell; the window is sized to the taller one so switching never resizes it
        self.pages = tk.Frame(self, bg=BG)
        self.pages.pack(fill="both", expand=True)
        self.pages.grid_rowconfigure(0, weight=1)
        self.pages.grid_columnconfigure(0, weight=1)
        self.page_main = tk.Frame(self.pages, bg=BG)
        self.page_log = tk.Frame(self.pages, bg=BG)
        self.page_main.grid(row=0, column=0, sticky="nsew")
        self.page_log.grid(row=0, column=0, sticky="nsew")

        card = tk.Frame(self.page_main, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x", padx=P(14), pady=P(4), ipady=P(8))
        tk.Frame(card, bg=CARD, width=W, height=1).pack()   # keeps the window width steady between states

        # Top row: flight info on the left, small aircraft photo on the right
        top = tk.Frame(card, bg=CARD)
        top.pack(fill="x", pady=(P(10), 0), **pad)
        self.photo_box = tk.Frame(top, bg=CARD)
        self.photo = label(self.photo_box)
        self.credit_lbl = label(self.photo_box, textvariable=self.credit, fg=MUTED, font=f(8),
                                wraplength=photo_w, justify="left")

        info = tk.Frame(top, bg=CARD)
        info.pack(side="left", fill="both", expand=True, anchor="n")
        self.info, self.gap = info, gap
        self.flight_font, self.flight_font_ul = f(28, True), f(28, True, underline=True)
        flight_lbl = label(info, textvariable=self.vars["flight"], font=self.flight_font, wraplength=inner,
                           justify="left", anchor="w")
        flight_lbl.pack(anchor="w")
        flight_lbl.bind("<Button-1>", self.open_flight)
        flight_lbl.bind("<Enter>", lambda e: self.link_hover(True))
        flight_lbl.bind("<Leave>", lambda e: self.link_hover(False))
        self.flight_lbl = flight_lbl
        label(info, textvariable=self.vars["sub"], fg=MUTED, font=f(10), wraplength=left_w,
              justify="left").pack(anchor="w")

        # Codes and arrow share one grid row so the dashes always sit level with the arrow;
        # city names live on a second row underneath.
        route = tk.Frame(info, bg=CARD)
        route.pack(fill="x", pady=P(10))
        big = f(18, True)
        label(route, textvariable=self.vars["origin"], font=big).grid(row=0, column=0, sticky="w")
        label(route, text="→", fg=ACCENT, font=big).grid(row=0, column=1, padx=P(8))
        label(route, textvariable=self.vars["dest"], font=big).grid(row=0, column=2, sticky="w")
        label(route, textvariable=self.vars["origin_city"], fg=MUTED, font=f(9), wraplength=P(80),
              justify="left").grid(row=1, column=0, sticky="nw")
        label(route, textvariable=self.vars["dest_city"], fg=MUTED, font=f(9), wraplength=P(80),
              justify="left").grid(row=1, column=2, sticky="nw")

        # Details stacked below
        self.model_font, self.model_font_ul = f(14, True), f(14, True, underline=True)
        for text, key in (("MODEL", "model"), ("SPEED", "speed"), ("ALTITUDE", "altitude"), ("DISTANCE", "distance")):
            row = tk.Frame(card, bg=CARD)
            row.pack(fill="x", pady=(P(6), 0), **pad)
            label(row, text=text, fg=MUTED, font=f(8)).pack(anchor="w")
            value = label(row, textvariable=self.vars[key], font=self.model_font if key == "model" else f(14, True),
                          wraplength=inner, justify="left")
            value.pack(anchor="w")
            if key == "model":   # links to the model's Wikipedia page; underlined only on hover, like the flight number
                self.model_lbl = value
                value.bind("<Button-1>", lambda e: self.cur_model and self.open_wikipedia(self.cur_model))
                value.bind("<Enter>", lambda e: self.model_hover(True))
                value.bind("<Leave>", lambda e: self.model_hover(False))

        box = tk.Frame(self.page_main, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        box.pack(fill="x", padx=P(14), pady=P(10), ipady=P(6))
        srow = tk.Frame(box, bg=CARD)
        srow.pack(anchor="w", padx=P(10), pady=(P(6), 0))
        label(srow, textvariable=self.status, fg=MUTED, font=f(9), wraplength=P(340),
              justify="left").pack(side="left")
        self.next_lbl = label(srow, text="Show next", fg=ACCENT, font=f(9, underline=True), cursor="hand2")
        self.next_lbl.bind("<Button-1>", self.show_next)
        if self.next_visible:
            self.next_lbl.pack(side="left", padx=(P(8), 0))
        label(box, textvariable=self.updated, fg=ACCENT, font=f(9)).pack(anchor="w", padx=P(10), pady=(P(2), 0))

        self.build_log_page()
        self.show_tab(self.tab)
        self.refresh_log()

    def fit_photo(self, img):
        """Shrinks (or grows) the photo to fit its spot on the card at the current zoom, keeping its shape
        and using a high-quality filter so it doesn't look jaggy."""
        max_w, max_h = self.P(190), self.P(130)
        r = min(max_w / img.width, max_h / img.height)
        return img.resize((max(1, int(img.width * r)), max(1, int(img.height * r))), LANCZOS)

    def apply_photo(self):
        """Puts the current photo (and its credit line) on the card. If Pillow isn't installed, shows a
        hint about how to get photos instead. If there's nothing to show, hides the photo area entirely."""
        self.photo.pack_forget()
        self.credit_lbl.pack_forget()
        if self.cur_photo and Image is not None:
            self.tkimg = ImageTk.PhotoImage(self.fit_photo(self.cur_photo[0]))
            self.photo.config(image=self.tkimg)
            self.photo.pack()
            self.credit.set(self.cur_photo[1])
            self.credit_lbl.pack(anchor="w", pady=(self.P(3), 0))
        elif Image is None and self.has_plane:
            self.credit.set("Aircraft photos need Pillow:  pip install pillow")
            self.credit_lbl.pack(anchor="w")
        # Only reserve the right-hand column while there is something in it. (An emptied frame keeps its
        # old size, which squeezed the text beside it.)
        has_photo_area = bool(self.cur_photo and Image is not None) or (Image is None and self.has_plane)
        if has_photo_area:
            self.photo_box.pack(side="right", anchor="n", padx=(self.gap, 0), before=self.info)
        else:
            self.photo_box.pack_forget()

    def show(self, flight, sub, origin=("—", ""), dest=("—", ""), model="—", speed="—",
             altitude="—", distance="—", photo=None, plane=False):
        """The one place that fills in every field on the main card. Anything you don't pass in gets a
        dash. Also updates the photo and makes sure the window's still tall enough."""
        v = self.vars
        v["flight"].set(flight); v["sub"].set(sub)
        v["origin"].set(origin[0]); v["origin_city"].set(origin[1])
        v["dest"].set(dest[0]); v["dest_city"].set(dest[1])
        v["model"].set(model); v["speed"].set(speed); v["altitude"].set(altitude); v["distance"].set(distance)
        self.cur_photo, self.has_plane = photo, plane
        self.cur_model = model if model not in ("—", "") else None
        self.apply_photo()
        self.after_idle(self.fit_height)

    def reset(self):
        """Back to the "nothing nearby" look: wipes every field and the photo, and turns off the links."""
        self.show("Watching the sky...", "")
        self.flight_url = None
        self.link_hover(False)
        self.model_hover(False)

    def mark_updated(self):
        """Notes that we just got fresh data, which resets the "Last updated" counter back to zero."""
        self.last_ok = time.monotonic()
        self.updated.set("Last updated 0 seconds ago")

    def clock(self):
        """Keeps the "Last updated X ago" line ticking along. Runs once a second, forever. Switches to
        minutes + seconds after the first minute."""
        if self.last_ok is not None:
            secs = int(time.monotonic() - self.last_ok)
            txt = f"{secs} second{'' if secs == 1 else 's'}" if secs < 60 else f"{secs // 60} min {secs % 60} s"
            self.updated.set(f"Last updated {txt} ago")
        self.after(1000, self.clock)

    # ---------- location ----------
    def save_config(self):
        """Saves the window size so it opens the same way next time. The "layout" number lets us ignore
        sizes saved by older versions whose layout was different. If saving fails, no big deal."""
        data = {"layout": 3, "w": self.winfo_width(), "h": self.winfo_height()}
        try:
            with open(CONFIG, "w") as f:
                json.dump(data, f)
        except OSError:
            pass

    def set_pos(self, lat, lon):
        """Stores our new location and immediately checks the sky around it."""
        self.pos = (lat, lon)
        self.refresh()

    def range_text(self, n):
        """The little status line, like "2 aircraft within 3 mi of you."."""
        return f"{n} aircraft within {RADIUS_MI} mi of you."

    def find_location(self):
        """Figures out where you are, in the background so the window doesn't freeze. Tries Windows
        Location Services first, and falls back to a rough guess from your IP address. Results get passed
        back through the queue for drain() to pick up."""
        self.status.set("Getting your location…")

        def work():
            """The actual location lookup, running on its own thread."""
            try:
                if not windows_location_stream(lambda lat, lon, acc: self.q.put(("pos", lat, lon))):
                    # Windows couldn't provide a position: fall back to the IP address (approximate)
                    j = get_json("https://ipwho.is/")
                    if not j.get("success"):
                        raise RuntimeError(j.get("message", "lookup failed"))
                    self.q.put(("pos", j["latitude"], j["longitude"]))
            except Exception as e:
                self.q.put(("locerr", f"Couldn't get your location ({e}). Retrying…"))
        threading.Thread(target=work, daemon=True).start()

    # ---------- cycling through aircraft ----------
    def set_next_visible(self, flag):
        """Shows or hides the "Show next" link. No point showing it when there's only one plane around.
        We also remember the choice so it survives a UI rebuild."""
        self.next_visible = flag
        if flag:
            self.next_lbl.pack(side="left", padx=(self.P(8), 0))
        else:
            self.next_lbl.pack_forget()

    def show_next(self, _event=None):
        """Jumps to the next aircraft in range, wrapping back around to the first one after the last.
        Shows whatever we already know about it instantly, then kicks off a poll to fill in the rest."""
        if len(self.order) < 2:
            return
        idx = self.order.index(self.sel_hex) if self.sel_hex in self.order else -1
        self.sel_hex = self.order[(idx + 1) % len(self.order)]
        r = next((x for x in self.inrange if x["a"]["hex"] == self.sel_hex), None)
        if r:  # show what we already have right away; the poll fills in route, model and photo
            a = r["a"]
            cs = (a.get("flight") or "").strip()
            self.render(dict(a=a, d=r["d"], dir=r["dir"], route=route_cache.get(cs),
                             ac=aircraft_cache.get(a["hex"]), photo=photo_cache.get(a["hex"])))
        if self.busy:
            self.again = True
        else:
            self.refresh()

    def show_tab(self, key):
        """Switches between the main tab and the Log tab, and highlights whichever one's active."""
        self.tab = key
        (self.page_main if key == "main" else self.page_log).tkraise()
        for k, lbl in self.tab_lbls.items():
            on = k == key
            lbl.config(fg=ACCENT if on else MUTED)
            self.tab_bars[k].config(bg=ACCENT if on else BG)

    def log_cols(self, frame):
        """Sets up the Log's three column widths. The header and the rows both use this, so they always
        line up with each other."""
        P = self.P
        frame.grid_columnconfigure(0, weight=1, minsize=P(130))
        frame.grid_columnconfigure(1, minsize=P(75))
        frame.grid_columnconfigure(2, minsize=P(180))

    def build_log_page(self):
        """Builds the Log tab: a dark-themed scrollbar, clickable column headers for sorting, and a
        scrollable list underneath (Tk doesn't have a scrollable frame, so it's a frame inside a canvas).
        The rows themselves get filled in by refresh_log()."""
        P, f = self.P, self.f
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Overhead.Vertical.TScrollbar", background=BTN, troughcolor=CARD, bordercolor=CARD,
                        lightcolor=BTN, darkcolor=BTN, arrowcolor=MUTED)

        label(self.page_log, text="Times in UTC  ·  each aircraft counts once per 24 hours", fg=MUTED,
              font=f(8), wraplength=P(400), justify="left").pack(side="bottom", anchor="w", padx=P(18), pady=(P(4), P(6)))
        card = tk.Frame(self.page_log, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True, padx=P(14), pady=P(4))
        bar = ttk.Scrollbar(card, orient="vertical", style="Overhead.Vertical.TScrollbar")
        bar.pack(side="right", fill="y")
        body = tk.Frame(card, bg=CARD)
        body.pack(side="left", fill="both", expand=True)

        # Clickable column headings (click to sort, click again to reverse)
        head = tk.Frame(body, bg=BTN)
        head.pack(fill="x")
        self.log_cols(head)
        self.log_heads = {}
        for i, (key, text, anchor) in enumerate((("model", "Model", "w"), ("count", "Sightings", "center"),
                                                 ("last", "Last sighted (UTC)", "w"))):
            h = tk.Label(head, text=text, bg=BTN, fg=MUTED, font=f(9, True), anchor=anchor,
                         padx=P(10), pady=P(6), cursor="hand2")
            h.grid(row=0, column=i, sticky="ew")
            h.bind("<Button-1>", lambda e, k=key: self.sort_by(k))
            h.bind("<Enter>", lambda e, w=h: w.config(bg=BTN_HOVER))
            h.bind("<Leave>", lambda e, w=h: w.config(bg=BTN))
            self.log_heads[key] = (h, text)

        # Scrollable list of rows
        canvas = tk.Canvas(body, bg=CARD, highlightthickness=0, bd=0, width=P(10), height=P(240),
                           yscrollincrement=P(26))
        canvas.pack(fill="both", expand=True)
        inner = tk.Frame(canvas, bg=CARD)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        bar.configure(command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        self.log_cols(inner)
        self.log_canvas, self.log_inner = canvas, inner
        self.log_font, self.log_font_ul = f(10), f(10, underline=True)
        self.log_empty = label(card, text="No sightings logged yet.", fg=MUTED, font=f(10))

    def sort_by(self, key):
        """Handles a click on a Log column header. Click a new column to sort by it, click the same one
        again to flip the order. Then jumps back to the top of the list."""
        if key == self.sort_key:
            self.sort_desc = not self.sort_desc       # same column again: reverse the order
        else:                                         # new column: Model A-Z, Sightings most, Last sighted newest
            self.sort_key, self.sort_desc = key, {"model": False, "count": True, "last": True}[key]
        self.refresh_log()
        self.log_canvas.yview_moveto(0)

    def sorted_log(self):
        """Hands back the log entries sorted by whichever column you picked. We sort newest-first before
        that, so anything that ties (say, two models with 3 sightings) shows the most recent one on top."""
        rows = sorted(self.log.values(), key=lambda e: e.get("last", 0), reverse=True)   # newest first breaks ties
        primary = {"model": lambda e: str(e["model"]).lower(),
                   "count": lambda e: e["count"],
                   "last": lambda e: e.get("last", 0)}[self.sort_key]
        rows.sort(key=primary, reverse=self.sort_desc)
        return rows

    def refresh_log(self):
        """Redraws the whole Log list from scratch: updates the sort arrows on the headers, then adds a
        row per model (with the model name linking to Wikipedia). Shows a "nothing yet" message if
        the log's empty."""
        P = self.P
        for key, (h, text) in self.log_heads.items():
            on = key == self.sort_key
            h.config(text=text + (("  ▼" if self.sort_desc else "  ▲") if on else ""), fg=ACCENT if on else MUTED)
        for w in self.log_inner.winfo_children():
            w.destroy()
        rows = self.sorted_log()
        if rows:
            self.log_empty.place_forget()
        else:
            self.log_empty.place(relx=0.5, rely=0.45, anchor="center")
        for i, e in enumerate(rows):
            link = tk.Label(self.log_inner, text=e["model"], bg=CARD, fg=ACCENT, font=self.log_font, anchor="w",
                            justify="left", wraplength=P(165), padx=P(10), pady=P(5), cursor="hand2")
            link.grid(row=2 * i, column=0, sticky="ew")
            link.bind("<Enter>", lambda ev, w=link: w.config(font=self.log_font_ul))
            link.bind("<Leave>", lambda ev, w=link: w.config(font=self.log_font))
            link.bind("<Button-1>", lambda ev, m=e["model"]: self.open_wikipedia(m))
            tk.Label(self.log_inner, text=str(e["count"]), bg=CARD, fg=TX, font=self.log_font, anchor="center",
                     padx=P(10), pady=P(5)).grid(row=2 * i, column=1, sticky="ew")
            tk.Label(self.log_inner, text=fmt_utc(e["last"]), bg=CARD, fg=TX, font=self.log_font, anchor="w",
                     padx=P(10), pady=P(5)).grid(row=2 * i, column=2, sticky="ew")
            tk.Frame(self.log_inner, bg=BORDER, height=1).grid(row=2 * i + 1, column=0, columnspan=3, sticky="ew")

    def open_wikipedia(self, model):
        """Opens the model's Wikipedia page in your browser. Finding the right page takes a network call,
        so we do it on a background thread to keep the window responsive."""
        threading.Thread(target=lambda: webbrowser.open(wikipedia_url(model)), daemon=True).start()

    def _wheel(self, e):
        """Lets the mouse wheel scroll the Log list, but only when your pointer is actually over it.
        We walk up from whatever widget is under the mouse to check if it's inside the list."""
        if self.tab != "log":
            return
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            if w is self.log_canvas:
                self.log_canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")
                return
            w = w.master

    def record_sightings(self, infos):
        """Adds to the Log when a plane shows up. Each aircraft only counts once; it can count again only
        if it's been gone for 24 hours. Also forgets planes we haven't seen in over a day, and saves to disk
        when something new was added (or at least once a minute, to keep the "last seen" times current)."""
        now, added = time.time(), False
        for hex_id, key, model in infos:
            last = self.seen.get(hex_id)
            if last is None or now - last > SIGHTING_GAP_S:
                e = self.log.setdefault(key, {"model": model, "count": 0, "last": 0})
                e["model"], e["count"], e["last"] = model, e["count"] + 1, int(now)
                added = True
            self.seen[hex_id] = now
        for h in [h for h, t in self.seen.items() if now - t > SIGHTING_GAP_S]:
            del self.seen[h]
        if added:
            self.refresh_log()
        if added or time.monotonic() - self._saved_at > 60:
            self.save_log()

    def save_log(self):
        """Writes the sightings log to disk. If it fails, we'll just get it next time."""
        self._saved_at = time.monotonic()
        try:
            with open(LOG_PATH, "w") as f:
                json.dump({"models": self.log, "seen": self.seen}, f)
        except OSError:
            pass

    def link_hover(self, on):
        """Underlines the flight number while you hover over it, so it feels like a link. Only does it
        when there's actually somewhere to go."""
        if hasattr(self, "flight_lbl"):
            self.flight_lbl.config(font=self.flight_font_ul if on and self.flight_url else self.flight_font)

    def model_hover(self, on):
        """Same idea as link_hover, but for the model name (which links to Wikipedia)."""
        if hasattr(self, "model_lbl"):
            self.model_lbl.config(font=self.model_font_ul if on and self.cur_model else self.model_font)

    def open_flight(self, _event=None):
        """Opens the current flight on FlightAware when you click the flight number. If there's no plane
        showing, clicking does nothing."""
        if self.flight_url:
            webbrowser.open(self.flight_url)

    def finish_poll(self):
        """Marks the current poll as done. If someone asked for another one while it was running (like
        hitting "Show next"), fire that off now."""
        self.busy = False
        if self.again:
            self.again = False
            self.refresh()

    # ---------- polling ----------
    def tick_loop(self):
        """The heartbeat: checks the sky every POLL_MS (10 seconds), forever."""
        self.refresh()
        self.after(POLL_MS, self.tick_loop)

    def refresh(self):
        """Starts a poll on a background thread, as long as we know where we are and one isn't already
        running (no need to pile them up)."""
        if self.pos and not self.busy:
            self.busy = True
            threading.Thread(target=self.work, args=self.pos, daemon=True).start()

    def work(self, lat, lon):
        """The actual poll, running on a background thread. Grabs nearby aircraft, keeps the ones within
        range, picks which one to show, looks up its route, details and photo, and gathers model info
        for the Log. Everything gets sent back through the queue, since only the main thread is allowed
        to touch the UI."""
        try:
            found = []
            for a in nearby(lat, lon):
                if isinstance(a.get("lat"), (int, float)) and isinstance(a.get("lon"), (int, float)):
                    d = dist_mi(lat, lon, a["lat"], a["lon"])
                    if d <= RADIUS_MI:
                        found.append((d, a))
            found.sort(key=lambda x: x[0])
            if not found:
                self.q.put(("none", 0))
                return
            # Keep showing the aircraft you picked while it's in range; otherwise the nearest one
            want = self.sel_hex
            d, a = next(((dd, aa) for dd, aa in found if aa["hex"] == want), found[0])
            cs = (a.get("flight") or "").strip()
            ac_sel = lookup_aircraft(a["hex"])
            listing = [dict(a=x, d=dd, dir=direction(lat, lon, x["lat"], x["lon"])) for dd, x in found]
            infos = []  # (hex, model key, model name) for every aircraft in range, used by the Log tab
            for _dd, x in found:
                ac_x = None if x.get("desc") else lookup_aircraft(x["hex"])
                name = model_text(x, ac_x)
                if name != "—":
                    infos.append((x["hex"], x.get("t") or (ac_x or {}).get("icao_type") or name, name))
            self.q.put(("ok", len(found), dict(
                a=a, d=d, route=lookup_route(cs), ac=ac_sel, photo=lookup_photo(a["hex"], model_text(a, ac_sel)),
                dir=direction(lat, lon, a["lat"], a["lon"])), listing, infos))
        except Exception as e:
            self.q.put(("err", f"Couldn't reach the flight data feed: {e}"))

    def drain(self):
        """Checks the queue every 200 ms for messages from the background threads and acts on them:
        "pos" is a new location, "locerr" means location failed (try again in 15 s), "err" means the
        flight feed failed, "none" means the sky's empty, and "ok" means we've got a plane to show."""
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "pos":
                    self.set_pos(item[1], item[2])
                elif kind == "locerr":
                    self.status.set(item[1])
                    self.after(15000, self.find_location)
                elif kind == "err":
                    self.status.set(item[1])
                    self.finish_poll()
                elif kind == "none":
                    self.inrange, self.order, self.sel_hex = [], [], None
                    self.reset()
                    self.mark_updated()
                    self.status.set(self.range_text(0))
                    self.set_next_visible(False)
                    self.finish_poll()
                elif kind == "ok":
                    self.inrange = item[3]
                    self.record_sightings(item[4])
                    present = [x["a"]["hex"] for x in self.inrange]
                    # stable cycle order: keep existing order, append newcomers nearest-first
                    self.order = [h for h in self.order if h in present] + [h for h in present if h not in self.order]
                    self.sel_hex = item[2]["a"]["hex"]
                    self.render(item[2])
                    self.mark_updated()
                    self.status.set(self.range_text(item[1]))
                    self.set_next_visible(item[1] > 1)
                    self.finish_poll()
        except queue.Empty:
            pass
        self.after(200, self.drain)

    def render(self, r):
        """Takes everything we know about one aircraft and turns it into what you see on the card:
        callsign, airline and registration, route, model, speed (mph and knots), altitude and distance.
        Also sets up the FlightAware link, using the callsign if there is one, or the hex ID if not."""
        a, route, ac = r["a"], r["route"], r["ac"]
        cs = (a.get("flight") or "").strip() or a["hex"].upper()
        airline = ((route or {}).get("airline") or {}).get("name")
        reg = a.get("r") or (ac or {}).get("registration")
        gs = a.get("gs")
        speed = f"{round(gs * 1.15078)} mph ({round(gs)} kt)" if isinstance(gs, (int, float)) else "—"
        self.show(cs, " · ".join(x for x in (airline, reg) if x) or "Unidentified flight",
                  place((route or {}).get("origin")), place((route or {}).get("destination")),
                  model_text(a, ac), speed, altitude_text(a.get("alt_baro")),
                  f"{r['d']:.1f} mi {r['dir']}", r["photo"], plane=True)
        raw = (a.get("flight") or "").strip()
        self.flight_url = (f"https://www.flightaware.com/live/flight/{quote(raw)}" if raw
                           else f"https://www.flightaware.com/live/modes/{a['hex']}/redirect")


if __name__ == "__main__":
    # DPI awareness has to be switched on before the window exists, or it won't take
    enable_dpi_awareness()
    App().mainloop()
