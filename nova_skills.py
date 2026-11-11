"""
Nova's abilities: laptop control, timers & reminders, smarter files & notes, AI brain.
Keep this file next to assistant.py. The AI brain also needs:  pip install anthropic
"""
import base64
import ctypes
import difflib
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

try:
    import nova_memory as mem_mod
except ImportError:
    mem_mod = None

HOME = Path.home()
DATA = HOME / ".laptop_assistant"
DATA.mkdir(exist_ok=True)
UNDO_FILE, REM_FILE, CONFIG_FILE = DATA / "last_move.json", DATA / "reminders.json", DATA / "config.json"
NOTES = HOME / "Documents" / "Nova Notes.txt"
ENV_FILE = Path(__file__).resolve().parent / ".env"
IS_WIN = os.name == "nt"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WIN_ONLY = "Sorry, I can only do that on Windows."


def _load_dotenv(path=ENV_FILE):
    """Load KEY=VALUE pairs from a local .env file without overriding existing OS env vars."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k:
            os.environ.setdefault(k, v)


_load_dotenv()

ctx = {}                       # filled in by assistant.py (ask, is_yes, alert, task functions)
state = {"dictating": False}
_lock = threading.Lock()


def configure(**kw):
    ctx.update(kw)


def dictating():
    return state["dictating"]


def confirm(question, action, no="Okay, I won't do that."):
    return ctx["ask"](question, lambda x: action() if ctx["is_yes"](x) else no)


# ============ 1. laptop control ============
def ps(script, **env):
    """Run PowerShell hidden (stdin/stdout set so it also works from pythonw)."""
    return subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                          text=True, timeout=30, env={**os.environ, **env},
                          stdin=subprocess.DEVNULL, creationflags=NOWIN)


VK = {"mute": 0xAD, "down": 0xAE, "up": 0xAF, "next": 0xB0, "prev": 0xB1, "play": 0xB3,
      "alt": 0x12, "f4": 0x73, "win": 0x5B, "d": 0x44, "ctrl": 0x11, "v": 0x56}


def press(*keys, times=1):
    u = ctypes.windll.user32
    for _ in range(times):
        for k in keys:
            u.keybd_event(VK[k], 0, 0, 0)
        for k in reversed(keys):
            u.keybd_event(VK[k], 0, 2, 0)
        time.sleep(0.004)


def open_path(p):
    try:
        if IS_WIN:
            os.startfile(str(p))
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True
    except OSError:
        return False


def volume_set(pct):
    if not IS_WIN:
        return WIN_ONLY
    pct = max(0, min(100, int(pct)))
    press("down", times=50)
    press("up", times=pct // 2)
    return f"Volume set to {pct} percent."


def volume_change(direction, pct=10):
    if not IS_WIN:
        return WIN_ONLY
    press(direction, times=max(1, pct // 2))
    return f"Volume {direction}."


def media(action):
    if not IS_WIN:
        return WIN_ONLY
    key = "next" if re.search(r"next|skip", action) else "prev" if re.search(r"prev|back|last", action) else "play"
    press(key)
    return {"next": "Skipping.", "prev": "Going back.", "play": "Done."}[key]


def brightness(set_to=None, delta=0):
    if not IS_WIN:
        return WIN_ONLY
    cur = ps("(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness").stdout.strip()
    if not cur.isdigit():
        return "I couldn't change the brightness on this display."
    level = max(0, min(100, int(set_to) if set_to is not None else int(cur) + delta))
    ps("(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
       f".WmiSetBrightness(1, {level})")
    return f"Brightness set to {level} percent."


APPS = {"chrome": "chrome", "google chrome": "chrome", "edge": "msedge", "microsoft edge": "msedge",
        "firefox": "firefox", "notepad": "notepad", "calculator": "calc", "paint": "mspaint",
        "word": "winword", "excel": "excel", "powerpoint": "powerpnt", "outlook": "outlook",
        "teams": "msteams:", "spotify": "spotify:", "settings": "ms-settings:", "file explorer": "explorer",
        "explorer": "explorer", "task manager": "taskmgr", "terminal": "wt", "command prompt": "cmd",
        "cmd": "cmd", "vs code": "code", "vscode": "code", "visual studio code": "code",
        "whatsapp": "whatsapp:", "camera": "microsoft.windows.camera:", "store": "ms-windows-store:",
        "clock": "ms-clock:", "snipping tool": "snippingtool", "control panel": "control", "zoom": "zoom"}
SITES = {"youtube": "https://www.youtube.com", "google": "https://www.google.com",
         "gmail": "https://mail.google.com", "github": "https://github.com",
         "facebook": "https://www.facebook.com", "instagram": "https://www.instagram.com",
         "twitter": "https://x.com", "x": "https://x.com", "linkedin": "https://www.linkedin.com",
         "netflix": "https://www.netflix.com", "amazon": "https://www.amazon.com",
         "reddit": "https://www.reddit.com", "wikipedia": "https://www.wikipedia.org",
         "maps": "https://maps.google.com", "claude": "https://claude.ai",
         "google drive": "https://drive.google.com", "whatsapp web": "https://web.whatsapp.com"}
FOLDERS = {"download": "Downloads", "desktop": "Desktop", "document": "Documents",
           "picture": "Pictures", "video": "Videos", "music": "Music"}


SHELL = {"recycle bin": "shell:RecycleBinFolder", "recycling bin": "shell:RecycleBinFolder",
         "trash": "shell:RecycleBinFolder", "bin": "shell:RecycleBinFolder",
         "this pc": "shell:MyComputerFolder", "my computer": "shell:MyComputerFolder",
         "computer": "shell:MyComputerFolder", "network": "shell:NetworkPlacesFolder",
         "startup folder": "shell:startup", "device manager": "devmgmt.msc",
         "disk management": "diskmgmt.msc", "services": "services.msc", "event viewer": "eventvwr.msc",
         "system information": "msinfo32", "on screen keyboard": "osk", "magnifier": "magnify"}
SETTINGS = {"wifi": "ms-settings:network-wifi", "wi fi": "ms-settings:network-wifi",
            "network": "ms-settings:network", "bluetooth": "ms-settings:bluetooth",
            "display": "ms-settings:display", "sound": "ms-settings:sound",
            "battery": "ms-settings:batterysaver", "power": "ms-settings:powersleep",
            "update": "ms-settings:windowsupdate", "windows update": "ms-settings:windowsupdate",
            "privacy": "ms-settings:privacy", "microphone": "ms-settings:privacy-microphone",
            "camera": "ms-settings:privacy-webcam", "storage": "ms-settings:storagesense",
            "apps": "ms-settings:appsfeatures", "default apps": "ms-settings:defaultapps",
            "wallpaper": "ms-settings:personalization-background",
            "background": "ms-settings:personalization-background",
            "personalization": "ms-settings:personalization", "night light": "ms-settings:nightlight",
            "about": "ms-settings:about", "notifications": "ms-settings:notifications",
            "accounts": "ms-settings:yourinfo", "taskbar": "ms-settings:taskbar",
            "airplane mode": "ms-settings:network-airplanemode", "vpn": "ms-settings:network-vpn",
            "mouse": "ms-settings:mousetouchpad", "touchpad": "ms-settings:devices-touchpad"}
PROCS = {"chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe",
         "notepad": "notepad.exe", "word": "WINWORD.EXE", "excel": "EXCEL.EXE", "powerpoint": "POWERPNT.EXE",
         "outlook": "OUTLOOK.EXE", "spotify": "Spotify.exe", "teams": "ms-teams.exe",
         "calculator": "CalculatorApp.exe", "vs code": "Code.exe", "vscode": "Code.exe",
         "task manager": "Taskmgr.exe", "paint": "mspaint.exe", "zoom": "Zoom.exe", "whatsapp": "WhatsApp.exe",
         "discord": "Discord.exe", "slack": "slack.exe", "vlc": "vlc.exe"}


def _lookup(table, key, cutoff=0.8):
    """Exact match, else a close one (so 'recycle been' still finds 'recycle bin')."""
    if key in table:
        return table[key]
    m = difflib.get_close_matches(key, list(table), n=1, cutoff=cutoff)
    return table[m[0]] if m else None


def launch_any(target):
    if target.startswith("shell:"):                    # explorer opens shell folders (it reports exit 1 even on success)
        try:
            subprocess.Popen(["explorer.exe", target], creationflags=NOWIN)
            return IS_WIN
        except OSError:
            return False
    return launch(target)


_start = None


def start_apps():
    """Every app in the Start menu, as (name, id). Cached after the first lookup."""
    global _start
    if _start is None:
        _start = []
        if IS_WIN:
            try:
                out = ps("Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }").stdout
                _start = [tuple(l.split("|", 1)) for l in out.splitlines() if "|" in l]
            except Exception as err:
                print("start menu lookup failed:", err)
    return _start


def open_start_app(key):
    apps = {n.lower(): (n, i) for n, i in start_apps()}
    hit = apps.get(key) or next((v for n, v in apps.items() if n.startswith(key) or key in n.split()), None)
    if not hit:
        m = difflib.get_close_matches(key, list(apps), n=1, cutoff=0.8)
        hit = apps[m[0]] if m else None
    if not hit:
        return None
    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + hit[1]], creationflags=NOWIN)
    return hit[0]


def close_app(name):
    if not IS_WIN:
        return None
    exe = _lookup(PROCS, name.strip().lower(), 0.8)
    if not exe:
        return None
    r = subprocess.run(["taskkill", "/IM", exe], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, creationflags=NOWIN)      # polite close, asks about unsaved work
    return f"Closing {name.strip()}." if r.returncode == 0 else f"{name.strip().title()} doesn't seem to be running."


def empty_recycle_bin():
    if not IS_WIN:
        return WIN_ONLY
    ps("Clear-RecycleBin -Force -ErrorAction SilentlyContinue")
    return "Done. The Recycle Bin is empty."


def launch(target):
    if not IS_WIN:
        return False
    try:
        return ps("Start-Process $env:NOVA_ARG -ErrorAction Stop", NOVA_ARG=target).returncode == 0
    except Exception:
        return False


def folder_from(text, exact=False):
    for key, name in FOLDERS.items():
        hit = re.fullmatch(rf"{key}s?(?: folder)?", text) if exact else key in text
        if hit:
            return HOME / name
    return None


def web_search(query):
    webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote(query))
    return f"Searching the web for {query}."


def lock_screen():
    if not IS_WIN:
        return WIN_ONLY
    subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], creationflags=NOWIN)
    return "Locking your laptop."


def sleep_pc():
    if not IS_WIN:
        return WIN_ONLY
    subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=NOWIN)
    return "Going to sleep."


def power(kind):
    if not IS_WIN:
        return WIN_ONLY
    subprocess.run(["shutdown", "/s" if kind == "shutdown" else "/r", "/t", "30"], creationflags=NOWIN)
    word = "Shutting down" if kind == "shutdown" else "Restarting"
    return f"Okay. {word} in 30 seconds. Say cancel shutdown to stop it."


def screenshot():
    if not IS_WIN:
        return WIN_ONLY
    folder = HOME / "Pictures" / "Screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"nova_{datetime.now():%Y%m%d_%H%M%S}.png"
    ps("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
       "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
       "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
       "$g=[System.Drawing.Graphics]::FromImage($bmp);"
       "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
       "$bmp.Save($env:NOVA_PATH)", NOVA_PATH=str(path))
    return "Screenshot saved in your Pictures, Screenshots folder." if path.exists() else \
        "I couldn't take the screenshot."


def clipboard():
    if not IS_WIN:
        return ""
    return ps("Get-Clipboard -Raw").stdout.strip()


def type_text(text):
    if not IS_WIN:
        return WIN_ONLY
    ps("Set-Clipboard -Value $env:NOVA_TEXT", NOVA_TEXT=text)
    press("ctrl", "v")
    return None


class _SPS(ctypes.Structure):
    _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("pct", ctypes.c_ubyte),
                ("sys", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]


def battery():
    if not IS_WIN:
        return WIN_ONLY
    s = _SPS()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
    if s.pct == 255:
        return "This computer doesn't seem to have a battery."
    out = f"Your battery is at {s.pct} percent" + (" and charging." if s.ac == 1 else ".")
    if s.ac != 1 and s.life != 0xFFFFFFFF:
        out += f" About {s.life // 3600} hours {s.life % 3600 // 60} minutes left."
    return out


def disk_space():
    u = shutil.disk_usage(HOME.anchor or "/")
    return f"You have {u.free / 1e9:.0f} gigabytes free out of {u.total / 1e9:.0f}."


def wifi():
    if not IS_WIN:
        return WIN_ONLY
    out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL, creationflags=NOWIN).stdout
    m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
    return f"You're connected to {m.group(1).strip()}." if m else "You're not connected to Wi-Fi."


def system_status():
    return " ".join(x for x in (battery() if IS_WIN else "", disk_space()) if x)


def weather(city="", short=False):
    url = "https://wttr.in/" + urllib.parse.quote(city) + "?m&format=%C,+%t,+feels+like+%f,+humidity+%h"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        out = urllib.request.urlopen(req, timeout=5).read().decode("utf-8").strip()
    except Exception:
        return "" if short else "I couldn't get the weather. Are you online?"
    out = out.replace("+", "").replace("°C", " degrees")
    where = f"in {city}" if city else "near you"
    return f"The weather {where} is {out}."


# ============ 2. timers, alarms, reminders ============
WORDNUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
           "thirty": 30, "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90}
NUM = r"(?:\d+(?:\.\d+)?|" + "|".join(sorted(WORDNUM, key=len, reverse=True)) + ")"
UNITS = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)"
DUR = re.compile(rf"\b({NUM})\s*({UNITS})\b")
DUR_STRIP = re.compile(rf"\b(?:in |for |after )?{NUM}\s*{UNITS}\b|\b(?:in |for )?half (?:an? )?hour\b")
HOURW = {k: v for k, v in WORDNUM.items() if 1 <= v <= 12 and k not in ("a", "an")}
CLOCK = re.compile(r"\bat (\d{1,2}|" + "|".join(HOURW) + r"|noon|midnight)(?: (\d{2}))?"
                   r"(?: (a m|am|p m|pm|in the morning|in the afternoon|in the evening|tonight|o'?clock))?\b")


HALF_HOUR = re.compile(r"\bhalf (?:an? )?hour\b")


def parse_duration(t):
    total = 1800 if HALF_HOUR.search(t) else 0
    for num, unit in DUR.findall(HALF_HOUR.sub(" ", t)):
        n = float(num) if num[0].isdigit() else WORDNUM[num]
        total += n * {"s": 1, "m": 60, "h": 3600}[unit[0]]
    return total


def fmt_duration(s):
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = ([f"{h} hour{'s' * (h != 1)}"] if h else []) + ([f"{m} minute{'s' * (m != 1)}"] if m else []) \
        + ([f"{sec} second{'s' * (sec != 1)}"] if sec and not h else [])
    return " ".join(parts) or "0 seconds"


def parse_clock(t, prefer_am=False):
    """(datetime, span) for 'at 5 pm' / 'at 7 30' / 'tomorrow at 9' etc., else None."""
    m = CLOCK.search(t)
    if not m:
        return None
    w, mins, mer = m.group(1), int(m.group(2) or 0), m.group(3) or ""
    if w == "noon":
        h, mer = 12, "pm"
    elif w == "midnight":
        h, mer = 12, "am"
    else:
        h = int(w) if w.isdigit() else HOURW[w]
    if mins > 59 or h > 24:
        return None
    pm = mer in ("pm", "p m", "in the afternoon", "in the evening", "tonight")
    am = mer in ("am", "a m", "in the morning")
    tomorrow = "tomorrow" in t
    if pm:
        hours = [h % 12 + 12]
    elif am:
        hours = [h % 12]
    elif h > 12 or h == 0:
        hours = [h % 24]
    elif tomorrow:
        hours = [12 if h == 12 else (h if h >= 5 else h + 12)]
    else:
        hours = [h % 12, h % 12 + 12]
    now, cands = datetime.now(), []
    for hr in hours:
        d = datetime.combine((now + timedelta(days=int(tomorrow))).date(), dtime(hr % 24, mins))
        if d <= now:
            d += timedelta(days=1)
        cands.append(d)
    return (cands[0] if prefer_am else min(cands)), m.span()


def clock_text(d):
    day = "today" if d.date() == datetime.now().date() else \
        "tomorrow" if d.date() == datetime.now().date() + timedelta(days=1) else f"on {d:%A}"
    return f"{day} at {d.strftime('%I:%M %p').lstrip('0')}"


def _load_rem():
    try:
        return json.loads(REM_FILE.read_text())
    except Exception:
        return []


def add_reminder(due, text, kind):
    with _lock:
        items = _load_rem()
        items.append({"due": due, "text": text, "kind": kind})
        REM_FILE.write_text(json.dumps(items))


def _alert_text(i, now):
    late = now - i["due"] > 120
    if i["kind"] == "timer":
        return "Your timer is done."
    if i["kind"] == "alarm":
        return "Time to wake up. Your alarm is going off."
    return ("Missed reminder: " if late else "Reminder: ") + i["text"] + "."


def _scheduler():
    while True:
        time.sleep(1)
        try:
            with _lock:
                items, now = _load_rem(), time.time()
                due = [i for i in items if i["due"] <= now]
                if due:
                    REM_FILE.write_text(json.dumps([i for i in items if i["due"] > now]))
            for i in due:
                ctx.get("alert", lambda k, t: print(t))(i["kind"], _alert_text(i, now))
        except Exception as err:
            print("scheduler error:", err)


def start_scheduler():
    threading.Thread(target=_scheduler, daemon=True).start()
    if IS_WIN:
        threading.Thread(target=start_apps, daemon=True).start()   # so "open <any app>" is instant later


def set_timer(secs):
    add_reminder(time.time() + secs, "", "timer")
    return f"Timer set for {fmt_duration(secs)}."


def set_alarm(d):
    add_reminder(d.timestamp(), "", "alarm")
    return f"Alarm set for {clock_text(d)}."


def _clean_msg(s):
    s = re.sub(r"\b(?:remind me|set a reminder|set reminder|reminder|please|tomorrow)\b", " ", s)
    s = " ".join(s.split())
    return re.sub(r"^(?:to|that|about|for|of|and)\s+|\s+(?:to|at|in|on|for)$", "", s).strip()


def set_reminder_from(t):
    """Returns a reply, or None if there is no time in the request (then it's just a task)."""
    secs = parse_duration(t)
    if secs:
        due, rest, when = time.time() + secs, DUR_STRIP.sub(" ", t), f"in {fmt_duration(secs)}"
    elif (c := parse_clock(t)):
        d, (a, b) = c
        due, rest, when = d.timestamp(), t[:a] + " " + t[b:], clock_text(d)
    elif "tomorrow" in t:
        d = datetime.combine(datetime.now().date() + timedelta(days=1), dtime(9))
        due, rest, when = d.timestamp(), t, clock_text(d)
    else:
        return None
    text = _clean_msg(rest)
    if not text:
        return ctx["ask"]("What should I remind you about?",
                          lambda x: (add_reminder(due, x, "reminder"), f"Okay, I'll remind you {when}.")[1])
    add_reminder(due, text, "reminder")
    return f"Okay, I'll remind you to {text} {when}."


def list_reminders(kind=None):
    items = sorted((i for i in _load_rem() if kind in (None, i["kind"])), key=lambda i: i["due"])
    if not items:
        return "You have nothing set."
    now, parts = time.time(), []
    for i in items[:5]:
        label = {"timer": "a timer", "alarm": "an alarm"}.get(i["kind"], f"a reminder to {i['text']}")
        left = i["due"] - now
        parts.append(f"{label} {'in ' + fmt_duration(left) if left < 7200 else clock_text(datetime.fromtimestamp(i['due']))}")
    return "You have " + "; ".join(parts) + "."


def cancel_reminders(kind=None):
    with _lock:
        items = _load_rem()
        keep = [i for i in items if kind not in (None, i["kind"])]
        REM_FILE.write_text(json.dumps(keep))
    n = len(items) - len(keep)
    return f"Cancelled {n}." if n else "There was nothing to cancel."


# ============ 3. files and notes ============
STOP = {"file", "files", "the", "a", "my", "called", "named", "of"}
SEARCH_DIRS = ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music")
NUMW = {w: i + 1 for i, w in enumerate("one two three four five six seven eight nine ten".split())}
IMG = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".svg"}
EXTS = {"word": {".doc", ".docx"}, "excel": {".xls", ".xlsx", ".csv"}, "spreadsheet": {".xls", ".xlsx", ".csv"},
        "powerpoint": {".ppt", ".pptx"}, "presentation": {".ppt", ".pptx"}, "text": {".txt"},
        "pdf": {".pdf"}, "image": IMG, "photo": IMG, "picture": IMG, "screenshot": IMG, "jpg": {".jpg", ".jpeg"},
        "png": {".png"}, "video": {".mp4", ".mov", ".mkv", ".avi", ".webm"},
        "music": {".mp3", ".wav", ".m4a", ".flac"}, "song": {".mp3", ".wav", ".m4a", ".flac"},
        "audio": {".mp3", ".wav", ".m4a", ".flac", ".ogg"}, "zip": {".zip", ".rar", ".7z"},
        "archive": {".zip", ".rar", ".7z", ".tar", ".gz"}, "installer": {".exe", ".msi", ".dmg"},
        "document": {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt"}}


def qwords(query):
    return [w for w in re.split(r"\W+", query.lower()) if w and w not in STOP]


def search_files(words, limit=20):
    hits, deadline = [], time.time() + 5
    for base in SEARCH_DIRS:
        for root, dirs, files in os.walk(HOME / base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if all(w in re.sub(r"[_\-.]+", " ", f.lower()) for w in words):
                    hits.append(Path(root) / f)
            if time.time() > deadline:
                break
    hits.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return hits[:limit]


def find_files_text(query):
    hits = search_files(qwords(query), 5)
    for h in hits:
        print("   ", h)
    return ("Found: " + ", ".join(h.name for h in hits[:3])) if hits else "I couldn't find that."


def open_file(query, interactive=True):
    hits = search_files(qwords(query))
    if not hits:
        return None
    if len(hits) == 1 or not interactive:
        open_path(hits[0])
        return f"Opening {hits[0].name}."
    top = hits[:3]

    def pick(x):
        n = int(re.search(r"\d", x).group()) if re.search(r"\d", x) else \
            next((NUMW[w] for w in x.split() if w in NUMW), 0)
        if not 1 <= n <= len(top):
            return "Okay, never mind."
        open_path(top[n - 1])
        return f"Opening {top[n - 1].name}."
    names = ". ".join(f"{i + 1}, {p.name}" for i, p in enumerate(top))
    return ctx["ask"](f"I found {len(hits)} matches. {names}. Which one?", pick)


def newest(folder):
    files = [f for f in folder.iterdir() if f.is_file() and not f.name.startswith(".")] \
        if folder.exists() else []
    return max(files, key=lambda f: f.stat().st_mtime, default=None)


def open_thing(name, interactive=True):
    key = re.sub(r"^(?:(?:the|my|up|a) )+", "", name.lower().strip())
    key = re.sub(r"\s+(?:app|application|website|site|page|folder|please)$", "", key).strip()
    if key in ("tasks", "task", "to do list", "todo list", "to do"):
        return None
    for table in (APPS, SHELL):                                    # apps and special Windows places
        if (target := _lookup(table, key, 0.8)):
            return f"Opening {key}." if launch_any(target) else f"I couldn't open {key}."
    base = re.sub(r"\s+settings?$", "", key)
    if base != key or key in SETTINGS:                             # "wifi settings", "bluetooth"
        if (target := _lookup(SETTINGS, base, 0.75)):
            return f"Opening {base} settings." if launch_any(target) else f"I couldn't open {base} settings."
    if key in SITES:
        webbrowser.open(SITES[key])
        return f"Opening {key}."
    dom = key.replace(" dot ", ".").replace(" ", "")
    if re.fullmatch(r"[\w\-]+(\.[\w\-]+)*\.(com|org|net|io|co|edu|gov|ai|app)", dom):
        webbrowser.open("https://" + dom)
        return f"Opening {dom}."
    folder = folder_from(key, exact=True)
    if folder and folder.exists():
        open_path(folder)
        return f"Opening your {folder.name} folder."
    if re.match(r"(latest|newest|most recent|last)\b", key):
        f = newest(HOME / "Pictures" / "Screenshots" if "screenshot" in key else HOME / "Downloads")
        if f:
            open_path(f)
            return f"Opening {f.name}."
        return "I couldn't find one."
    if re.fullmatch(r"(?:nova )?notes?|notes file", key):
        NOTES.parent.mkdir(parents=True, exist_ok=True)
        NOTES.touch()
        open_path(NOTES)
        return "Opening your notes."
    if len(key) > 2 and (found := open_start_app(key)):           # any installed app
        return f"Opening {found}."
    r = open_file(key, interactive)
    if r:
        return r
    if launch(name):
        return f"Opening {key}."
    msg = f"I couldn't find anything called {key}."
    return None if ai_ready() else msg                             # with the AI brain, let it have a go


def unique(p):
    q, i = p, 1
    while q.exists():
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        i += 1
    return q


def do_moves(moves, msg):
    done = []
    for src, dst in moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst = unique(dst)
        shutil.move(str(src), str(dst))
        done.append([str(src), str(dst)])
    UNDO_FILE.write_text(json.dumps(done))          # same format assistant.py's undo uses
    return msg.format(n=len(done), s="" if len(done) == 1 else "s")


def move_type(typeword, src, dst):
    exts = next((e for k, e in EXTS.items() if k in re.sub(r"s$", "", typeword.strip())), None)
    if exts is None:
        return "What kind of files should I move? For example PDFs, images or videos."
    moves = [(f, dst / f.name) for f in src.iterdir()
             if f.is_file() and f.suffix.lower() in exts and not f.name.startswith(".")] if src.exists() else []
    if not moves:
        return f"I couldn't find any {typeword} in {src.name}."
    return confirm(f"I found {len(moves)} {typeword} in {src.name}. Move them to {dst.name}?",
                   lambda: do_moves(moves, "Done. I moved {n} file{s}. Say undo if you change your mind."))


def trash(paths):
    folder = DATA / "trash" / datetime.now().strftime("%Y%m%d_%H%M%S")
    return do_moves([(p, folder / p.name) for p in paths],
                    "Done. I moved {n} file{s} to Nova's trash. Say undo to bring it back.")


def biggest(n=3):
    sizes, deadline = [], time.time() + 6
    for base in ("Downloads", "Documents", "Desktop", "Videos", "Pictures"):
        for root, dirs, files in os.walk(HOME / base):
            for f in files:
                try:
                    sizes.append((os.path.getsize(os.path.join(root, f)), Path(root) / f))
                except OSError:
                    pass
            if time.time() > deadline:
                break
    sizes.sort(reverse=True)
    for s, p in sizes[:5]:
        print(f"    {s / 1e6:8.0f} MB  {p}")
    if not sizes:
        return "I couldn't find any files."
    return "Your biggest files are " + ", ".join(
        f"{p.name}, {s / 1e6:.0f} megabytes" for s, p in sizes[:n]) + ". I printed more in the log."


def take_note(text):
    NOTES.parent.mkdir(parents=True, exist_ok=True)
    with open(NOTES, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M}] {text.strip()}\n")
    return "Noted."


def read_notes(n=3):
    if not NOTES.exists() or not NOTES.read_text(encoding="utf-8").strip():
        return "You don't have any notes yet."
    lines = NOTES.read_text(encoding="utf-8").strip().splitlines()[-n:]
    return "Your latest notes: " + " ... ".join(re.sub(r"^\[[^\]]*\]\s*", "", l) for l in lines)


def briefing():
    now = datetime.now()
    part = "morning" if now.hour < 12 else "afternoon" if now.hour < 18 else "evening"
    out = [f"Good {part}. It's {now.strftime('%I:%M %p').lstrip('0')} on {now:%A}, {now:%B} {now.day}."]
    tasks = [x["text"] for x in ctx["load_tasks"]() if not x["done"]] if "load_tasks" in ctx else []
    if tasks:
        out.append(f"You have {len(tasks)} open task{'s' * (len(tasks) != 1)}, including {tasks[0]}.")
    end = datetime.combine(now.date() + timedelta(days=1), dtime(0)).timestamp()
    todays = [i for i in _load_rem() if i["kind"] != "timer" and i["due"] < end]
    if todays:
        out.append(f"{len(todays)} reminder{'s' * (len(todays) != 1)} or alarm{'s' * (len(todays) != 1)} today.")
    out.append(weather(short=True))
    return " ".join(x for x in out if x)


# ============ 4. AI brain, memory & screen awareness ============
AI_MODEL_DEFAULT = "claude-haiku-5-5"
AI_DEFAULTS = {
    "anthropic": {"model": AI_MODEL_DEFAULT, "base_url": "https://api.anthropic.com/v1"},
    "ollama_cloud": {"model": "deepseek-v4.1-flash", "base_url": "https://ollama.com/v1"},
    "openai": {"model": "gpt-4o-mini", "base_url": "https://api.openai.com/v1"},
    "gemini": {"model": "gemini-2.5-flash", "base_url": "https://generativelanguage.googleapis.com/v1beta"},
    "ollama": {"model": "llama3.2", "base_url": "http://127.0.0.1:11434"},
}
AI_HINT = ("I can answer general questions once my AI brain is set up. "
           "Run assistant.py with --setup-ai to connect Claude, Ollama Cloud, OpenAI, Gemini, or local Ollama.")
_history = []
_mem = None


def _memory():
    global _mem
    if _mem is None and mem_mod:
        _mem = mem_mod.Memory()
    return _mem


def active_window():
    """Return {'title': str, 'app': str} for the active Windows foreground window, else None."""
    if not IS_WIN:
        return None
    try:
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        n = u.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, title_buf, n + 1)
        pid = ctypes.c_ulong(0)
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        app = ""
        if pid.value:
            proc = k.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
            if proc:
                try:
                    exe_buf = ctypes.create_unicode_buffer(512)
                    size = ctypes.c_ulong(512)
                    if k.QueryFullProcessImageNameW(proc, 0, exe_buf, ctypes.byref(size)):
                        app = Path(exe_buf.value).name
                finally:
                    k.CloseHandle(proc)
        title = title_buf.value.strip()
        return {"title": title, "app": app} if (title or app) else None
    except Exception:
        return None


def active_window_text():
    info = active_window()
    if not info:
        return "I couldn't tell which window is active."
    if info["title"] and info["app"]:
        return f"You're in {info['app']}, titled {info['title']}."
    return f"The active window is {info['title'] or info['app']}."


def capture_screen_b64(max_dim=1280):
    """Capture the primary screen as a resized JPEG (base64, mime) for vision AI."""
    if not IS_WIN:
        return None, None
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab().convert("RGB")
    except Exception:
        try:
            from PIL import Image
            tmp = DATA / "_screen_tmp.png"
            ps("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
               "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
               "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
               "$g=[System.Drawing.Graphics]::FromImage($bmp);"
               "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
               "$bmp.Save($env:NOVA_PATH)", NOVA_PATH=str(tmp))
            img = Image.open(tmp).convert("RGB")
            tmp.unlink(missing_ok=True)
        except Exception:
            return None, None
    img.thumbnail((max_dim, max_dim))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"


def _config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def _ai_settings():
    """Resolve active (provider, api_key, model, base_url) from config and env vars."""
    cfg = _config()
    provider = cfg.get("provider")
    key = cfg.get("api_key")
    if not provider:
        if os.environ.get("ANTHROPIC_API_KEY") or key:
            provider = "anthropic"
        elif os.environ.get("OLLAMA_API_KEY"):
            provider = "ollama_cloud"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        elif os.environ.get("GEMINI_API_KEY"):
            provider = "gemini"
        else:
            return None, None, None, None
    env_key = {
        "anthropic": os.environ.get("ANTHROPIC_API_KEY"),
        "ollama_cloud": os.environ.get("OLLAMA_API_KEY"),
        "openai": os.environ.get("OPENAI_API_KEY"),
        "gemini": os.environ.get("GEMINI_API_KEY"),
    }.get(provider)
    key = env_key or (key if (cfg.get("provider") in (None, provider)) else None)
    defaults = AI_DEFAULTS.get(provider, {})
    model = cfg.get("model") or defaults.get("model", "")
    base_url = (cfg.get("base_url") or defaults.get("base_url", "")).rstrip("/")
    return provider, key, model, base_url


def _api_key():
    return _ai_settings()[1]


def ai_status():
    """Return (ready, provider, model) for diagnostics and routing."""
    provider, key, model, _ = _ai_settings()
    if not provider:
        return False, None, None
    ready = bool(model) if provider == "ollama" else bool(key and model)
    return ready, provider, model


def ai_ready():
    return ai_status()[0]


def reset_ai():
    _history.clear()


def _tool(name, desc, props):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object",
                             "properties": {k.rstrip("?"): {"type": v} for k, v in props.items()},
                             "required": [k for k in props if not k.endswith("?")]}}


