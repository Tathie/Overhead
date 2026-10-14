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

route_cache, photo_cache, aircraft_cache = {}, {}, {}


def enable_dpi_awareness():
    """Stops Windows from bitmap-stretching the window (the cause of blurry text)."""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def load_config():
    for path in (CONFIG, OLD_CONFIG):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def load_log():
    """Returns (models, seen): per-model sighting counts, and when each aircraft was last in range."""
    try:
        with open(LOG_PATH) as f:
            d = json.load(f)
        return dict(d.get("models", {})), dict(d.get("seen", {}))
    except Exception:
        return {}, {}


def fmt_utc(epoch):
    """MM-DD-YYYY HH:MM (12-hour clock) in UTC. For a 24-hour clock change %I:%M %p to %H:%M."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%m-%d-%Y %I:%M %p")


def get_bytes(url):
    req = Request(url, headers={"User-Agent": "Overhead/1.0"})
    with urlopen(req, timeout=10) as r:
        return r.read()


def get_json(url):
    return json.loads(get_bytes(url).decode("utf-8"))


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
    """Asks Windows Location Services (Wi-Fi positioning when an adapter is available) for a position.
    Calls on_fix(lat, lon, accuracy_m) each time the fix improves, and stops once it is accurate
    enough or has settled. Returns True if at least one fix arrived."""
    if os.name != "nt":
        return False
    got, proc, watchdog = False, None, None
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", LOCATION_SCRIPT],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            text=True, creationflags=0x08000000)  # CREATE_NO_WINDOW: no console flash
        watchdog = threading.Timer(60, proc.kill)
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


def dist_mi(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl, dn = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dl / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dn / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def direction(lat1, lon1, lat2, lon2):
    p1, p2, dn = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dn) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dn)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][round(deg / 45) % 8]


def nearby(lat, lon):
    nm = math.ceil(RADIUS_MI / 1.15078) + 1   # feed radius is in nautical miles; add a little margin
    try:
        return get_json(f"https://api.airplanes.live/v2/point/{lat}/{lon}/{nm}").get("ac") or []
    except Exception:
        return get_json(f"https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{nm}").get("aircraft") or []


def lookup_route(callsign):
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
    """Manufacturer / type details for this airframe (adsbdb)."""
    if hex_id not in aircraft_cache:
        try:
            resp = get_json(f"https://api.adsbdb.com/v0/aircraft/{hex_id}").get("response")
            aircraft_cache[hex_id] = resp.get("aircraft") if isinstance(resp, dict) else None
        except Exception:
            return None  # transient failure; try again on the next poll
    return aircraft_cache[hex_id]


def fetch_image(url):
    img = Image.open(io.BytesIO(get_bytes(url))).convert("RGB")
    img.thumbnail((1200, 800))  # keep native detail; it's resized to fit the window later
    return img


def lookup_photo(hex_id):
    """Returns (PIL image, credit text) for this exact airframe, or None.
    Uses the larger of the available sources so it stays sharp on high-res screens."""
    if Image is None:
        return None
    if hex_id in photo_cache:
        return photo_cache[hex_id]
    best, errored = None, False

    def consider(url, credit):
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
    if best or not errored:  # don't permanently cache a network hiccup
        photo_cache[hex_id] = best
    return best


def model_text(a, ac):
    if a.get("desc"):
        return str(a["desc"]).title()
    if ac:
        name = " ".join(x for x in (ac.get("manufacturer"), ac.get("type")) if x).strip()
        if name:
            return name
    return a.get("t") or "—"


def altitude_text(alt):
    if alt == "ground":
        return "On ground"
    if isinstance(alt, (int, float)):
        return f"{round(alt):,} ft"
    return "—"


def place(p):
    if not p:
        return "—", "Unknown"
    return p.get("iata_code") or p.get("icao_code") or "—", p.get("municipality") or p.get("name") or ""


def label(parent, **kw):
    kw.setdefault("bg", parent["bg"])
    kw.setdefault("fg", TX)
    return tk.Label(parent, **kw)


class App(tk.Tk):
    def __init__(self):
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
        self.flight_url = None
        self.tab, self._saved_at = "main", 0.0
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
        self.after(300, self.become_ready)

        self.after(100, self.dark_titlebar)
        self.find_location()
        self.after(200, self.drain)
        self.after(POLL_MS, self.tick_loop)
        self.after(1000, self.clock)

    # ---------- sizing ----------
    def P(self, n):
        return int(n * self.s * self.zoom)

    def f(self, size, bold=False, underline=False):
        font = tkfont.Font(family="Segoe UI", size=max(6, round(size * self.zoom)),
                           weight="bold" if bold else "normal", underline=underline)
        self._fonts.append(font)  # keep a reference so Tk doesn't discard it
        return font

    def zoom_for(self, width):
        return round(min(ZOOM_MAX, max(ZOOM_MIN, width / self.base_w)), 2)

    def become_ready(self):
        self.ready = True
        self.auto_h = None if self.saved else self.winfo_height()
        self.fit_height()

    def on_configure(self, e):
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
        self._resize_job = None
        z = self.zoom_for(self.winfo_width())
        if z != self.zoom:
            self.zoom = z
            self.rebuild()

    def fit_height(self):
        """Keep the window tall enough for its content, and shrink-wrap it unless you set the height yourself."""
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
        for w in self.winfo_children():
            w.destroy()
        self._fonts = []
        self.build()
        self.apply_photo()
        self.fit_height()

    def dark_titlebar(self):
        try:  # Windows 10/11 only; silently skipped elsewhere
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            for attr in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(1)), 4)
        except Exception:
            pass

    # ---------- UI ----------
    def build(self):
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
        self.photo_box.pack(side="right", anchor="n", padx=(gap, 0))
        self.photo = label(self.photo_box)
        self.credit_lbl = label(self.photo_box, textvariable=self.credit, fg=MUTED, font=f(8),
                                wraplength=photo_w, justify="left")

        info = tk.Frame(top, bg=CARD)
        info.pack(side="left", fill="both", expand=True, anchor="n")
        flight_lbl = label(info, textvariable=self.vars["flight"], font=f(28, True), wraplength=inner, justify="left")
        flight_lbl.pack(anchor="w")
        flight_lbl.bind("<Button-1>", self.open_flight)
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
        for text, key in (("MODEL", "model"), ("SPEED", "speed"), ("ALTITUDE", "altitude"), ("DISTANCE", "distance")):
            row = tk.Frame(card, bg=CARD)
            row.pack(fill="x", pady=(P(6), 0), **pad)
            label(row, text=text, fg=MUTED, font=f(8)).pack(anchor="w")
            label(row, textvariable=self.vars[key], font=f(14, True), wraplength=inner,
                  justify="left").pack(anchor="w")

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
        """Resize with a high-quality filter to fit the card at the current zoom."""
        max_w, max_h = self.P(190), self.P(130)
        r = min(max_w / img.width, max_h / img.height)
        return img.resize((max(1, int(img.width * r)), max(1, int(img.height * r))), LANCZOS)

    def apply_photo(self):
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

    def show(self, flight, sub, origin=("—", ""), dest=("—", ""), model="—", speed="—",
             altitude="—", distance="—", photo=None, plane=False):
        v = self.vars
        v["flight"].set(flight); v["sub"].set(sub)
        v["origin"].set(origin[0]); v["origin_city"].set(origin[1])
        v["dest"].set(dest[0]); v["dest_city"].set(dest[1])
        v["model"].set(model); v["speed"].set(speed); v["altitude"].set(altitude); v["distance"].set(distance)
        self.cur_photo, self.has_plane = photo, plane
        self.apply_photo()
        self.after_idle(self.fit_height)

    def reset(self):
        """Default 'nothing in range' state: clears every field and the photo."""
        self.show("Watching the sky...", "")
        self.flight_url = None

    def mark_updated(self):
        self.last_ok = time.monotonic()
        self.updated.set("Last updated 0 seconds ago")

    def clock(self):
        """Live 'last updated' counter, ticks once a second."""
        if self.last_ok is not None:
            secs = int(time.monotonic() - self.last_ok)
            txt = f"{secs} second{'' if secs == 1 else 's'}" if secs < 60 else f"{secs // 60} min {secs % 60} s"
            self.updated.set(f"Last updated {txt} ago")
        self.after(1000, self.clock)

    # ---------- location ----------
    def save_config(self):
        data = {"layout": 3, "w": self.winfo_width(), "h": self.winfo_height()}
        try:
            with open(CONFIG, "w") as f:
                json.dump(data, f)
        except OSError:
            pass

    def set_pos(self, lat, lon):
        self.pos = (lat, lon)
        self.refresh()

    def range_text(self, n):
        return f"{n} aircraft within {RADIUS_MI} mi of you."

    def find_location(self):
        self.status.set("Getting your location…")

        def work():
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
        self.next_visible = flag
        if flag:
            self.next_lbl.pack(side="left", padx=(self.P(8), 0))
        else:
            self.next_lbl.pack_forget()

    def show_next(self, _event=None):
        """Advance to the next aircraft in range, looping back to the first after the last."""
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
        self.tab = key
        (self.page_main if key == "main" else self.page_log).tkraise()
        for k, lbl in self.tab_lbls.items():
            on = k == key
            lbl.config(fg=ACCENT if on else MUTED)
            self.tab_bars[k].config(bg=ACCENT if on else BG)

    def build_log_page(self):
        P, f = self.P, self.f
        font = lambda n, bold=False: ("Segoe UI", max(6, round(n * self.zoom))) + (("bold",) if bold else ())
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Overhead.Treeview", background=CARD, fieldbackground=CARD, foreground=TX,
                        borderwidth=0, rowheight=P(26), font=font(10))
        style.layout("Overhead.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.map("Overhead.Treeview", background=[("selected", CARD)], foreground=[("selected", TX)])
        style.configure("Overhead.Treeview.Heading", background=BTN, foreground=MUTED, relief="flat",
                        borderwidth=0, font=font(9, True))
        style.map("Overhead.Treeview.Heading", background=[("active", BTN_HOVER)])
        style.configure("Overhead.Vertical.TScrollbar", background=BTN, troughcolor=CARD, bordercolor=CARD,
                        lightcolor=BTN, darkcolor=BTN, arrowcolor=MUTED)

        label(self.page_log, text="Times in UTC  ·  each aircraft counts once per 24 hours", fg=MUTED,
              font=f(8), wraplength=P(400), justify="left").pack(side="bottom", anchor="w", padx=P(18), pady=(P(4), P(6)))
        card = tk.Frame(self.page_log, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True, padx=P(14), pady=P(4))
        tree = ttk.Treeview(card, columns=("model", "count", "last"), show="headings",
                            style="Overhead.Treeview", selectmode="none", height=12)
        tree.heading("model", text="Model", anchor="w")
        tree.heading("count", text="Sightings", anchor="center")
        tree.heading("last", text="Last sighted (UTC)", anchor="w")
        tree.column("model", width=P(150), minwidth=P(100), anchor="w", stretch=True)
        tree.column("count", width=P(75), minwidth=P(60), anchor="center", stretch=False)
        tree.column("last", width=P(160), minwidth=P(120), anchor="w", stretch=False)
        bar = ttk.Scrollbar(card, orient="vertical", command=tree.yview, style="Overhead.Vertical.TScrollbar")
        tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        self.tree = tree
        self.log_empty = label(card, text="No sightings logged yet.", fg=MUTED, font=f(10))

    def refresh_log(self):
        self.tree.delete(*self.tree.get_children())
        rows = sorted(self.log.values(), key=lambda e: e.get("last", 0), reverse=True)
        for e in rows:
            self.tree.insert("", "end", values=(e["model"], e["count"], fmt_utc(e["last"])))
        if rows:
            self.log_empty.place_forget()
        else:
            self.log_empty.place(relx=0.5, rely=0.45, anchor="center")

    def record_sightings(self, infos):
        """Count a sighting the first time an aircraft is in range (again only after 24 hours away)."""
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
        self._saved_at = time.monotonic()
        try:
            with open(LOG_PATH, "w") as f:
                json.dump({"models": self.log, "seen": self.seen}, f)
        except OSError:
            pass

    def open_flight(self, _event=None):
        """Open the displayed flight on FlightAware (does nothing when no aircraft is shown)."""
        if self.flight_url:
            webbrowser.open(self.flight_url)

    def finish_poll(self):
        self.busy = False
        if self.again:
            self.again = False
            self.refresh()

    # ---------- polling ----------
    def tick_loop(self):
        self.refresh()
        self.after(POLL_MS, self.tick_loop)

    def refresh(self):
        if self.pos and not self.busy:
            self.busy = True
            threading.Thread(target=self.work, args=self.pos, daemon=True).start()

    def work(self, lat, lon):
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
            listing = [dict(a=x, d=dd, dir=direction(lat, lon, x["lat"], x["lon"])) for dd, x in found]
            infos = []  # (hex, model key, model name) for every aircraft in range, used by the Log tab
            for _dd, x in found:
                ac_x = None if x.get("desc") else lookup_aircraft(x["hex"])
                name = model_text(x, ac_x)
                if name != "—":
                    infos.append((x["hex"], x.get("t") or (ac_x or {}).get("icao_type") or name, name))
            self.q.put(("ok", len(found), dict(
                a=a, d=d, route=lookup_route(cs), ac=lookup_aircraft(a["hex"]), photo=lookup_photo(a["hex"]),
                dir=direction(lat, lon, a["lat"], a["lon"])), listing, infos))
        except Exception as e:
            self.q.put(("err", f"Couldn't reach the flight data feed: {e}"))

    def drain(self):
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
    enable_dpi_awareness()
    App().mainloop()
