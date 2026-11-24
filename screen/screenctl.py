#!/usr/bin/env python3
"""screenctl: web remote, settings and display control for the homelab.

Runs at boot as a user service, with or without a screen. Without one (headless box) you still get
the remote page, settings, volume and the dashboard in any browser (/dashboard). When the sway
kiosk session is running it also drives the built-in display:

Nothing reloads when switching modes; each mode lives on its own sway workspace:
  ws 1  kiosk-shell    Chromium (CDP :9222) with /shell = dashboard + fireplace <video>
  ws 2  kiosk-youtube  Chromium (CDP :9223) with YouTube TV, always loaded (cast target)
  ws 3  overlays       mpv (non-YouTube links), UxPlay (AirPlay) - sway moves them here
Switching = `swaymsg workspace N` (+ a JS call in the shell). When an overlay closes
we go back to the current mode's workspace. If someone casts to YouTube while
another mode is shown, we switch to YouTube automatically.

HTTP API on :8090 (LAN + Tailscale only, via ufw):
  GET  /  control page        GET /shell  kiosk page     GET /api/stats
  POST /api/mode/<dashboard|fireplace|youtube|spotify>
  POST /api/play  url=...     YouTube links -> YouTube TV, others -> mpv
  POST /api/stop              stop mpv / pause YouTube
  POST /api/key/<up|down|left|right|select|back|playpause|home>
  GET  /api/spotify           now playing in the Spotify web player (ws 4, Connect "Web Player (Chrome)")
  POST /api/spotify/<playpause|next|prev>
  POST /api/mode/spotify      show the web player (to log in once with the laptop keyboard)
  GET  /api/claude            latest Claude Code session transcript (for the dashboard)
  POST /api/screen/<on|off>
  POST /api/page/<id|auto>    lock the dashboard on a page (1 overview, 2 calendar, or a built page id), auto = rotate
  GET  /api/pages             page list: the two built-in pages + pages from the builder directory
  GET  /api/pages/<id>        data of a built page (its source, cached for source.every seconds)
  GET  /api/volume            {volume 0..1, muted, sink} of the default audio output (PipeWire)
  POST /api/volume            value=0..1 or step=+-0.05;   POST /api/volume/mute  toggles mute
  GET  /api/config            dashboard settings (no secrets)
  POST /api/push/<source>     snapshot of calendars / reminders / notes from a client (X-Push-Token)
  GET  /api/agenda            that snapshot, filtered by the dashboard settings (page 2)
  GET  /settings              settings page
  GET|POST /api/settings      read / change config/settings.json and config/homelab.env (X-Settings-Token)
  POST /api/settings/apply    bin/homelab apply: re-render configs, restart what changed (X-Settings-Token)
"""
import hmac, json, os, re, shutil, socket, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import websocket  # python3-websocket, only needed to drive the kiosk browsers
except ImportError:
    websocket = None

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import hlconfig  # noqa: E402
MEDIA = Path("/srv/media")
SHELL, YT, SPOT = 9222, 9223, 9224
WS = {"dashboard": 1, "fireplace": 1, "youtube": 2, "spotify": 4}
STATE = {"mode": "dashboard", "playing": None, "screen": "on", "page": "auto"}   # page: auto | 1 | 2 | built page id
# where to go back to when a cast / shared video ends (None = user chose YouTube, stay there)
RETURN = {"to": None, "seen_watch": False, "idle_since": None}
LOCK = threading.RLock()
MPV_SOCK = "/tmp/screenctl-mpv.sock"
_mpv = None