def _norm_kind(kind):
    return kind if kind in ("timer", "alarm", "reminder") else None


def _tool_remember(key, value, category="general"):
    m = _memory()
    if not m:
        return "Memory module is unavailable."
    res = m.remember(key, value, category)
    return f"Remembered {res[0].replace('_', ' ')} as {res[1]}." if res else "Nothing to remember."


def _tool_recall(query=""):
    m = _memory()
    if not m:
        return "Memory module is unavailable."
    data = m.recall(query)
    parts = []
    if data["facts"]:
        parts.append("Facts: " + "; ".join(f"{f['key'].replace('_', ' ')} = {f['value']}" for f in data["facts"]))
    if data["episodes"]:
        parts.append("Recent: " + "; ".join(f"{e['user_text']} -> {e['assistant_text']}" for e in data["episodes"][:3]))
    return " | ".join(parts) if parts else "No matching memories found."


def _tool_forget(query):
    m = _memory()
    if not m:
        return "Memory module is unavailable."
    n = m.forget(query)
    return f"Forgot {n} item{'s' * (n != 1)}." if n else "Nothing matched that memory."


def inspect_screen(question="Describe what is currently on my screen and help with anything relevant."):
    if not ai_ready():
        return AI_HINT
    img_b64, mime = capture_screen_b64()
    if not img_b64:
        return "I couldn't capture the screen."
    return ask_ai_vision(question, img_b64, mime)


TOOLS = [
    _tool("open_app_or_website", "Open an app, folder, website or file by name.", {"name": "string"}),
    _tool("web_search", "Search the web in the user's browser.", {"query": "string"}),
    _tool("set_volume", "Set the speaker volume from 0 to 100.", {"percent": "number"}),
    _tool("media_control", "Control media: play, pause, next or previous.", {"action": "string"}),
    _tool("set_timer", "Start a countdown timer.", {"seconds": "number"}),
    _tool("set_reminder", "Speak a reminder to the user later.",
          {"minutes_from_now": "number", "message": "string"}),
    _tool("list_reminders", "List upcoming timers, alarms, or reminders.", {"kind?": "string"}),
    _tool("cancel_reminders", "Cancel active timers, alarms, or reminders.", {"kind?": "string"}),
    _tool("add_task", "Add an item to the to-do list.", {"text": "string"}),
    _tool("list_tasks", "Read the open to-do tasks.", {}),
    _tool("complete_task", "Mark a to-do task done by its number.", {"number": "number"}),
    _tool("find_files", "Find files by words in their name.", {"query": "string"}),
    _tool("read_clipboard", "Get the text currently copied on the clipboard.", {}),
    _tool("take_note", "Save a note for the user.", {"text": "string"}),
    _tool("read_notes", "Read the user's recent saved notes.", {"count?": "number"}),
    _tool("remember_fact", "Save a personal fact or preference about the user in long-term memory.",
          {"key": "string", "value": "string", "category?": "string"}),
    _tool("recall_memory", "Search long-term memory for facts or past conversations.", {"query?": "string"}),
    _tool("forget_memory", "Remove a fact from long-term memory.", {"query": "string"}),
    _tool("get_active_window", "Get the currently focused Windows app and window title.", {}),
    _tool("inspect_screen", "Take a live screenshot and visually inspect what is on the user's screen.",
          {"question?": "string"}),
    _tool("get_weather", "Current weather; leave city empty for the user's own location.", {"city?": "string"}),
    _tool("system_status", "Battery level and free disk space.", {}),
    _tool("take_screenshot", "Save a screenshot of the screen.", {}),
]
FUNCS = {
    "open_app_or_website": lambda name: open_thing(name, interactive=False) or "Nothing to open.",
    "web_search": web_search,
    "set_volume": lambda percent: volume_set(percent),
    "media_control": media,
    "set_timer": lambda seconds: set_timer(float(seconds)),
    "set_reminder": lambda minutes_from_now, message: (
        add_reminder(time.time() + float(minutes_from_now) * 60, message, "reminder"), "Reminder set.")[1],
    "list_reminders": lambda kind="": list_reminders(_norm_kind(kind)),
    "cancel_reminders": lambda kind="": cancel_reminders(_norm_kind(kind)),
    "add_task": lambda text: ctx["add_task"](text),
    "list_tasks": lambda: ctx["list_tasks"](),
    "complete_task": lambda number: ctx["change_task"](int(number)),
    "find_files": find_files_text,
    "read_clipboard": lambda: clipboard() or "The clipboard is empty.",
    "take_note": take_note,
    "read_notes": lambda count=3: read_notes(int(count)),
    "remember_fact": _tool_remember,
    "recall_memory": _tool_recall,
    "forget_memory": _tool_forget,
    "get_active_window": active_window_text,
    "inspect_screen": inspect_screen,
    "get_weather": lambda city="": weather(city),
    "system_status": system_status,
    "take_screenshot": screenshot,
}