# ---------- Chromium via CDP ----------
def cdp(port, *calls):
    """Run (method, params) pairs on the page of the Chromium at `port`; return last result."""
    if websocket is None:
        raise RuntimeError("python3-websocket is not installed")
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3))
    pages = [t for t in tabs if t.get("type") == "page"]
    # Chrome may add its own UI pages (chrome://signin-..., new tab); use the real web page
    page = next((t for t in pages if t.get("url", "").startswith(("http://", "https://"))), pages[0])
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=5)
    result = None
    try:
        for i, (method, params) in enumerate(calls, 1):
            ws.send(json.dumps({"id": i, "method": method, "params": params}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == i:
                    result = msg.get("result")
                    break
    finally:
        ws.close()
    return result


def js(port, expr):
    r = cdp(port, ("Runtime.evaluate", {"expression": expr, "returnByValue": True}))
    return (r or {}).get("result", {}).get("value")


def yt_url():
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{YT}/json", timeout=3))
    return next((t["url"] for t in tabs if t.get("type") == "page" and t.get("url", "").startswith("http")), "")


YT_PLAYING = "(()=>{const v=document.querySelector('video');return !!(v&&!v.paused&&!v.ended&&v.currentTime>0)})()"
YT_PAUSE = "(()=>{const v=document.querySelector('video');if(v&&!v.paused)v.pause()})()"


# ---------- sway ----------
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def display():
    """True when the sway kiosk session runs. We start at boot, maybe before sway, so look up its
    sockets each time instead of trusting the environment we were started with."""
    socks = sorted(RUNTIME.glob("sway-ipc.*.sock"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not socks:
        return False
    os.environ["SWAYSOCK"] = str(socks[0])
    wl = sorted(p.name for p in RUNTIME.glob("wayland-[0-9]") if p.is_socket())
    if wl:
        os.environ["WAYLAND_DISPLAY"] = wl[0]
    return True


def sway(cmd):
    if display():
        subprocess.run(["swaymsg", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def overlay_open():
    return _mpv is not None and _mpv.poll() is None


# ---------- mpv (non-YouTube links) ----------
def mpv_stop():
    global _mpv
    if overlay_open():
        _mpv.terminate()
        try:
            _mpv.wait(3)
        except subprocess.TimeoutExpired:
            _mpv.kill()
    _mpv = None
    STATE["playing"] = None


def mpv_play(url):
    global _mpv
    if not display():
        raise RuntimeError("no screen: the sway kiosk session is not running")
    mpv_stop()
    _mpv = subprocess.Popen(
        ["mpv", "--fs", "--no-terminal", "--force-window=immediate", "--input-ipc-server=" + MPV_SOCK,
         "--ytdl-format=bestvideo[height<=1080]+bestaudio/best[height<=1080]",
         "--script-opts=ytdl_hook-ytdl_path=" + (shutil.which("yt-dlp") or "yt-dlp"), url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    STATE["playing"] = url


def mpv_cmd(command):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(MPV_SOCK)
        s.sendall((json.dumps({"command": command}) + "\n").encode())


# ---------- modes ----------
def set_mode(mode, auto=False):
    """auto=True: entered because of a cast / shared link, so return when the video ends."""
    if mode not in WS:
        raise ValueError("unknown mode")
    with LOCK:
        if mode == "youtube":
            prev = STATE["mode"] if STATE["mode"] != "youtube" else (RETURN["to"] or None)
            RETURN.update(to=prev if auto else None, seen_watch=False, idle_since=None)
        else:
            RETURN["to"] = None
        mpv_stop()
        if mode != "youtube":
            try:
                js(YT, YT_PAUSE)  # otherwise auto-cast would switch straight back
            except Exception:
                pass
        js(SHELL, f"setMode({json.dumps(mode if mode in ('dashboard', 'fireplace') else 'dashboard')})")
        sway(f"workspace number {WS[mode]}")
        STATE["mode"] = mode
        if STATE["screen"] == "off":
            screen(True)


YT_ID = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/))([\w-]{11})")


def play(url):
    m = YT_ID.search(url)
    with LOCK:
        if STATE["screen"] == "off":
            screen(True)
        if m:
            cdp(YT, ("Page.navigate", {"url": "https://www.youtube.com/tv#/watch?v=" + m.group(1)}))
            set_mode("youtube", auto=True)
            STATE["playing"] = url
        else:
            mpv_play(url)  # sway moves it to workspace 3


def set_page(page):
    """Lock the dashboard on a page, or let it rotate. Browsers showing /dashboard follow via /api/stats."""
    if page != "auto" and page not in page_ids():
        raise ValueError("page must be auto, 1, 2 or the id of a built page")
    with LOCK:
        STATE["page"] = page
        try:
            if STATE["mode"] != "dashboard":
                set_mode("dashboard")
            js(SHELL, f"setPage({json.dumps(page)})")   # the kiosk at once; others within 3 s
        except Exception:
            pass


def stop():
    with LOCK:
        mpv_stop()
        try:
            js(YT, YT_PAUSE)
        except Exception:
            pass
        sway(f"workspace number {WS[STATE['mode']]}")


def screen(on):
    sway("output * power " + ("on" if on else "off"))
    STATE["screen"] = "on" if on else "off"


# ---------- remote keys ----------
KEYS = {
    "up": ("ArrowUp", 38), "down": ("ArrowDown", 40), "left": ("ArrowLeft", 37),
    "right": ("ArrowRight", 39), "select": ("Enter", 13), "back": ("Escape", 27),
    "playpause": ("MediaPlayPause", 179), "home": ("", 0),
}
MPV_KEYS = {"home": ["quit"], "playpause": ["cycle", "pause"], "select": ["cycle", "pause"], "left": ["seek", -10],
            "right": ["seek", 10], "back": ["quit"], "up": ["add", "volume", 10], "down": ["add", "volume", -10]}


def press_yt(key):
    k, vk = KEYS[key]
    base = {"key": k, "code": k, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
    cdp(YT, ("Input.dispatchKeyEvent", dict(base, type="rawKeyDown")),
        ("Input.dispatchKeyEvent", dict(base, type="keyUp")))


def yt_at_root():
    """YouTube TV home/browse screen (not a video, search or settings)."""
    frag = yt_url().partition("#")[2]
    return frag in ("", "/") or frag.startswith("/browse")


def press(key):
    if key == "home":                      # TV / info button: always back to the dashboard
        return set_mode("dashboard")
    if overlay_open():
        if key == "back":
            STATE["playing"] = None
        return mpv_cmd(MPV_KEYS.get(key, ["cycle", "pause"]))
    if key == "playpause" and STATE["mode"] in ("dashboard", "fireplace") and spotify_state().get("active"):
        return spotify_control("playpause")   # Apple TV remote play/pause -> Spotify when it's what plays
    if STATE["mode"] == "youtube":
        if key == "back" and yt_at_root():
            return set_mode("dashboard")
        return press_yt(key)
    js(SHELL, f"remote({json.dumps(key)})")   # dashboard / fireplace: the dock handles focus


# ---------- background watchers ----------
def tree_windows():
    if not display():
        return []
    out = subprocess.run(["swaymsg", "-r", "-t", "get_tree"], capture_output=True, text=True).stdout
    found = []

    def walk(n):
        if n.get("pid") and n.get("type") in ("con", "floating_con"):
            found.append(n)
        for c in n.get("nodes", []) + n.get("floating_nodes", []):
            walk(c)
    walk(json.loads(out or "{}"))
    return found


def place(con):
    """Kiosk windows to their own workspace, anything else is an overlay on ws 3."""
    cid, app = con["id"], con.get("app_id") or ""
    target = {"kiosk-shell": 1, "kiosk-youtube": 2, "kiosk-spotify": 4}.get(app, 3)
    sway(f"[con_id={cid}] move container to workspace number {target}")
    sway(f"[con_id={cid}] fullscreen enable")
    sway(f"workspace number {3 if target == 3 else WS[STATE['mode']]}")


def watch_overlays():
    """Place new windows; when an overlay (mpv, AirPlay) closes, return to the current mode."""
    for con in tree_windows():
        place(con)
    while True:
        if not display():
            time.sleep(5)
            continue
        p = subprocess.Popen(["swaymsg", "-m", "-r", "-t", "subscribe", '["window"]'],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            con = ev.get("container") or {}
            app = con.get("app_id") or ""
            if ev.get("change") == "new":
                time.sleep(0.2)  # let Chromium set its app_id
                fresh = next((w for w in tree_windows() if w["id"] == con.get("id")), con)
                place(fresh)
            elif ev.get("change") == "close" and not app.startswith("kiosk-"):
                if not overlay_open():
                    STATE["playing"] = None
                sway(f"workspace number {WS[STATE['mode']]}")
        time.sleep(3)


def reconcile():
    """Safety net for windows that appeared before we subscribed to sway events."""
    if not display():
        return
    for app, ws in (("kiosk-shell", 1), ("kiosk-youtube", 2), ("kiosk-spotify", 4)):
        out = subprocess.run(["swaymsg", f'[app_id="^{app}$" workspace="^(?!{ws}$)"] '
                              f"move container to workspace number {ws}, fullscreen enable"],
                             capture_output=True, text=True).stdout
        if '"success": true' in out:
            sway(f"workspace number {3 if overlay_open() else WS[STATE['mode']]}")
    sway('[app_id="^kiosk-"] fullscreen enable')  # idempotent; keeps the Chromium toolbar hidden
    # YouTube TV asks "who's watching?" after every start: pick the first (focused) profile
    try:
        if js(YT, "document.body.innerText.includes('Watch as guest')"):
            press_yt("select")
    except Exception:
        pass


def watch_cast():
    """Someone cast a video to YouTube TV while another mode is on screen: show it."""
    while True:
        time.sleep(2)
        reconcile()
        try:
            if STATE["mode"] != "youtube" and not overlay_open() and js(YT, YT_PLAYING):
                set_mode("youtube", auto=True)
            elif STATE["mode"] == "youtube" and RETURN["to"]:
                watching = "/watch" in yt_url()
                if watching:
                    RETURN.update(seen_watch=True, idle_since=None)
                elif RETURN["seen_watch"]:
                    # cast disconnected / video closed: YouTube went back to its home screen
                    RETURN["idle_since"] = RETURN["idle_since"] or time.time()
                    if time.time() - RETURN["idle_since"] > 3:
                        set_mode(RETURN["to"])
                        STATE["playing"] = None
        except Exception:
            pass


# ---------- slow probes (refreshed in a thread, read by /api/stats) ----------
PROBE = {"ping": None, "updates": None, "site": None, "tailscale": [], "lan_devices": None, "bluetooth": []}
_net = {"t": None, "rx": 0, "tx": 0, "rate": None}


def probe_loop():
    n = 0
    while True:
        try:
            out = subprocess.run(["ping", "-c1", "-W2", "1.1.1.1"], capture_output=True, text=True).stdout
            m = re.search(r"time=([\d.]+)", out)
            PROBE["ping"] = float(m.group(1)) if m else None
        except Exception:
            PROBE["ping"] = None
        try:
            ts = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5).stdout)
            PROBE["tailscale"] = [{"name": p.get("HostName"), "online": bool(p.get("Online")), "os": p.get("OS")}
                                  for p in (ts.get("Peer") or {}).values()]
        except Exception:
            pass
        try:
            iface = hlconfig.read_env().get("HL_LAN_IFACE", "")
            neigh = subprocess.run(["ip", "neigh", "show"] + (["dev", iface] if iface else []),
                                   capture_output=True, text=True).stdout
            PROBE["lan_devices"] = sum(1 for l in neigh.splitlines() if re.search(r"REACHABLE|STALE|DELAY", l))
        except Exception:
            pass
        try:
            out = subprocess.run(["bluetoothctl", "devices", "Connected"], capture_output=True, text=True, timeout=5).stdout
            out = re.sub(r"\x1b\[[0-9;]*m", "", out)
            PROBE["bluetooth"] = [l.split(" ", 2)[2] for l in out.splitlines() if l.startswith("Device ") and l.count(" ") >= 2]
        except Exception:
            pass
        url = hlconfig.dashboard_config()["probe_url"]
        if not url:
            PROBE["site"] = None
        elif n % 4 == 0:   # every ~2 min
            try:
                req = urllib.request.Request(url, method="HEAD")
                PROBE["site"] = urllib.request.urlopen(req, timeout=8).status < 500
            except urllib.error.HTTPError as e:
                PROBE["site"] = e.code < 500
            except Exception:
                PROBE["site"] = False
        if n % 60 == 0:  # every ~30 min
            try:
                out = subprocess.run(["apt", "list", "--upgradable"], capture_output=True, text=True, timeout=60).stdout
                PROBE["updates"] = sum(1 for l in out.splitlines() if "/" in l)
            except Exception:
                pass
        n += 1
        time.sleep(30)


def net_rate():
    rx = tx = 0
    for line in read("/proc/net/dev").splitlines()[2:]:
        name, data = line.split(":", 1)
        if name.strip().startswith(("wl", "en", "eth")):
            f = data.split()
            rx, tx = rx + int(f[0]), tx + int(f[8])
    now = time.time()
    if _net["t"] and now - _net["t"] >= 1:
        dt = now - _net["t"]
        _net["rate"] = {"rx": (rx - _net["rx"]) / dt, "tx": (tx - _net["tx"]) / dt}
    if not _net["t"] or now - _net["t"] >= 1:
        _net.update(t=now, rx=rx, tx=tx)
    return _net["rate"]


# ---------- Claude transcript (the latest session on this machine) ----------
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def claude_feed(limit=14):
    files = sorted(CLAUDE_PROJECTS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    files = [f for f in files if f.stat().st_size > 0]
    if not files:
        return {"messages": []}
    f = files[0]
    with f.open("rb") as fh:                       # only the tail of big transcripts
        fh.seek(max(0, f.stat().st_size - 400_000))
        lines = fh.read().decode("utf-8", "replace").splitlines()[1:] if f.stat().st_size > 400_000 else fh.read().decode("utf-8", "replace").splitlines()
    msgs, last_kind = [], None
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") not in ("user", "assistant") or e.get("isSidechain") or e.get("isMeta"):
            continue
        content = (e.get("message") or {}).get("content")
        parts = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        for c in parts:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and c.get("text", "").strip():
                text = c["text"].strip()
                if text.startswith("<") and e["type"] == "user":   # command/system wrappers
                    continue
                msgs.append({"role": "user" if e["type"] == "user" else "assistant", "text": text[:700]})
                last_kind = e["type"]
            elif c.get("type") == "tool_use":
                inp = c.get("input") or {}
                detail = inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                msgs.append({"role": "tool", "text": f"{c.get('name')}: {str(detail)[:120]}".rstrip(": ")})
                last_kind = "tool"
    updated = f.stat().st_mtime
    live = time.time() - updated < 20 and last_kind != "assistant"
    return {"messages": msgs[-limit:], "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(updated)),
            "live": live, "session": f.stem}


# ---------- Spotify: the official web player in Chrome (CDP :9224) ----------
SPOT_JS = """(() => {
  const m = navigator.mediaSession && navigator.mediaSession.metadata;
  const q = id => document.querySelector('[data-testid="' + id + '"]');
  const btn = q('control-button-playpause');
  const login = !!document.querySelector('[data-testid="login-button"]');
  if (!m || !m.title) return {active: false, login};
  const art = (m.artwork || []).slice().sort((a, b) => parseInt(b.sizes) - parseInt(a.sizes))[0];
  return {active: true, login, title: m.title, artists: m.artist, album: m.album, cover: art ? art.src : null,
          playing: navigator.mediaSession.playbackState === 'playing' || (btn && /pause/i.test(btn.getAttribute('aria-label') || '')),
          position: (q('playback-position') || {}).textContent || '', duration: (q('playback-duration') || {}).textContent || ''};
})()"""


def spotify_state():
    try:
        return js(SPOT, SPOT_JS) or {"active": False}
    except Exception as e:
        return {"active": False, "error": "web player not reachable"}


SPOT_KEYS = {"playpause": "control-button-playpause", "next": "control-button-skip-forward", "prev": "control-button-skip-back"}


def spotify_control(action):
    js(SPOT, f"document.querySelector('[data-testid=\"{SPOT_KEYS[action]}\"]').click()")


# ---------- volume: default PipeWire sink (speaker), covers Spotify, YouTube, AirPlay ----------
SINK = "@DEFAULT_AUDIO_SINK@"


def volume_state():
    if not shutil.which("wpctl"):        # no PipeWire (headless box): no volume control
        return {"volume": None, "muted": False, "sink": ""}
    out = subprocess.run(["wpctl", "get-volume", SINK], capture_output=True, text=True, timeout=5).stdout
    m = re.search(r"Volume:\s*([\d.]+)", out)
    name = ""
    try:
        ins = subprocess.run(["wpctl", "inspect", SINK], capture_output=True, text=True, timeout=5).stdout
        n = re.search(r'node\.description = "([^"]*)"', ins) or re.search(r'node\.nick = "([^"]*)"', ins)
        name = n.group(1) if n else ""
    except Exception:
        pass
    return {"volume": float(m.group(1)) if m else None, "muted": "[MUTED]" in out, "sink": name}


def volume_set(value=None, step=None):
    if step is not None:
        step = max(-0.5, min(0.5, float(step)))
        arg = f"{abs(step):.2f}{'+' if step >= 0 else '-'}"
    else:
        arg = f"{max(0.0, min(1.0, float(value))):.2f}"
    subprocess.run(["wpctl", "set-volume", "-l", "1.0", SINK, arg], timeout=5, check=True)
    subprocess.run(["wpctl", "set-mute", SINK, "0"], timeout=5)
    return volume_state()


def volume_mute():
    subprocess.run(["wpctl", "set-mute", SINK, "toggle"], timeout=5, check=True)
    return volume_state()


# ---------- stats ----------
def read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def stats():
    mem = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0])
    du = shutil.disk_usage("/")
    temps = [int(read(f"{z}/temp", "0")) / 1000 for z in Path("/sys/class/thermal").glob("thermal_zone*")
             if read(f"{z}/type") == "x86_pkg_temp"]
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
                             capture_output=True, text=True, timeout=5).stdout
        containers = [c.split("\t") for c in out.splitlines() if c]
    except Exception:
        containers = []
    return {
        "load": os.getloadavg(), "cpus": os.cpu_count(),
        "mem_total": mem.get("MemTotal", 0) * 1024,
        "mem_used": (mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)) * 1024,
        "disk_total": du.total, "disk_used": du.used,
        "temp": temps[0] if temps else None,
        "battery": read("/sys/class/power_supply/BAT0/capacity"),
        "battery_status": read("/sys/class/power_supply/BAT0/status"),
        "uptime": float(read("/proc/uptime", "0 0").split()[0]),
        "containers": containers,
        "net": net_rate(),
        **PROBE,
        "state": dict(STATE, overlay=overlay_open(), display=display()),
    }


# ---------- agenda: snapshots pushed by clients/macos/homelab-sync ----------
PUSH_DIR = Path.home() / ".local/state/homelab/push"
PUSH_MAX = 4 << 20


def _csv(v):
    return [x.strip().lower() for x in str(v or "").split(",") if x.strip()]


def push_store(source, body):
    if not re.fullmatch(r"[a-z0-9-]{1,32}", source):
        raise ValueError("bad source name")
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    for k in ("events", "reminders", "notes"):
        if not isinstance(body.get(k, []), list):
            raise ValueError(f"{k} must be a list")
    body["received_at"] = time.time()
    PUSH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PUSH_DIR / f".{source}.json"
    tmp.write_text(json.dumps(body))
    tmp.chmod(0o600)
    tmp.replace(PUSH_DIR / f"{source}.json")
    return {"ok": True, "events": len(body.get("events", [])), "reminders": len(body.get("reminders", [])),
            "notes": len(body.get("notes", []))}


def agenda():
    cfg = hlconfig.read_settings()
    hidden, lists, wanted = _csv(cfg["hidden_calendars"]), _csv(cfg["reminder_lists"]), _csv(cfg["notes"])
    horizon = time.time() + max(1, int(cfg["agenda_days"])) * 86400
    events, reminders, notes, sources = [], [], {}, []
    for f in sorted(PUSH_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        sources.append({"source": f.stem, "device": d.get("device", ""), "received_at": d.get("received_at")})
        for e in d.get("events", []):
            cal, acct = str(e.get("calendar", "")), str(e.get("account", ""))
            if cal.lower() in hidden or f"{acct} / {cal}".lower() in hidden:
                continue
            if e.get("end_ts", 0) < time.time() or e.get("start_ts", 0) > horizon:
                continue
            events.append(e)
        for r in d.get("reminders", []):
            if not lists or str(r.get("list", "")).lower() in lists:
                reminders.append(r)
        for n in d.get("notes", []):
            notes[str(n.get("name", "")).lower()] = n
    events.sort(key=lambda e: (e.get("start_ts", 0), not e.get("all_day")))
    # overdue and dated first (soonest), then undated; flagged before the rest within each group
    reminders.sort(key=lambda r: (r.get("due_ts") is None, r.get("due_ts") or 0, not r.get("flagged")))
    ordered = [notes[n] for n in wanted if n in notes] if wanted else list(notes.values())
    return {"events": events, "reminders": reminders[:40], "notes": ordered, "sources": sources,
            "wanted_notes": [n.strip() for n in cfg["notes"].split(",") if n.strip()]}


# ---------- page builder: extra dashboard pages from JSON files ----------
# Page 1 (overview) ships with the repo, page 2 (calendar) shows once a client pushes a snapshot.
# Every *.json in PAGES_DIR adds a page: tiles laid out on the same 4x2 grid, filled from the page's
# source (a command printing JSON, an http(s) URL returning JSON, or a JSON file). The directory is
# outside the repo on purpose: pages belong to the projects that feed them, not to this repo.
# Pages are only read from disk; nothing over HTTP can create one, so a command source can't be injected.
PAGES_DIR = Path(os.environ.get("HOMELAB_PAGES_DIR") or Path.home() / ".config/homelab/pages")
BUILTIN_PAGES = [{"id": "1", "title": "Overview", "builtin": True},
                 {"id": "2", "title": "Calendar", "builtin": True, "optional": True}]
PAGE_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
TILE_TYPES = ("stat", "rows", "table", "line", "bars", "cells", "text", "mix", "summary", "heat")
_feeds, _feed_locks = {}, {}


def load_pages():
    pages, errors = [], []
    files = sorted(PAGES_DIR.glob("*.json")) if PAGES_DIR.is_dir() else []
    for f in files:
        try:
            p = json.loads(f.read_text())
            if not isinstance(p, dict):
                raise ValueError("expected a JSON object")
            pid = str(p.get("id") or f.name.split(".")[0])
            if not PAGE_ID.fullmatch(pid) or pid == "auto":
                raise ValueError("id: lowercase letters, digits and dashes, starting with a letter")
            tiles = p.get("tiles")
            if not isinstance(tiles, list) or not tiles:
                raise ValueError("tiles must be a non-empty list")
            for t in tiles:
                if not isinstance(t, dict) or t.get("type") not in TILE_TYPES:
                    raise ValueError(f"every tile needs a type: {', '.join(TILE_TYPES)}")
            strip = p.get("strip")
            if strip is not None and not (isinstance(strip, dict) and strip.get("field")):
                raise ValueError("strip must be an object with a field")
            src = p.get("source") or {}
            if not isinstance(src, dict):
                raise ValueError("source must be an object")
            if any(x["id"] == pid for x in pages):
                raise ValueError(f"duplicate page id {pid!r}")
            pages.append({"id": pid, "title": str(p.get("title") or pid), "order": float(p.get("order", 100)),
                          "refresh": max(5, int(p.get("refresh", 30))), "tiles": tiles, "strip": strip, "source": src,
                          "file": f.name})
        except (OSError, ValueError, TypeError) as e:
            errors.append({"file": f.name, "error": str(e)})
    pages.sort(key=lambda p: (p["order"], p["id"]))
    return pages, errors


def page_ids():
    return {p["id"] for p in BUILTIN_PAGES} | {p["id"] for p in load_pages()[0]}


def pages_view():
    pages, errors = load_pages()
    built = [{k: p[k] for k in ("id", "title", "refresh", "tiles", "strip")} for p in pages]
    return {"pages": BUILTIN_PAGES + built, "errors": errors, "dir": str(PAGES_DIR)}


def fetch_source(src):
    kind = src.get("type") or next((k for k in ("command", "url", "file") if k in src), None)
    timeout = float(src.get("timeout", 20))
    if kind == "command":
        cmd = src["command"]
        if not isinstance(cmd, list) or not cmd:
            raise ValueError("command must be a list: [program, arg, ...]")
        p = subprocess.run([os.path.expanduser(str(c)) for c in cmd], capture_output=True, text=True,
                           timeout=timeout, cwd=os.path.expanduser(str(src.get("cwd", "~"))))
        if p.returncode:
            raise RuntimeError((p.stderr or p.stdout).strip()[-300:] or f"exit code {p.returncode}")
        return json.loads(p.stdout)
    if kind == "url":
        url = str(src["url"])
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must be http(s)")
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    if kind == "file":
        return json.loads(Path(os.path.expanduser(str(src["file"]))).read_text())
    if not src:
        return {}
    raise ValueError("source needs a command, url or file")


def page_data(pid):
    page = next((p for p in load_pages()[0] if p["id"] == pid), None)
    if page is None:
        raise KeyError(pid)
    every = max(2.0, float(page["source"].get("every", page["refresh"])))
    with _feed_locks.setdefault(pid, threading.Lock()):   # one run per page at a time
        t, data = _feeds.get(pid, (0.0, None))
        if data is not None and time.time() - t < every:
            return data
        try:
            data = fetch_source(page["source"])
            if not isinstance(data, dict):
                raise ValueError("the source must return a JSON object")
            data["_updated"] = time.time()
        except Exception as e:   # keep showing the last good data, flagged
            old = _feeds.get(pid, (0.0, None))[1] or {}
            data = dict(old, _error=f"{type(e).__name__}: {e}"[:300])
        _feeds[pid] = (time.time(), data)
        return data


# ---------- settings ----------
def settings_view():
    env, out = hlconfig.read_env(), []
    known = set()
    for k in hlconfig.schema():
        known.add(k["key"])
        v = env.get(k["key"], "")
        out.append(dict(k, value="" if k["secret"] else v, set=bool(v)))
    for key, v in env.items():          # keys added by hand, not in the example
        if key not in known:
            out.append({"key": key, "default": "", "help": "", "secret": False, "value": v, "set": bool(v)})
    return {"dashboard": hlconfig.read_settings(), "env": out}


def settings_save(body):
    if body.get("dashboard"):
        hlconfig.write_settings(body["dashboard"])
    env = body.get("env") or {}
    hidden = hlconfig.secret_keys()
    # an empty secret field means "keep the current value"; clear one explicitly via "clear"
    updates = {k: str(v) for k, v in env.items() if not (k in hidden and v in ("", None))}
    updates.update({k: "" for k in body.get("clear") or []})
    if updates:
        hlconfig.write_env(updates)
        hlconfig.write_public_env()
    return settings_view()


def settings_apply():
    p = subprocess.run([str(HERE.parent / "bin/homelab"), "apply"], capture_output=True, text=True, timeout=600,
                       cwd=HERE.parent)
    return {"ok": p.returncode == 0, "log": (p.stdout + p.stderr)[-8000:]}


# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if rng:
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else end
            else:
                start = size - int(rng.group(2))
            end = min(end, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def params(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        p = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        if raw.lstrip().startswith("{"):
            p.update(json.loads(raw))
        elif raw:
            p.update(dict(urllib.parse.parse_qsl(raw)) or {"url": raw.strip()})
        return p

    def authorized(self):
        token = hlconfig.read_env().get("SETTINGS_TOKEN", "")
        given = self.headers.get("X-Settings-Token", "")
        return bool(token) and hmac.compare_digest(token.encode(), given.encode())

    def deny(self):
        if not hlconfig.read_env().get("SETTINGS_TOKEN"):
            return self.send(403, {"error": "no SETTINGS_TOKEN yet: run bin/homelab setup"})
        return self.send(401, {"error": "wrong or missing settings token"})

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self.send(200, (HERE / "control.html").read_bytes(), "text/html")
        if path == "/favicon.svg":
            return self.send(200, (HERE / "favicon.svg").read_bytes(), "image/svg+xml")
        if path == "/settings":
            return self.send(200, (HERE / "settings.html").read_bytes(), "text/html")
        if path == "/api/config":
            return self.send(200, hlconfig.dashboard_config())
        if path == "/api/agenda":
            return self.send(200, agenda())
        if path == "/api/pages":
            return self.send(200, pages_view())
        if path.startswith("/api/pages/"):
            try:
                return self.send(200, page_data(path.rsplit("/", 1)[1]))
            except KeyError:
                return self.send(404, {"error": "no such page"})
        if path == "/api/settings":
            return self.send(200, settings_view()) if self.authorized() else self.deny()
        if path in ("/shell", "/dashboard"):
            return self.send(200, (HERE / "dashboard.html").read_bytes(), "text/html")
        if path == "/media/fireplace.mp4" and (MEDIA / "fireplace.mp4").exists():
            return self.send_file(MEDIA / "fireplace.mp4", "video/mp4")
        if path == "/api/stats":
            return self.send(200, stats())
        if path == "/api/claude":
            return self.send(200, claude_feed())
        if path == "/api/spotify":
            return self.send(200, spotify_state())
        if path == "/api/volume":
            return self.send(200, volume_state())
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        try:
            if path.startswith("/api/push/"):
                token = hlconfig.read_env().get("PUSH_TOKEN", "")
                given = self.headers.get("X-Push-Token", "")
                if not token or not hmac.compare_digest(token.encode(), given.encode()):
                    return self.send(401, {"error": "wrong or missing push token"})
                n = int(self.headers.get("Content-Length") or 0)
                if n > PUSH_MAX:
                    return self.send(413, {"error": "snapshot too large"})
                body = json.loads(self.rfile.read(n) or b"{}")
                return self.send(200, push_store(path.rsplit("/", 1)[1], body))
            if path.startswith("/api/settings"):
                if not self.authorized():
                    return self.deny()
                if path == "/api/settings":
                    return self.send(200, settings_save(self.params()))
                if path == "/api/settings/apply":
                    r = settings_apply()
                    return self.send(200 if r["ok"] else 500, r)
                return self.send(404, {"error": "not found"})
            if path.startswith("/api/mode/"):
                set_mode(path.rsplit("/", 1)[1])
            elif path == "/api/play":
                url = self.params().get("url", "").strip()
                if not url.startswith(("http://", "https://")):
                    return self.send(400, {"error": "url must be http(s)"})
                play(url)
            elif path.startswith("/api/spotify/"):
                action = path.rsplit("/", 1)[1]
                if action not in SPOT_KEYS:
                    return self.send(400, {"error": "unknown action"})
                spotify_control(action)
            elif path == "/api/volume":
                p = self.params()
                if "step" not in p and "value" not in p:
                    return self.send(400, {"error": "value=0..1 or step=+-0.05"})
                return self.send(200, volume_set(p.get("value"), p.get("step")))
            elif path == "/api/volume/mute":
                return self.send(200, volume_mute())
            elif path.startswith("/api/page/"):
                set_page(path.rsplit("/", 1)[1])
            elif path == "/api/stop":
                stop()
            elif path.startswith("/api/key/"):
                key = path.rsplit("/", 1)[1]
                if key not in KEYS:
                    return self.send(400, {"error": "unknown key"})
                press(key)
            elif path in ("/api/screen/on", "/api/screen/off"):
                screen(path.endswith("on"))
            else:
                return self.send(404, {"error": "not found"})
            return self.send(200, {"ok": True, "state": STATE})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        except Exception as e:
            return self.send(500, {"error": str(e)})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", 8090), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=watch_overlays, daemon=True).start()
    threading.Thread(target=probe_loop, daemon=True).start()
    for _ in range(60 if display() else 0):  # the shell may have loaded before we were listening
        try:
            cdp(SHELL, ("Page.navigate", {"url": "http://127.0.0.1:8090/shell"}))
            break
        except Exception:
            time.sleep(2)
    sway("workspace number 1")
    threading.Thread(target=watch_cast, daemon=True).start()
    threading.Event().wait()