def _system():
    base = ("You are Nova, a sharp, Jarvis-like personal AI assistant living on the user's Windows laptop. "
            "Your replies are spoken aloud: answer in one to three short sentences, in plain words, "
            "with no markdown, lists, links or emojis. Use the tools to act on the laptop when asked, "
            "remember personal facts/preferences the user shares via remember_fact, and confirm briefly what you did. "
            "If a request is unclear, ask one short question. If you don't know something, say so. "
            "You cannot delete files or change system power. "
            f"Current date and time: {datetime.now():%A, %B %d, %Y, %I:%M %p}.")
    win = active_window()
    if win:
        base += f"\nActive window: {win.get('app') or 'unknown'} — {win.get('title') or ''}."
    m = _memory()
    if m:
        mem_ctx = m.context_summary()
        if mem_ctx:
            base += "\n\n" + mem_ctx
    return base


def _run_tool(name, args):
    fn = FUNCS.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        return str(fn(**(args or {})))
    except Exception as err:
        return f"Error: {err}"


def _http_json(url, payload=None, headers=None, timeout=30):
    hdrs = {"User-Agent": "Nova/1.0", **(headers or {})}
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs,
                                 method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {err.code}: {body[:300]}") from err


def _ask_anthropic(text, key, model, base_url):
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msgs = _history + [{"role": "user", "content": text}]
        for _ in range(5):
            r = client.messages.create(model=model, max_tokens=400, system=_system(),
                                       tools=TOOLS, messages=msgs)
            if r.stop_reason != "tool_use":
                break
            msgs.append({"role": "assistant", "content": r.content})
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": _run_tool(b.name, b.input)}
                for b in r.content if b.type == "tool_use"
            ]})
        return " ".join(b.text for b in r.content if b.type == "text").strip() or "Done."
    except ImportError:
        msgs = _history + [{"role": "user", "content": text}]
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        for _ in range(5):
            r = _http_json(f"{base_url}/messages",
                           {"model": model, "max_tokens": 400, "system": _system(),
                            "tools": TOOLS, "messages": msgs}, headers)
            blocks = r.get("content", [])
            if r.get("stop_reason") != "tool_use":
                return " ".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip() or "Done."
            msgs.append({"role": "assistant", "content": blocks})
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b["id"],
                 "content": _run_tool(b["name"], b.get("input", {}))}
                for b in blocks if b.get("type") == "tool_use"
            ]})
        return "Done."


def _openai_tools():
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in TOOLS]


def _ask_openai(text, key, model, base_url):
    msgs = [{"role": "system", "content": _system()}] + _history + [{"role": "user", "content": text}]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    tools = _openai_tools()
    use_tools = True
    for _ in range(5):
        payload = {"model": model, "messages": msgs, "max_tokens": 400}
        if use_tools:
            payload["tools"] = tools
        try:
            r = _http_json(f"{base_url}/chat/completions", payload, headers, timeout=45)
        except RuntimeError as err:
            if use_tools and ("does not support tools" in str(err).lower() or ("tools" in str(err).lower() and "400" in str(err))):
                use_tools = False
                continue
            raise
        msg = r["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip() or "Done."
        msgs.append(msg)
        for tc in calls:
            fn = tc["function"]
            args = fn.get("arguments") or "{}"
            if isinstance(args, str):
                args = json.loads(args) if args.strip() else {}
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": _run_tool(fn["name"], args)})
    return "Done."


def _gemini_tools():
    decls = []
    for t in TOOLS:
        d = {"name": t["name"], "description": t["description"]}
        props = t["input_schema"]["properties"]
        if props:
            d["parameters"] = {
                "type": "OBJECT",
                "properties": {k: {**v, "type": v["type"].upper()} for k, v in props.items()},
                "required": t["input_schema"]["required"],
            }
        decls.append(d)
    return [{"functionDeclarations": decls}]


def _ask_gemini(text, key, model, base_url):
    contents = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in _history
    ] + [{"role": "user", "parts": [{"text": text}]}]
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    tools = _gemini_tools()
    url = f"{base_url}/models/{urllib.parse.quote(model)}:generateContent"
    for _ in range(5):
        r = _http_json(url, {
            "systemInstruction": {"parts": [{"text": _system()}]},
            "contents": contents,
            "tools": tools,
            "generationConfig": {"maxOutputTokens": 400},
        }, headers)
        content = r["candidates"][0]["content"]
        parts = content.get("parts", [])
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        if not calls:
            return " ".join(p.get("text", "") for p in parts if "text" in p).strip() or "Done."
        contents.append({"role": "model", "parts": parts})
        contents.append({"role": "function", "parts": [
            {"functionResponse": {"name": c["name"], "response": {"output": _run_tool(c["name"], c.get("args", {}))}}}
            for c in calls
        ]})
    return "Done."


def _ask_ollama(text, model, base_url):
    msgs = [{"role": "system", "content": _system()}] + _history + [{"role": "user", "content": text}]
    headers = {"Content-Type": "application/json"}
    tools = _openai_tools()
    use_tools = True
    for _ in range(5):
        payload = {"model": model, "messages": msgs, "stream": False}
        if use_tools:
            payload["tools"] = tools
        try:
            r = _http_json(f"{base_url}/api/chat", payload, headers, timeout=60)
        except RuntimeError as err:
            if use_tools and "does not support tools" in str(err).lower():
                use_tools = False
                continue
            raise
        msg = r["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip() or "Done."
        msgs.append(msg)
        for tc in calls:
            fn = tc["function"]
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args) if args.strip() else {}
            msgs.append({"role": "tool", "content": _run_tool(fn["name"], args)})
    return "Done."


def _ai_error_reply(provider, err):
    msg = str(err)
    if "HTTP 402" in msg:
        return "Your AI provider account needs usage credits or a plan."
    if "HTTP 401" in msg or "HTTP 403" in msg:
        return "My AI API key was rejected. Check your key."
    if "HTTP 429" in msg:
        return "The AI service is busy or rate limited right now."
    if provider == "ollama":
        return "I couldn't reach local Ollama. Make sure Ollama is running and the model is pulled."
    return "I couldn't reach my AI brain. Check your internet connection and API key."


def ask_ai(text):
    """Returns Nova's reply, or None if the AI brain isn't set up."""
    ready, provider, model = ai_status()
    if not ready:
        return None
    _, key, _, base_url = _ai_settings()
    try:
        if provider == "anthropic":
            reply = _ask_anthropic(text, key, model, base_url)
        elif provider in ("openai", "ollama_cloud"):
            reply = _ask_openai(text, key, model, base_url)
        elif provider == "gemini":
            reply = _ask_gemini(text, key, model, base_url)
        elif provider == "ollama":
            reply = _ask_ollama(text, model, base_url)
        else:
            return f"Unknown AI provider: {provider}."
        _history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
        del _history[:-8]
        m = _memory()
        if m:
            m.add_episode(text, reply)
        return reply
    except Exception as err:
        print("AI error:", err)
        return _ai_error_reply(provider, err)


def ask_ai_vision(question, image_b64, mime="image/jpeg"):
    """Answer a question about a captured screen image across the configured AI provider."""
    ready, provider, model = ai_status()
    if not ready:
        return AI_HINT
    _, key, _, base_url = _ai_settings()
    try:
        if provider == "anthropic":
            r = _http_json(f"{base_url}/messages", {
                "model": model, "max_tokens": 400, "system": _system(),
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": image_b64}},
                    {"type": "text", "text": question},
                ]}],
            }, {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            reply = " ".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text").strip() or "Done."
        elif provider in ("openai", "ollama_cloud"):
            r = _http_json(f"{base_url}/chat/completions", {
                "model": model, "max_tokens": 400,
                "messages": [
                    {"role": "system", "content": _system()},
                    {"role": "user", "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                    ]},
                ],
            }, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=45)
            reply = (r["choices"][0]["message"].get("content") or "").strip() or "Done."
        elif provider == "gemini":
            url = f"{base_url}/models/{urllib.parse.quote(model)}:generateContent"
            r = _http_json(url, {
                "systemInstruction": {"parts": [{"text": _system()}]},
                "contents": [{"role": "user", "parts": [
                    {"inlineData": {"mimeType": mime, "data": image_b64}},
                    {"text": question},
                ]}],
                "generationConfig": {"maxOutputTokens": 400},
            }, {"x-goog-api-key": key, "Content-Type": "application/json"})
            parts = r["candidates"][0]["content"].get("parts", [])
            reply = " ".join(p.get("text", "") for p in parts if "text" in p).strip() or "Done."
        elif provider == "ollama":
            r = _http_json(f"{base_url}/api/chat", {
                "model": model, "stream": False,
                "messages": [
                    {"role": "system", "content": _system()},
                    {"role": "user", "content": question, "images": [image_b64]},
                ],
            }, {"Content-Type": "application/json"}, timeout=60)
            reply = (r["message"].get("content") or "").strip() or "Done."
        else:
            return f"Unknown AI provider: {provider}."
        m = _memory()
        if m:
            m.add_episode(question, reply)
        return reply
    except Exception as err:
        print("AI vision error:", err)
        return _ai_error_reply(provider, err)


def setup_ai():
    import getpass
    print("Choose Nova's AI brain:")
    print("  1) Anthropic Claude (API key)")
    print("  2) Ollama Cloud (https://ollama.com/v1, e.g. deepseek-v4.1-flash)")
    print("  3) OpenAI / OpenAI-compatible (OpenAI, Groq, OpenRouter, etc.)")
    print("  4) Google Gemini (API key)")
    print("  5) Local Ollama (free, offline, no API key needed)")
    choice = (input("Pick 1-5 [2]: ").strip() or "2")
    providers = {"1": "anthropic", "2": "ollama_cloud", "3": "openai", "4": "gemini", "5": "ollama"}
    provider = providers.get(choice)
    if not provider:
        print("Invalid choice.")
        return
    cfg = _config()
    cfg["provider"] = provider
    defaults = AI_DEFAULTS[provider]

    if provider == "ollama":
        base_url = input(f"Ollama URL [{defaults['base_url']}]: ").strip() or defaults["base_url"]
        models = []
        try:
            tags = _http_json(f"{base_url.rstrip('/')}/api/tags", timeout=3)
            models = [m["name"] for m in tags.get("models", []) if "name" in m]
        except Exception:
            pass
        if models:
            print("Installed Ollama models:", ", ".join(models))
        default_model = models[0] if models else defaults["model"]
        model = input(f"Model name [{default_model}]: ").strip() or default_model
        cfg["base_url"] = base_url.rstrip("/")
        cfg["model"] = model
        cfg.pop("api_key", None)
    else:
        hints = {
            "anthropic": "https://console.anthropic.com",
            "ollama_cloud": "https://ollama.com/settings/keys (or set OLLAMA_API_KEY)",
            "openai": "https://platform.openai.com/api-keys",
            "gemini": "https://aistudio.google.com/apikey",
        }
        print(f"Key source: {hints[provider]}")
        key = getpass.getpass("Paste your API key (it won't show as you type): ").strip()
        if not key:
            print("No key entered.")
            return
        cfg["api_key"] = key
        if provider in ("openai", "ollama_cloud"):
            base_url = input(f"Base URL [{defaults['base_url']}]: ").strip() or defaults["base_url"]
            cfg["base_url"] = base_url.rstrip("/")
        else:
            cfg.pop("base_url", None)
        model = input(f"Model name [{defaults['model']}]: ").strip() or defaults["model"]
        cfg["model"] = model

    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    print(f"Saved to {CONFIG_FILE}. Testing...")
    print(ask_ai("Say hello in five words.") or "No reply.")


def ai_about_clipboard(raw):
    text = clipboard()
    if not text:
        return "Your clipboard is empty."
    if not ai_ready():
        return AI_HINT
    return ask_ai(f"{raw}\n\nClipboard contents:\n{text[:6000]}")


# ============ the voice-command router ============
def dictate(raw, t):
    """While dictation is on, everything you say is typed. Returns a reply or None."""
    if re.search(r"\b(stop|end|finish|exit)\b.*\bdictat", t) or t in ("stop", "stop dictating"):
        state["dictating"] = False
        return "Dictation off."
    type_text(raw.strip() + " ")
    return None


def route(t, raw):
    """Match a cleaned command to an ability. Returns a reply, or None if it's not mine."""
    # --- memory, screen & active window ---
    if re.search(r"\b(what's|what is|read|look at|see|check|describe|explain)\b.*\b(on my screen|my screen|this screen)\b", t):
        return inspect_screen(raw)
    if re.search(r"\b(what window|which window|what app|which app|active window)\b", t):
        return active_window_text()
    if m := re.match(r"remember (?:that |my )(.+)", t):
        if _memory():
            return mem_mod.remember_phrase(_memory(), m.group(0)[len("remember "):])
    if re.search(r"\bwhat do you (remember|know)\b", t):
        if _memory():
            q = re.sub(r"^.*?\b(?:about me|about)\b\s*", "", t).strip() if "about" in t else ""
            facts = _memory().recall(q, limit=5)["facts"]
            if not facts:
                return "I don't have anything saved about that yet."
            return "I remember: " + "; ".join(f"{f['key'].replace('_', ' ')} is {f['value']}" for f in facts) + "."
    if m := re.match(r"forget (?:that |about |my )?(.+)", t):
        target = m.group(1).strip()
        if target not in ("it", "that", "everything") and _memory():
            n = _memory().forget(target)
            return f"Okay, I forgot {n} item{'s' * (n != 1)}." if n else "I didn't find anything matching that in memory."

    # --- "open ... settings" must win over status questions like "wifi" or "battery" ---
    if m := re.match(r"(?:open|launch|go to|show me)\s+(.+\bsettings?)$", t):
        return open_thing(m.group(1))

    # --- briefing, dictation, typing, notes, clipboard ---
    if re.fullmatch(r"good (morning|afternoon|evening)( nova)?", t) or \
            re.search(r"\b(daily|morning) briefing\b|\bbrief me\b|\bwhat's my day\b", t):
        return briefing()
    if re.match(r"(start dictat|dictate|dictation mode)", t):
        state["dictating"] = True
        return "Dictation on. I'll type what you say. Say stop dictating when you're done."
    if m := re.search(r"\btype\s+(.+)", raw, re.I) if re.match(r"type\b", t) else None:
        return type_text(m.group(1)) or "Typed."
    if m := re.match(r"(?:take|make|add|write|jot)(?: down)? a note(?: that| to self)?\s*(.*)", t) \
            or re.match(r"(?:note down|write down|jot down)\s+(.+)", t):
        return take_note(m.group(1)) if m.group(1) else ctx["ask"]("Sure. What should the note say?", take_note)
    if re.search(r"\b(read|show|what are|what's in)\b.*\bnotes?\b", t) and "open" not in t:
        return read_notes()
    if re.search(r"\bclipboard\b|\bwhat i copied\b", t):
        if re.search(r"\b(summari[sz]e|explain|translate|rewrite|proofread|fix|shorten)\b", t):
            return ai_about_clipboard(raw)
        return (clipboard()[:300] or "Your clipboard is empty.") if re.search(r"\b(read|what)\b", t) else None

    # --- timers, alarms, reminders ---
    kinds = {"timer": "timer", "alarm": "alarm", "reminder": "reminder"}
    if re.search(r"\b(cancel|stop|delete|clear|remove)\b.*\b(timers?|alarms?|reminders?)\b", t):
        k = next((v for w, v in kinds.items() if w in t), None)
        return cancel_reminders(k)
    if re.search(r"\b(what|which|list|show|any|how much|how long)\b.*\b(timers?|alarms?|reminders?|time left)\b", t):
        k = next((v for w, v in kinds.items() if w in t), None)
        return list_reminders(k)
    if re.search(r"\b(timer|countdown)\b", t):
        secs = parse_duration(t)
        return set_timer(secs) if secs else ctx["ask"](
            "For how long?", lambda x: set_timer(parse_duration(x)) if parse_duration(x) else "Sorry, I didn't get a time.")
    if re.search(r"\balarm\b|\bwake me\b", t):
        secs = parse_duration(t)
        if secs:                                          # "alarm in 2 hours"
            return set_alarm(datetime.now() + timedelta(seconds=secs))
        c = parse_clock(re.sub(r"\bfor\b", "at", t, count=1), prefer_am=True)   # "alarm for 7 am"
        if c:
            return set_alarm(c[0])

        def from_answer(x):
            c2 = parse_clock("at " + re.sub(r"^(?:at|for) ", "", x), prefer_am=True)
            return set_alarm(c2[0]) if c2 else "Sorry, I didn't get a time."
        return ctx["ask"]("What time should I wake you?", from_answer)
    if re.search(r"\bremind me\b|\bset (?:a )?reminder\b", t):
        r = set_reminder_from(t)
        if r:
            return r                          # no time given -> falls through to a plain task

    # --- volume, media, brightness ---
    if re.search(r"\b(un)?mute\b", t) and "timer" not in t:
        if not IS_WIN:
            return WIN_ONLY
        press("mute")
        return "Done."
    if re.search(r"\bvolume\b|\blouder\b|\bquieter\b|\bsofter\b|\bturn (it|the (sound|music)) (up|down)\b", t):
        n, by = re.search(r"\b(\d{1,3})\b", t), re.search(r"\bby (\d{1,3})\b", t)
        up = re.search(r"\b(up|higher|increase|raise|louder)\b", t)
        down = re.search(r"\b(down|lower|decrease|reduce|quieter|softer)\b", t)
        if re.search(r"\b(max|maximum|full)\b", t):
            return volume_set(100)
        if n and not by and (not (up or down) or re.search(r"\b(to|at)\s+\d", t)):
            return volume_set(int(n.group(1)))
        return volume_change("down" if down else "up", int(by.group(1)) if by else 10)
    if re.search(r"\bbrightness\b|\bbrighter\b|\bdimmer\b|\bdim the screen\b", t):
        n = re.search(r"\b(\d{1,3})\b", t)
        if n and not re.search(r"\b(up|down)\b", t):
            return brightness(set_to=int(n.group(1)))
        return brightness(delta=-20 if re.search(r"\b(down|dimmer|dim|lower)\b", t) else 20)
    if re.fullmatch(r"(pause|resume|play|stop)( the)?( music| song| video| media)?|play (some )?music", t) \
            and t != "stop":
        return media("play")
    if re.fullmatch(r"(next|skip)( the| this)?( song| track| video)?", t):
        return media("next")
    if re.fullmatch(r"(previous|go back|last)( song| track| video)?", t):
        return media("prev")
    if m := re.match(r"play (.+?)(?: on (youtube|spotify))?$", t):
        what, where = m.group(1), m.group(2) or "youtube"
        if where == "spotify":
            webbrowser.open("spotify:search:" + urllib.parse.quote(what))
            return f"Searching Spotify for {what}."
        webbrowser.open("https://www.youtube.com/results?search_query=" + urllib.parse.quote(what))
        return f"Searching YouTube for {what}."

    # --- system ---
    if re.search(r"\bcancel\b.*\b(shut ?down|restart)\b", t):
        if IS_WIN:
            subprocess.run(["shutdown", "/a"], creationflags=NOWIN)
        return "Okay, cancelled."
    if re.search(r"\b(shut ?down|power off|turn off)\b.*\b(computer|laptop|pc)\b", t):
        return confirm("Do you want me to shut down the computer?", lambda: power("shutdown"))
    if re.search(r"\b(restart|reboot)\b.*\b(computer|laptop|pc)\b", t):
        return confirm("Do you want me to restart the computer?", lambda: power("restart"))
    if re.search(r"\block\b.*\b(laptop|computer|pc|screen)\b|^lock$", t):
        return lock_screen()
    if re.search(r"\bsleep\b", t) and re.search(r"\b(laptop|computer|pc)\b", t):
        return confirm("Put the computer to sleep?", sleep_pc)
    if re.search(r"\b(take|capture|grab|make)\b.*\bscreen ?shot\b|^screen ?shot$", t):
        return screenshot()
    if re.search(r"\b(minimi[sz]e)\b.*\b(everything|all|windows)\b|\bshow (the )?desktop\b", t):
        if not IS_WIN:
            return WIN_ONLY
        press("win", "d")
        return "Done."
    if re.search(r"\bclose\b.*\b(this|the|current|active)\b.*\bwindow\b|^close (this|it|window)$", t):
        if not IS_WIN:
            return WIN_ONLY
        press("alt", "f4")
        return "Closed."
    if m := re.match(r"switch to (.+)", t):
        if IS_WIN and "True" in ps("(New-Object -ComObject WScript.Shell).AppActivate($env:NOVA_ARG)",
                                   NOVA_ARG=m.group(1)).stdout:
            return f"Switched to {m.group(1)}."
        return f"I couldn't find a window called {m.group(1)}."

    # --- info ---
    if re.search(r"\bbattery\b", t):
        return battery()
    if re.search(r"\b(disk|storage|drive)\b.*\b(space|left|free)\b|\bhow much (disk |storage )?space\b", t):
        return disk_space()
    if re.search(r"\bwi ?fi\b", t):
        return wifi()
    if re.search(r"\bsystem status\b", t):
        return system_status()
    if re.search(r"\b(weather|forecast|will it rain|is it going to rain)\b", t):
        m = re.search(r"\bin ([a-z ]+)$", t)
        return weather(m.group(1).strip() if m else "")

    # --- files ---
    if re.search(r"\b(biggest|largest)\b.*\bfiles?\b", t):
        return biggest()
    if m := re.search(r"\b(?:create|make|new) (?:a |new )*folder (?:called |named )?(.+?)"
                      r"(?: (?:in|on|inside|under) (?:my |the )?(\w+))?$", t):
        name = re.sub(r'[<>:"/\\|?*]', "", m.group(1)).strip().title()
        base = folder_from(m.group(2) or "") or HOME / "Documents"
        if name:
            (base / name).mkdir(parents=True, exist_ok=True)
            return f"Created the folder {name} in {base.name}."
    if m := re.search(r"\bmove (?:all )?(?:my |the )?(.+?)(?: files?)? (?:from|in|on) (?:my |the )?(\w+)"
                      r"(?: folder)? (?:to|into) (?:my |the )?(\w+)", t):
        src, dst = folder_from(m.group(2)), folder_from(m.group(3))
        if not (src and dst):
            return "I only know downloads, desktop, documents, pictures, videos and music."
        return move_type(m.group(1), src, dst)
    if re.search(r"\b(delete|trash|remove)\b", t) and re.search(r"\bfiles?\b", t):
        words = [w for w in re.sub(r"\b(delete|trash|remove|the|my|file|files|please)\b", " ", t).split()]
        hits = search_files(words) if words else []
        if not hits:
            return "I couldn't find that file."
        p = hits[0]
        return confirm(f"Delete {p.name} from {p.parent.name}? It goes to Nova's trash, so you can undo.",
                       lambda: trash([p]))

    if re.search(r"\bempty\b", t) and re.search(r"\b(recycle|recycling|trash|bin)\b", t):
        return confirm("Empty the Recycle Bin? This permanently deletes everything in it.", empty_recycle_bin)
    if m := re.match(r"(?:close|quit|exit|stop|kill) (.+)", t):
        r = close_app(m.group(1))
        if r:
            return r

    # --- web and opening things ---
    if m := re.match(r"(?:google|look up|search (?:the web|online|google)(?: for)?)\s+(.+)", t):
        return web_search(m.group(1))
    if m := re.match(r"(?:open|launch|start|go to|visit)\s+(.+)", t):
        return open_thing(m.group(1))
    return None
