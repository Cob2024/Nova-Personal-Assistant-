"""
Nova's abilities: laptop control, timers & reminders, smarter files & notes, AI brain.
Keep this file next to assistant.py. The AI brain also needs:  pip install anthropic
"""
import ast
import atexit
import base64
import csv
import ctypes
import difflib
from html.parser import HTMLParser
import io
import json
import math
import operator
import os
import re
import shutil
import socket
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

try:
    import nova_calendar as cal_mod
except ImportError:
    cal_mod = None

HOME = Path.home()
DATA = HOME / ".laptop_assistant"
DATA.mkdir(exist_ok=True)
UNDO_FILE, REM_FILE, CONFIG_FILE = DATA / "last_move.json", DATA / "reminders.json", DATA / "config.json"
ROUTINES_FILE = DATA / "routines.json"
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
      "alt": 0x12, "f4": 0x73, "win": 0x5B, "d": 0x44, "ctrl": 0x11, "shift": 0x10,
      "tab": 0x09, "pgup": 0x21, "pgdn": 0x22, "end": 0x23, "home": 0x24,
      "left_arrow": 0x25, "up_arrow": 0x26, "right_arrow": 0x27, "down_arrow": 0x28,
      "a": 0x41, "c": 0x43, "f": 0x46, "r": 0x52, "t": 0x54, "v": 0x56, "w": 0x57,
      "x": 0x58, "y": 0x59, "z": 0x5A}


def press(*keys, times=1):
    u = ctypes.windll.user32
    for _ in range(times):
        for k in keys:
            u.keybd_event(VK[k], 0, 0, 0)
        for k in reversed(keys):
            u.keybd_event(VK[k], 0, 2, 0)
        time.sleep(0.004)


_SHORTCUTS = {
    "maximize": (("win", "up_arrow"), "Maximized window."),
    "minimize": (("win", "down_arrow"), "Minimized window."),
    "snap_left": (("win", "left_arrow"), "Snapped window left."),
    "snap_right": (("win", "right_arrow"), "Snapped window right."),
    "switch_window": (("alt", "tab"), "Switched window."),
    "new_tab": (("ctrl", "t"), "Opened a new tab."),
    "close_tab": (("ctrl", "w"), "Closed the tab."),
    "reopen_tab": (("ctrl", "shift", "t"), "Reopened the tab."),
    "next_tab": (("ctrl", "tab"), "Switched to the next tab."),
    "prev_tab": (("ctrl", "shift", "tab"), "Switched to the previous tab."),
    "refresh": (("ctrl", "r"), "Refreshed."),
    "find_on_page": (("ctrl", "f"), "Opened find."),
    "copy": (("ctrl", "c"), "Copied."),
    "paste": (("ctrl", "v"), "Pasted."),
    "cut": (("ctrl", "x"), "Cut."),
    "select_all": (("ctrl", "a"), "Selected all."),
    "undo_edit": (("ctrl", "z"), "Undid the last edit."),
    "redo": (("ctrl", "y"), "Redid the last edit."),
    "scroll_down": (("pgdn",), "Scrolled down."),
    "scroll_up": (("pgup",), "Scrolled up."),
    "scroll_top": (("ctrl", "home"), "Scrolled to the top."),
    "scroll_bottom": (("ctrl", "end"), "Scrolled to the bottom."),
}


def window_action(action):
    """Execute a standard Windows window, browser tab, scrolling, or editing shortcut."""
    if not IS_WIN:
        return WIN_ONLY
    spec = _SHORTCUTS.get(action)
    if not spec:
        return None
    keys, reply = spec
    press(*keys)
    return reply


def open_path(p):
    try:
        if IS_WIN:
            os.startfile(str(p))
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True
    except OSError:
        return False


_AUDIO_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
    int RegisterControlChangeNotify(IntPtr pNotify);
    int UnregisterControlChangeNotify(IntPtr pNotify);
    int GetChannelCount(out uint pnChannelCount);
    int SetMasterVolumeLevel(float fLevelDB, ref Guid pguidEventContext);
    int SetMasterVolumeLevelScalar(float fLevel, ref Guid pguidEventContext);
    int GetMasterVolumeLevel(out float pfLevelDB);
    int GetMasterVolumeLevelScalar(out float pfLevel);
    int SetChannelVolumeLevel(uint nChannel, float fLevelDB, ref Guid pguidEventContext);
    int SetChannelVolumeLevelScalar(uint nChannel, float fLevel, ref Guid pguidEventContext);
    int GetChannelVolumeLevel(uint nChannel, out float pfLevelDB);
    int GetChannelVolumeLevelScalar(uint nChannel, out float pfLevel);
    int SetMute([MarshalAs(UnmanagedType.Bool)] bool bMute, ref Guid pguidEventContext);
    int GetMute(out bool pbMute);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
    int Activate(ref Guid id, int clsCtx, IntPtr pActivationParams, [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
    int EnumAudioEndpoints(int dataFlow, int dwStateMask, out IntPtr ppDevices);
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppDevice);
}
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumerator {}
public class NovaAudio {
    static IAudioEndpointVolume Ep() {
        var e = (IMMDeviceEnumerator)(new MMDeviceEnumerator());
        IMMDevice dev; e.GetDefaultAudioEndpoint(0, 0, out dev);
        var iid = typeof(IAudioEndpointVolume).GUID;
        object o; dev.Activate(ref iid, 23, IntPtr.Zero, out o);
        return (IAudioEndpointVolume)o;
    }
    public static string Run(string cmd, int val) {
        var ep = Ep();
        var g = Guid.Empty;
        float v; bool m;
        if (cmd == "set") {
            float lvl = Math.Max(0f, Math.Min(1f, val / 100f));
            ep.SetMasterVolumeLevelScalar(lvl, ref g);
        } else if (cmd == "delta") {
            ep.GetMasterVolumeLevelScalar(out v);
            float lvl = Math.Max(0f, Math.Min(1f, v + val / 100f));
            ep.SetMasterVolumeLevelScalar(lvl, ref g);
        } else if (cmd == "mute") {
            if (val < 0) { ep.GetMute(out m); ep.SetMute(!m, ref g); }
            else { ep.SetMute(val != 0, ref g); }
        }
        ep.GetMasterVolumeLevelScalar(out v);
        ep.GetMute(out m);
        return ((int)Math.Round(v * 100)) + "|" + (m ? "1" : "0");
    }
}
"@
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::Out.WriteLine("ready"); [Console]::Out.Flush()
while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $parts = $line.Trim().Split(" ")
    if ($parts.Length -ge 1 -and $parts[0] -ne "") {
        try {
            $v = if ($parts.Length -ge 2) { [int]$parts[1] } else { 0 }
            [Console]::Out.WriteLine("ok:" + [NovaAudio]::Run($parts[0], $v))
        } catch {
            [Console]::Out.WriteLine("err:" + $_.Exception.Message)
        }
    } else {
        [Console]::Out.WriteLine("err:empty")
    }
    [Console]::Out.Flush()
}
'''

_audio_lock = threading.Lock()
_audio_proc = None


def close_audio():
    global _audio_proc
    if _audio_proc is not None:
        try:
            _audio_proc.stdin.close()
            _audio_proc.terminate()
        except Exception:
            pass
        _audio_proc = None


atexit.register(close_audio)


def _audio_cmd(cmd, val=0):
    """Run a command on persistent Windows Core Audio COM helper; returns (pct, muted) or None."""
    global _audio_proc
    if not IS_WIN:
        return None
    with _audio_lock:
        try:
            if _audio_proc is None or _audio_proc.poll() is not None:
                enc = base64.b64encode(_AUDIO_SCRIPT.encode("utf-16le")).decode()
                _audio_proc = subprocess.Popen(
                    ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", bufsize=1, creationflags=NOWIN,
                )
                if _audio_proc.stdout.readline().strip() != "ready":
                    close_audio()
                    return None
            _audio_proc.stdin.write(f"{cmd} {int(val)}\n")
            _audio_proc.stdin.flush()
            resp = _audio_proc.stdout.readline().strip()
            if resp.startswith("ok:"):
                p, m = resp[3:].split("|", 1)
                return int(p), m == "1"
        except Exception:
            close_audio()
    return None


def volume_set(pct):
    if not IS_WIN:
        return WIN_ONLY
    pct = max(0, min(100, int(pct)))
    res = _audio_cmd("set", pct)
    if res is not None:
        return f"Volume set to {res[0]} percent."
    press("down", times=50)
    press("up", times=pct // 2)
    return f"Volume set to {pct} percent."


def volume_change(direction, pct=10):
    if not IS_WIN:
        return WIN_ONLY
    delta = -abs(int(pct)) if direction == "down" else abs(int(pct))
    res = _audio_cmd("delta", delta)
    if res is not None:
        return f"Volume at {res[0]} percent."
    press(direction, times=max(1, abs(int(pct)) // 2))
    return f"Volume {direction}."


def volume_mute(mute=None):
    if not IS_WIN:
        return WIN_ONLY
    mode = -1 if mute is None else (1 if mute else 0)
    res = _audio_cmd("mute", mode)
    if res is not None:
        return "Muted." if res[1] else f"Unmuted, volume at {res[0]} percent."
    press("mute")
    return "Done."


def volume_status():
    if not IS_WIN:
        return WIN_ONLY
    res = _audio_cmd("get", 0)
    if res is None:
        return "I couldn't check the current volume."
    pct, muted = res
    return f"Volume is muted, currently at {pct} percent." if muted else f"Volume is at {pct} percent."


def media(action):
    if not IS_WIN:
        return WIN_ONLY
    key = "next" if re.search(r"next|skip", action) else "prev" if re.search(r"prev|back|last", action) else "play"
    press(key)
    return {"next": "Skipping.", "prev": "Going back.", "play": "Done."}[key]


def brightness_status():
    if not IS_WIN:
        return WIN_ONLY
    cur = ps("(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness").stdout.strip()
    if not cur.isdigit():
        return "I couldn't read the brightness on this display."
    return f"Brightness is at {cur} percent."


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
        "whatsapp": "whatsapp:", "what's up": "whatsapp:", "whats up": "whatsapp:",
        "telegram": "tg:", "discord": "discord:", "slack": "slack:", "vlc": "vlc",
        "camera": "microsoft.windows.camera:", "store": "ms-windows-store:",
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
         "task manager": "Taskmgr.exe", "paint": "mspaint.exe", "zoom": "Zoom.exe",
         "whatsapp": "WhatsApp.exe", "what's up": "WhatsApp.exe", "whats up": "WhatsApp.exe",
         "terminal": "WindowsTerminal.exe", "command prompt": "cmd.exe", "cmd": "cmd.exe",
         "snipping tool": "SnippingTool.exe", "discord": "Discord.exe", "slack": "slack.exe", "vlc": "vlc.exe"}


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


def _running_procs():
    """Return list of (exe, pid, mem_kb) from Windows tasklist."""
    if not IS_WIN:
        return []
    r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, creationflags=NOWIN)
    procs = []
    for row in csv.reader(r.stdout.splitlines()):
        if len(row) < 5:
            continue
        exe, pid = row[0].strip(), row[1].strip()
        mem_nums = re.sub(r"[^\d]", "", row[4])
        procs.append((exe, pid, int(mem_nums) if mem_nums else 0))
    return procs


def top_processes(n=3):
    """Report the apps using the most RAM right now."""
    if not IS_WIN:
        return WIN_ONLY
    totals = {}
    for exe, _, kb in _running_procs():
        name = re.sub(r"\.exe$", "", exe, flags=re.I)
        if name.lower() in ("memory compression", "system", "idle"):
            continue
        totals[name] = totals.get(name, 0) + kb
    ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:max(1, int(n))]
    if not ranked:
        return "I couldn't inspect running apps."
    parts = [f"{name} at {kb / 1024:.0f} megabytes" for name, kb in ranked]
    return "Top memory users are " + ", ".join(parts) + "."


def running_apps_text(limit=8):
    """List top active user-facing applications currently running."""
    if not IS_WIN:
        return WIN_ONLY
    ignore = {"memory compression", "system", "idle", "svchost", "registry", "smss", "csrss",
              "wininit", "services", "lsass", "fontdrvhost", "dwm", "conhost", "runtimebroker",
              "searchhost", "shellexperiencehost", "startmenuexperiencehost", "textinputhost",
              "taskhostw", "sihost", "ctfmon", "dllhost", "audiodg", "spoolsv", "wmiprvse"}
    totals, display = {}, {}
    for exe, _, kb in _running_procs():
        name = re.sub(r"\.exe$", "", exe, flags=re.I)
        low = name.lower()
        if not low or low in ignore:
            continue
        totals[low] = totals.get(low, 0) + kb
        display.setdefault(low, name)
    if not totals:
        return "I couldn't inspect running apps."
    top = [display[k] for k, _ in sorted(totals.items(), key=lambda x: x[1], reverse=True)[:max(1, int(limit))]]
    if len(top) == 1:
        return f"Currently running: {top[0]}."
    return "Currently running apps include " + ", ".join(top[:-1]) + f", and {top[-1]}."


def close_app(name):
    if not IS_WIN:
        return None
    raw = re.sub(r"^(?:(?:the|my|that)\s+)+", "", name.strip().lower())
    raw = re.sub(r"(?:\s+(?:app|application|window|program|please|hello|hi|hey|now|thanks|thank you))+$",
                 "", raw).strip()
    if not raw:
        return None
    exe = _lookup(PROCS, raw, 0.8)
    if not exe:
        running = {re.sub(r"\.exe$", "", p[0], flags=re.I).lower(): p[0] for p in _running_procs()}
        exe = running.get(raw) or next((v for k, v in running.items() if raw in k), None)
    if not exe:
        return None
    r = subprocess.run(["taskkill", "/IM", exe], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, creationflags=NOWIN)      # polite close, asks about unsaved work
    return f"Closing {raw}." if r.returncode == 0 else f"{raw.title()} doesn't seem to be running."


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


def clear_clipboard():
    if not IS_WIN:
        return WIN_ONLY
    ps("Set-Clipboard -Value ''")
    return "Clipboard cleared."


def type_text(text):
    if not IS_WIN:
        return WIN_ONLY
    ps("Set-Clipboard -Value $env:NOVA_TEXT", NOVA_TEXT=text)
    press("ctrl", "v")
    return None


class _SPS(ctypes.Structure):
    _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("pct", ctypes.c_ubyte),
                ("sys", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]


class _MSE(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _battery_info():
    if not IS_WIN:
        return None
    s = _SPS()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
    return None if s.pct == 255 else (int(s.pct), s.ac == 1, int(s.life))


def battery():
    if not IS_WIN:
        return WIN_ONLY
    info = _battery_info()
    if not info:
        return "This computer doesn't seem to have a battery."
    pct, charging, life = info
    out = f"Your battery is at {pct} percent" + (" and charging." if charging else ".")
    if not charging and life != 0xFFFFFFFF:
        out += f" About {life // 3600} hours {life % 3600 // 60} minutes left."
    return out


def disk_space():
    u = shutil.disk_usage(HOME.anchor or "/")
    return f"You have {u.free / 1e9:.0f} gigabytes free out of {u.total / 1e9:.0f}."


def system_diagnostics():
    """Live hardware summary: RAM, uptime, battery, and free disk space."""
    parts = []
    if IS_WIN:
        m = _MSE()
        m.dwLength = ctypes.sizeof(_MSE)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            total_gb = m.ullTotalPhys / (1024 ** 3)
            used_gb = (m.ullTotalPhys - m.ullAvailPhys) / (1024 ** 3)
            parts.append(f"Memory is at {m.dwMemoryLoad} percent, using {used_gb:.1f} of {total_gb:.0f} gigabytes.")
        ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        up_s = ctypes.windll.kernel32.GetTickCount64() // 1000
        h, rem = divmod(up_s, 3600)
        parts.append(f"Uptime is {h} hours and {rem // 60} minutes.")
        if _battery_info():
            parts.append(battery())
    parts.append(disk_space())
    return " ".join(parts)


def wifi():
    if not IS_WIN:
        return WIN_ONLY
    out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL, creationflags=NOWIN).stdout
    m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
    return f"You're connected to {m.group(1).strip()}." if m else "You're not connected to Wi-Fi."


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
    except OSError:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            ip = ""
    return f"Your local IP address is {ip}." if ip else "I couldn't find your local IP address."


def internet_status():
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=2.0):
            online = True
    except OSError:
        online = False
    if IS_WIN:
        w = wifi()
        return f"You're online. {w}" if online else f"You appear to be offline. {w}"
    return "You're online." if online else "You appear to be offline."


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


def add_reminder(due, text, kind, repeat_secs=0):
    with _lock:
        items = _load_rem()
        entry = {"due": due, "text": text, "kind": kind}
        if repeat_secs and repeat_secs > 0:
            entry["repeat_secs"] = float(repeat_secs)
        items.append(entry)
        REM_FILE.write_text(json.dumps(items))


def _alert_text(i, now):
    late = now - i["due"] > 120
    if i["kind"] == "timer":
        return "Your timer is done."
    if i["kind"] == "alarm":
        return "Time to wake up. Your alarm is going off."
    return ("Missed reminder: " if late else "Reminder: ") + i["text"] + "."


_battery_warned = False


def _check_proactive_battery():
    global _battery_warned
    info = _battery_info()
    if not info:
        return
    pct, charging, _ = info
    if charging or pct > 25:
        _battery_warned = False
    elif pct <= 15 and not _battery_warned:
        _battery_warned = True
        ctx.get("alert", lambda k, t: print(t))("battery", f"Heads up, your battery is down to {pct} percent.")


def _scheduler():
    ticks = 0
    while True:
        time.sleep(1)
        ticks += 1
        try:
            with _lock:
                items, now = _load_rem(), time.time()
                due, keep = [], []
                for i in items:
                    if i["due"] <= now:
                        due.append(dict(i))
                        rep = float(i.get("repeat_secs") or 0)
                        if rep > 0:
                            nxt = dict(i)
                            while nxt["due"] <= now:
                                nxt["due"] += rep
                            keep.append(nxt)
                    else:
                        keep.append(i)
                if due:
                    REM_FILE.write_text(json.dumps(keep))
            for i in due:
                ctx.get("alert", lambda k, t: print(t))(i["kind"], _alert_text(i, now))
            if ticks % 30 == 0:
                _check_proactive_battery()
            if ticks % 60 == 0 and cal_mod:
                for msg in cal_mod.due_meeting_alerts(10):
                    ctx.get("alert", lambda k, t: print(t))("calendar", msg)
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
    s = re.sub(r"\b(?:remind me|set a reminder|set reminder|reminder|please|tomorrow|every day|daily)\b", " ", s)
    s = re.sub(r"\bevery\b", " ", s)
    s = " ".join(s.split())
    return re.sub(r"^(?:to|that|about|for|of|and)\s+|\s+(?:to|at|in|on|for)$", "", s).strip()


def set_reminder_from(t):
    """Returns a reply, or None if there is no time in the request (then it's just a task)."""
    repeat_secs = 0
    if re.search(r"\bevery (?:day|morning|evening|night)\b|\bdaily\b", t):
        repeat_secs = 86400
    secs = parse_duration(t)
    if secs:
        if re.search(r"\bevery\b", t):
            repeat_secs = secs
        due, rest = time.time() + secs, DUR_STRIP.sub(" ", t)
        when = (f"every {fmt_duration(repeat_secs)}" if repeat_secs else f"in {fmt_duration(secs)}")
    elif (c := parse_clock(t)):
        d, (a, b) = c
        due, rest = d.timestamp(), t[:a] + " " + t[b:]
        when = (f"every day at {d.strftime('%I:%M %p').lstrip('0')}" if repeat_secs else clock_text(d))
    elif "tomorrow" in t:
        d = datetime.combine(datetime.now().date() + timedelta(days=1), dtime(9))
        due, rest, when = d.timestamp(), t, clock_text(d)
    else:
        return None
    text = _clean_msg(rest)
    if not text:
        return ctx["ask"]("What should I remind you about?",
                          lambda x: (add_reminder(due, x, "reminder", repeat_secs), f"Okay, I'll remind you {when}.")[1])
    add_reminder(due, text, "reminder", repeat_secs)
    return f"Okay, I'll remind you to {text} {when}."


def list_reminders(kind=None):
    items = sorted((i for i in _load_rem() if kind in (None, i["kind"])), key=lambda i: i["due"])
    if not items:
        return "You have nothing set."
    now, parts = time.time(), []
    for i in items[:5]:
        label = {"timer": "a timer", "alarm": "an alarm"}.get(i["kind"], f"a reminder to {i['text']}")
        left = i["due"] - now
        when = "in " + fmt_duration(left) if left < 7200 else clock_text(datetime.fromtimestamp(i["due"]))
        rep = float(i.get("repeat_secs") or 0)
        rep_txt = f" (every {fmt_duration(rep)})" if rep > 0 else ""
        parts.append(f"{label} {when}{rep_txt}")
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


SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "site-packages", "target", "dist", "build", "appdata"}


def search_files(words, limit=20):
    hits, deadline = [], time.time() + 5
    for base in SEARCH_DIRS:
        for root, dirs, files in os.walk(HOME / base):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIRS]
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
    key = re.sub(r"^(?:(?:the|my|up|a|that)\s+)+", "", name.lower().strip())
    key = re.sub(r"(?:\s+(?:app|application|website|site|page|folder|please|hello|hi|hey|now|thanks|thank you))+$",
                 "", key).strip()
    if not key:
        return None
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
    return None if (ai_ready() and not ai_blocked()) else msg      # with a working AI brain, let it have a go


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
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIRS]
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
    uname = get_user_name()
    greet = f"Good {part}, {uname}." if uname else f"Good {part}."
    out = [f"{greet} It's {now.strftime('%I:%M %p').lstrip('0')} on {now:%A}, {now:%B} {now.day}."]
    tasks = [x["text"] for x in ctx["load_tasks"]() if not x["done"]] if "load_tasks" in ctx else []
    if tasks:
        out.append(f"You have {len(tasks)} open task{'s' * (len(tasks) != 1)}, including {tasks[0]}.")
    end = datetime.combine(now.date() + timedelta(days=1), dtime(0)).timestamp()
    todays = [i for i in _load_rem() if i["kind"] != "timer" and i["due"] < end]
    if todays:
        out.append(f"{len(todays)} reminder{'s' * (len(todays) != 1)} or alarm{'s' * (len(todays) != 1)} today.")
    if cal_mod:
        cal_txt = cal_mod.events_summary("today")
        if not cal_txt.startswith("You have no"):
            out.append(cal_txt)
    info = _battery_info()
    if info and not info[1] and info[0] <= 30:
        out.append(f"Your battery is at {info[0]} percent and unplugged.")
    out.append(weather(short=True))
    return " ".join(x for x in out if x)


class _TextHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            s = " ".join(data.split())
            if s:
                self.parts.append(s)


DEFAULT_ROUTINES = {
    "focus mode": ["set volume to 25", "open vs code", "set a timer for 50 minutes"],
    "break time": ["set volume to 50", "set a timer for 10 minutes", "list my tasks"],
    "morning routine": ["daily briefing", "list my tasks"],
    "night mode": ["set brightness to 25", "set volume to 25"],
}
_routine_depth = 0


def load_routines():
    try:
        saved = json.loads(ROUTINES_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            return {**DEFAULT_ROUTINES, **{k.lower(): v for k, v in saved.items() if isinstance(v, list)}}
    except Exception:
        pass
    return dict(DEFAULT_ROUTINES)


def save_routine(name, steps):
    key = " ".join(name.lower().split())
    clean_steps = [s.strip() for s in steps if s and s.strip()]
    if not key or not clean_steps:
        return "Give a routine name and at least one step."
    with _lock:
        try:
            saved = json.loads(ROUTINES_FILE.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                saved = {}
        except Exception:
            saved = {}
        saved[key] = clean_steps
        ROUTINES_FILE.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    return f"Saved routine {key} with {len(clean_steps)} steps."


def delete_routine(name):
    key = " ".join(name.lower().split())
    with _lock:
        try:
            saved = json.loads(ROUTINES_FILE.read_text(encoding="utf-8"))
        except Exception:
            saved = {}
        if key not in saved:
            return f"I didn't find a custom routine called {key}."
        del saved[key]
        ROUTINES_FILE.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    return f"Deleted routine {key}."


def list_routines():
    r = load_routines()
    return "Available routines: " + ", ".join(sorted(r)) + "."


def run_routine(name):
    global _routine_depth
    if _routine_depth >= 2:
        return "Stopped nested routine loop."
    routines = load_routines()
    key = " ".join(name.lower().split())
    if key not in routines:
        m = difflib.get_close_matches(key, list(routines), n=1, cutoff=0.75)
        key = m[0] if m else None
    if not key:
        return f"I don't have a routine called {name.strip()}."
    steps = routines[key]
    runner = ctx.get("handle") or (lambda s: route(s.lower(), s))
    results = []
    _routine_depth += 1
    try:
        for step in steps:
            out = runner(step)
            if out:
                results.append(out)
    finally:
        _routine_depth -= 1
    return " ".join(results[:4]) if results else f"Ran {key}."


def _find_repo(repo_hint=""):
    hint = repo_hint.strip().strip("\"'")
    if not hint:
        return Path(__file__).resolve().parent
    p = Path(hint)
    if p.exists():
        return p
    for base in (HOME / "Documents", HOME / "Desktop", HOME):
        if not base.exists():
            continue
        for child in base.iterdir():
            if child.is_dir() and child.name.lower() == hint.lower() and (child / ".git").exists():
                return child
    return None


def git_status(repo=""):
    target = _find_repo(repo)
    if not target or not (target / ".git").exists():
        return f"I couldn't find a git repository{f' named {repo}' if repo else ''}."
    try:
        r = subprocess.run(["git", "-C", str(target), "status", "--short", "--branch"],
                           capture_output=True, text=True, timeout=5,
                           stdin=subprocess.DEVNULL, creationflags=NOWIN)
    except Exception as err:
        return f"Could not run git ({err})."
    if r.returncode != 0:
        return f"Git failed in {target.name}."
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    branch = lines[0].replace("## ", "").split("...")[0] if lines else "unknown"
    changes = len(lines) - 1
    if changes <= 0:
        return f"{target.name} is on branch {branch} with a clean working tree."
    return f"{target.name} is on branch {branch} with {changes} uncommitted change{'s' * (changes != 1)}."


def github_status():
    try:
        auth = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True,
                              timeout=5, stdin=subprocess.DEVNULL, creationflags=NOWIN)
    except FileNotFoundError:
        return "GitHub CLI is not installed."
    except Exception as err:
        return f"Could not check GitHub ({err})."
    if auth.returncode != 0:
        return "GitHub CLI is not logged in yet. Run gh auth login in a terminal."
    r = subprocess.run(["gh", "status"], capture_output=True, text=True,
                       timeout=10, stdin=subprocess.DEVNULL, creationflags=NOWIN)
    out = " ".join((r.stdout or r.stderr).split())
    return out[:280] if out else "GitHub has no active notifications or pull requests."


def _exec_shell(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20,
                           stdin=subprocess.DEVNULL, creationflags=NOWIN)
        out = " ".join((r.stdout or r.stderr).split()).strip()
        if r.returncode == 0:
            return out[:240] if out else "Command finished."
        return f"Command exited with code {r.returncode}. {out[:180]}".strip()
    except subprocess.TimeoutExpired:
        return "That command timed out after 20 seconds."
    except Exception as err:
        return f"Command failed: {err}"


def quick_lookup(topic):
    """Fetch a concise 1-2 sentence factual summary from Wikipedia (no API key needed)."""
    q = re.sub(r"^(?:who|what)\s+(?:is|was|are)\s+|^(?:define|tell me about)\s+", "", topic.strip(), flags=re.I)
    q = q.strip(" ?.!")
    if not q:
        return None
    slug = urllib.parse.quote(q.replace(" ", "_"))
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}"
    try:
        data = _http_json(url, timeout=6)
        extract = " ".join((data.get("extract") or "").split())
        if not extract:
            return None
        sents = re.split(r"(?<=[.!?])\s+", extract)
        return " ".join(sents[:2])[:360]
    except Exception:
        return None


def read_webpage(url):
    """Fetch an http/https webpage and extract clean readable text (up to 4000 chars)."""
    u = url.strip()
    if not u.startswith(("http://", "https://")):
        return "I can only read http or https web links."
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "Nova/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(250_000).decode("utf-8", errors="replace")
        parser = _TextHTMLParser()
        parser.feed(raw)
        text = " ".join(parser.parts)
        return text[:4000] if text else "The page had no readable text."
    except Exception as err:
        return f"I couldn't read that page ({err})."


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
_AI_COOLDOWN_SECS = 600.0
_ai_blocked_until = 0.0
_ai_blocked_msg = ""


def ai_blocked():
    """True while recent billing or auth errors paused outbound AI calls."""
    return time.monotonic() < _ai_blocked_until


def reset_ai_cooldown():
    """Clear any AI billing/auth cooldown so the next request tries again."""
    global _ai_blocked_until, _ai_blocked_msg
    _ai_blocked_until = 0.0
    _ai_blocked_msg = ""


def _memory():
    global _mem
    if _mem is None and mem_mod:
        _mem = mem_mod.Memory()
    return _mem


def _format_person_name(name):
    cleaned = re.sub(r"[?.!,]+$", "", " ".join(str(name).split())).strip()
    cleaned = re.sub(r"(?:\s+(?:please|thanks|thank you))+$", "", cleaned, flags=re.I).strip()
    if not cleaned:
        return ""
    return " ".join(w if (any(c.isupper() for c in w) and any(c.islower() for c in w)) else w.title()
                    for w in cleaned.split())


def get_user_name():
    """Return the user's saved name from persistent memory (or config), if known."""
    m = _memory()
    if m:
        for key in ("user_name", "name", "first_name"):
            row = next((f for f in m.all_facts(limit=30) if f["key"] == key and f["value"].strip()), None)
            if row:
                return row["value"].strip()
    return str(_config().get("user_name") or "").strip()


def set_user_name(name):
    """Save the user's preferred name into persistent memory (or config fallback)."""
    clean_name = _format_person_name(name)
    if not clean_name:
        return "What name should I call you?"
    m = _memory()
    if m:
        m.remember("user_name", clean_name, category="profile")
    else:
        cfg = _config()
        cfg["user_name"] = clean_name
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return f"Got it, {clean_name}. I'll remember your name."


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
    _tool("set_reminder", "Speak a reminder to the user later (optionally recurring).",
          {"minutes_from_now": "number", "message": "string", "repeat_minutes?": "number"}),
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
    _tool("system_diagnostics", "Detailed live diagnostics: RAM usage, uptime, battery, and disk space.", {}),
    _tool("top_processes", "List the running apps using the most RAM.", {"count?": "number"}),
    _tool("read_webpage", "Fetch readable text from an http or https URL.", {"url": "string"}),
    _tool("run_routine", "Execute a saved multi-step routine by name (e.g. focus mode, break time).", {"name": "string"}),
    _tool("save_routine", "Create or update a named multi-step routine.", {"name": "string", "steps_csv": "string"}),
    _tool("list_routines", "List available routines.", {}),
    _tool("delete_routine", "Delete a custom routine by name.", {"name": "string"}),
    _tool("git_status", "Check git branch and uncommitted changes in a repository.", {"repo?": "string"}),
    _tool("github_status", "Check GitHub PRs, issues, and notifications via gh CLI.", {}),
    _tool("list_calendar", "Read upcoming calendar events for today, tomorrow, or week.", {"period?": "string"}),
    _tool("add_calendar_event", "Add an event to the local calendar.",
          {"title": "string", "start_iso": "string", "duration_mins?": "number"}),
    _tool("quick_lookup", "Look up a concise factual summary for a topic or person.", {"topic": "string"}),
    _tool("take_screenshot", "Save a screenshot of the screen.", {}),
]
FUNCS = {
    "open_app_or_website": lambda name: open_thing(name, interactive=False) or "Nothing to open.",
    "web_search": web_search,
    "set_volume": lambda percent: volume_set(percent),
    "media_control": media,
    "set_timer": lambda seconds: set_timer(float(seconds)),
    "set_reminder": lambda minutes_from_now, message, repeat_minutes=0: (
        add_reminder(time.time() + float(minutes_from_now) * 60, message, "reminder",
                     float(repeat_minutes) * 60 if repeat_minutes else 0),
        "Recurring reminder set." if repeat_minutes else "Reminder set.")[1],
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
    "system_diagnostics": system_diagnostics,
    "top_processes": lambda count=3: top_processes(int(count)),
    "read_webpage": read_webpage,
    "run_routine": run_routine,
    "save_routine": lambda name, steps_csv: save_routine(name, [s.strip() for s in re.split(r"[,;\n]+", steps_csv)]),
    "list_routines": list_routines,
    "delete_routine": delete_routine,
    "git_status": git_status,
    "github_status": github_status,
    "list_calendar": lambda period="today": cal_mod.events_summary(period if period in ("today", "tomorrow", "week") else "today") if cal_mod else "Calendar module unavailable.",
    "add_calendar_event": lambda title, start_iso, duration_mins=60: (
        (cal_mod.add_event(title, datetime.fromisoformat(start_iso), duration_mins), f"Added {title} to your calendar.")[1]
        if cal_mod else "Calendar module unavailable."),
    "quick_lookup": lambda topic: quick_lookup(topic) or "I couldn't find a quick summary for that.",
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
    uname = get_user_name()
    if uname:
        base += f"\nThe user's name is {uname}."
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


def _http_stream(url, payload, headers=None, timeout=45):
    """Yield decoded text lines from a streaming HTTP POST (SSE or NDJSON)."""
    hdrs = {"User-Agent": "Nova/1.0", **(headers or {})}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {err.code}: {body[:300]}") from err


class _SentenceEmitter:
    """Incrementally buffers streamed LLM text tokens and emits complete spoken sentences."""

    def __init__(self, emit):
        self.emit = emit
        self.buf = ""
        self.all = []

    def feed(self, token):
        if not token:
            return
        self.all.append(token)
        self.buf += token
        while True:
            m = re.match(r"^(.{24,}?[.!?])(?:\s+|\n+)(.*)$", self.buf, re.S)
            if m and not re.search(r"\b\d+\.$", m.group(1)):
                sent = " ".join(m.group(1).split())
                self.buf = m.group(2)
                if sent:
                    self.emit(sent)
                continue
            if "\n" in self.buf:
                head, _, rest = self.buf.partition("\n")
                if len(head.strip()) >= 24:
                    self.buf = rest
                    self.emit(" ".join(head.split()))
                    continue
            if len(self.buf) >= 160:
                cut = self.buf.rfind(" ", 80, 155)
                if cut > 0:
                    head = " ".join(self.buf[:cut].split())
                    self.buf = self.buf[cut + 1:]
                    if head:
                        self.emit(head)
                    continue
            break

    def flush(self):
        rem = " ".join(self.buf.split())
        self.buf = ""
        if rem:
            self.emit(rem)
        return " ".join("".join(self.all).split()).strip() or "Done."


def _ask_anthropic_stream(text, key, model, base_url, on_sentence, stop_event=None):
    msgs = _history + [{"role": "user", "content": text}]
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    for _ in range(5):
        emitter = _SentenceEmitter(on_sentence)
        blocks = {}
        stop_reason = None
        for line in _http_stream(f"{base_url}/messages",
                                 {"model": model, "max_tokens": 400, "system": _system(),
                                  "tools": TOOLS, "messages": msgs, "stream": True}, headers):
            if stop_event and stop_event.is_set():
                return " ".join("".join(emitter.all).split()).strip()
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            ev = json.loads(raw)
            t = ev.get("type")
            if t == "content_block_start":
                idx = ev["index"]
                b = dict(ev["content_block"])
                if b.get("type") == "tool_use":
                    b["_raw_json"] = ""
                blocks[idx] = b
            elif t == "content_block_delta":
                idx = ev["index"]
                d = ev.get("delta", {})
                if d.get("type") == "text_delta":
                    emitter.feed(d.get("text", ""))
                elif d.get("type") == "input_json_delta" and idx in blocks:
                    blocks[idx]["_raw_json"] += d.get("partial_json", "")
            elif t == "message_delta":
                stop_reason = ev.get("delta", {}).get("stop_reason") or stop_reason
        if stop_reason != "tool_use":
            return emitter.flush()
        full_blocks = []
        results = []
        for idx in sorted(blocks):
            b = blocks[idx]
            if b.get("type") == "tool_use":
                raw_j = b.pop("_raw_json", "")
                args = json.loads(raw_j) if raw_j.strip() else b.get("input", {})
                b["input"] = args
                full_blocks.append(b)
                results.append({"type": "tool_result", "tool_use_id": b["id"], "content": _run_tool(b["name"], args)})
            else:
                full_blocks.append(b)
        msgs.append({"role": "assistant", "content": full_blocks})
        msgs.append({"role": "user", "content": results})
    return "Done."


def _ask_openai_stream(text, key, model, base_url, on_sentence, stop_event=None):
    msgs = [{"role": "system", "content": _system()}] + _history + [{"role": "user", "content": text}]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    tools = _openai_tools()
    use_tools = True
    for _ in range(5):
        payload = {"model": model, "messages": msgs, "max_tokens": 400, "stream": True}
        if use_tools:
            payload["tools"] = tools
        emitter = _SentenceEmitter(on_sentence)
        tool_acc = {}
        try:
            for line in _http_stream(f"{base_url}/chat/completions", payload, headers, timeout=45):
                if stop_event and stop_event.is_set():
                    return " ".join("".join(emitter.all).split()).strip()
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                ev = json.loads(raw)
                choices = ev.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tool_acc.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
                if delta.get("content") and not tool_acc:
                    emitter.feed(delta["content"])
        except RuntimeError as err:
            if use_tools and ("does not support tools" in str(err).lower() or ("tools" in str(err).lower() and "400" in str(err))):
                use_tools = False
                continue
            raise
        if not tool_acc:
            return emitter.flush()
        calls = [tool_acc[i] for i in sorted(tool_acc)]
        msgs.append({"role": "assistant", "content": "".join(emitter.all) or None, "tool_calls": calls})
        for tc in calls:
            fn = tc["function"]
            args_raw = fn.get("arguments") or "{}"
            args = json.loads(args_raw) if isinstance(args_raw, str) and args_raw.strip() else {}
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": _run_tool(fn["name"], args)})
    return "Done."


def _ask_ollama_stream(text, model, base_url, on_sentence, stop_event=None):
    msgs = [{"role": "system", "content": _system()}] + _history + [{"role": "user", "content": text}]
    headers = {"Content-Type": "application/json"}
    tools = _openai_tools()
    use_tools = True
    for _ in range(5):
        payload = {"model": model, "messages": msgs, "stream": True}
        if use_tools:
            payload["tools"] = tools
        emitter = _SentenceEmitter(on_sentence)
        tool_calls = []
        try:
            for line in _http_stream(f"{base_url}/api/chat", payload, headers, timeout=60):
                if stop_event and stop_event.is_set():
                    return " ".join("".join(emitter.all).split()).strip()
                if not line.strip():
                    continue
                ev = json.loads(line)
                msg = ev.get("message") or {}
                if msg.get("tool_calls"):
                    tool_calls.extend(msg["tool_calls"])
                elif msg.get("content") and not tool_calls:
                    emitter.feed(msg["content"])
        except RuntimeError as err:
            if use_tools and "does not support tools" in str(err).lower():
                use_tools = False
                continue
            raise
        if not tool_calls:
            return emitter.flush()
        msgs.append({"role": "assistant", "content": "".join(emitter.all), "tool_calls": tool_calls})
        for tc in tool_calls:
            fn = tc["function"]
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args) if args.strip() else {}
            msgs.append({"role": "tool", "content": _run_tool(fn["name"], args)})
    return "Done."


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


def _record_ai_error(provider, err):
    global _ai_blocked_until, _ai_blocked_msg
    msg = _ai_error_reply(provider, err)
    err_text = str(err)
    if any(code in err_text for code in ("HTTP 402", "HTTP 401", "HTTP 403")):
        _ai_blocked_until = time.monotonic() + _AI_COOLDOWN_SECS
        _ai_blocked_msg = msg
    return msg


def ask_ai(text, on_sentence=None, stop_event=None):
    """Returns Nova's reply, or None if the AI brain isn't set up or is in billing/auth cooldown."""
    ready, provider, model = ai_status()
    if not ready or ai_blocked():
        return None
    _, key, _, base_url = _ai_settings()
    try:
        if on_sentence is not None:
            if provider == "anthropic":
                reply = _ask_anthropic_stream(text, key, model, base_url, on_sentence, stop_event)
            elif provider in ("openai", "ollama_cloud"):
                reply = _ask_openai_stream(text, key, model, base_url, on_sentence, stop_event)
            elif provider == "ollama":
                reply = _ask_ollama_stream(text, model, base_url, on_sentence, stop_event)
            elif provider == "gemini":
                reply = _ask_gemini(text, key, model, base_url)
                em = _SentenceEmitter(on_sentence)
                em.feed(reply)
                em.flush()
            else:
                return f"Unknown AI provider: {provider}."
        elif provider == "anthropic":
            reply = _ask_anthropic(text, key, model, base_url)
        elif provider in ("openai", "ollama_cloud"):
            reply = _ask_openai(text, key, model, base_url)
        elif provider == "gemini":
            reply = _ask_gemini(text, key, model, base_url)
        elif provider == "ollama":
            reply = _ask_ollama(text, model, base_url)
        else:
            return f"Unknown AI provider: {provider}."
        reset_ai_cooldown()
        if stop_event and stop_event.is_set() and not reply:
            return ""
        _history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
        del _history[:-8]
        m = _memory()
        if m and reply:
            m.add_episode(text, reply)
        return reply
    except Exception as err:
        print("AI error:", err)
        msg = _record_ai_error(provider, err)
        if on_sentence is not None and not (stop_event and stop_event.is_set()):
            on_sentence(msg)
        return msg


def ask_ai_vision(question, image_b64, mime="image/jpeg"):
    """Answer a question about a captured screen image across the configured AI provider."""
    ready, provider, model = ai_status()
    if not ready:
        return AI_HINT
    if ai_blocked():
        return _ai_blocked_msg or "My AI brain is unavailable until the account issue is resolved."
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
        reset_ai_cooldown()
        return reply
    except Exception as err:
        print("AI vision error:", err)
        return _record_ai_error(provider, err)


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
    reset_ai_cooldown()
    print(f"Saved to {CONFIG_FILE}. Testing...")
    print(ask_ai("Say hello in five words.") or "No reply.")


def ai_about_clipboard(raw):
    text = clipboard()
    if not text:
        return "Your clipboard is empty."
    if not ai_ready():
        return AI_HINT
    if ai_blocked():
        return _ai_blocked_msg or "My AI brain is unavailable until the account issue is resolved."
    return ask_ai(f"{raw}\n\nClipboard contents:\n{text[:6000]}") or "My AI brain is unavailable right now."


# ============ offline math & unit conversion ============
_LENGTH_UNITS = {
    "mm": ("millimeters", 0.001), "millimeter": ("millimeters", 0.001), "millimeters": ("millimeters", 0.001),
    "cm": ("centimeters", 0.01), "centimeter": ("centimeters", 0.01), "centimeters": ("centimeters", 0.01),
    "m": ("meters", 1.0), "meter": ("meters", 1.0), "meters": ("meters", 1.0),
    "km": ("kilometers", 1000.0), "kilometer": ("kilometers", 1000.0), "kilometers": ("kilometers", 1000.0),
    "in": ("inches", 0.0254), "inch": ("inches", 0.0254), "inches": ("inches", 0.0254),
    "ft": ("feet", 0.3048), "foot": ("feet", 0.3048), "feet": ("feet", 0.3048),
    "yd": ("yards", 0.9144), "yard": ("yards", 0.9144), "yards": ("yards", 0.9144),
    "mi": ("miles", 1609.344), "mile": ("miles", 1609.344), "miles": ("miles", 1609.344),
}
_WEIGHT_UNITS = {
    "mg": ("milligrams", 0.001), "milligram": ("milligrams", 0.001), "milligrams": ("milligrams", 0.001),
    "g": ("grams", 1.0), "gram": ("grams", 1.0), "grams": ("grams", 1.0),
    "kg": ("kilograms", 1000.0), "kilo": ("kilograms", 1000.0), "kilos": ("kilograms", 1000.0),
    "kilogram": ("kilograms", 1000.0), "kilograms": ("kilograms", 1000.0),
    "oz": ("ounces", 28.349523125), "ounce": ("ounces", 28.349523125), "ounces": ("ounces", 28.349523125),
    "lb": ("pounds", 453.59237), "lbs": ("pounds", 453.59237),
    "pound": ("pounds", 453.59237), "pounds": ("pounds", 453.59237),
}
_DATA_UNITS = {
    "b": ("bytes", 1.0), "byte": ("bytes", 1.0), "bytes": ("bytes", 1.0),
    "kb": ("kilobytes", 1024.0), "kilobyte": ("kilobytes", 1024.0), "kilobytes": ("kilobytes", 1024.0),
    "mb": ("megabytes", 1024.0 ** 2), "megabyte": ("megabytes", 1024.0 ** 2), "megabytes": ("megabytes", 1024.0 ** 2),
    "gb": ("gigabytes", 1024.0 ** 3), "gigabyte": ("gigabytes", 1024.0 ** 3), "gigabytes": ("gigabytes", 1024.0 ** 3),
    "tb": ("terabytes", 1024.0 ** 4), "terabyte": ("terabytes", 1024.0 ** 4), "terabytes": ("terabytes", 1024.0 ** 4),
}
_VOLUME_UNITS = {
    "ml": ("milliliters", 1.0), "milliliter": ("milliliters", 1.0), "milliliters": ("milliliters", 1.0),
    "l": ("liters", 1000.0), "liter": ("liters", 1000.0), "liters": ("liters", 1000.0),
    "cup": ("cups", 236.5882365), "cups": ("cups", 236.5882365),
    "gal": ("gallons", 3785.411784), "gallon": ("gallons", 3785.411784), "gallons": ("gallons", 3785.411784),
}
_SPEED_UNITS = {
    "mph": ("miles per hour", 0.44704),
    "kph": ("kilometers per hour", 1.0 / 3.6),
    "kmh": ("kilometers per hour", 1.0 / 3.6),
}
_TEMP_UNITS = {
    "c": "Celsius", "celsius": "Celsius",
    "f": "Fahrenheit", "fahrenheit": "Fahrenheit",
    "k": "Kelvin", "kelvin": "Kelvin",
}
_AST_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_AST_UNOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _fmt_num(val):
    if abs(val - round(val)) < 1e-9:
        return str(int(round(val)))
    return f"{val:.4f}".rstrip("0").rstrip(".")


def _convert_units(low):
    norm = re.sub(r"\bdegrees?\s+(celsius|fahrenheit|kelvin|c|f|k)\b", r"\1", low)
    norm = norm.replace("miles per hour", "mph").replace("kilometers per hour", "kph").replace("km/h", "kph")
    norm = re.sub(r"[?,!]", "", norm).strip()
    m = re.match(
        r"^(?:convert\s+|what(?:'s| is)\s+|how many\s+\S+\s+(?:are\s+)?in\s+)?"
        r"(-?\d+(?:\.\d+)?)\s*°?\s*([a-z]+)\s+(?:to|in|into)\s+°?\s*([a-z]+)$",
        norm,
    )
    if not m:
        return None
    val, src, dst = float(m.group(1)), m.group(2), m.group(3)
    if src in _TEMP_UNITS and dst in _TEMP_UNITS:
        c = val if src[0] == "c" else ((val - 32.0) * 5.0 / 9.0 if src[0] == "f" else val - 273.15)
        out = c if dst[0] == "c" else (c * 9.0 / 5.0 + 32.0 if dst[0] == "f" else c + 273.15)
        s_lbl = ("degrees " + _TEMP_UNITS[src]) if src[0] in ("c", "f") else _TEMP_UNITS[src]
        d_lbl = ("degrees " + _TEMP_UNITS[dst]) if dst[0] in ("c", "f") else _TEMP_UNITS[dst]
        return f"{_fmt_num(val)} {s_lbl} is {_fmt_num(out)} {d_lbl}."
    for table in (_LENGTH_UNITS, _WEIGHT_UNITS, _DATA_UNITS, _VOLUME_UNITS, _SPEED_UNITS):
        if src in table and dst in table:
            s_lbl, s_fac = table[src]
            d_lbl, d_fac = table[dst]
            out = val * s_fac / d_fac
            return f"{_fmt_num(val)} {s_lbl} is {_fmt_num(out)} {d_lbl}."
    return None


def _eval_ast_node(node):
    if isinstance(node, ast.Expression):
        return _eval_ast_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _AST_UNOPS:
        return _AST_UNOPS[type(node.op)](_eval_ast_node(node.operand))
    if isinstance(node, ast.BinOp):
        left, right = _eval_ast_node(node.left), _eval_ast_node(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > 1000 or abs(left) > 1e6:
                raise ValueError("exponent too large")
            return left ** right
        if type(node.op) in _AST_BINOPS:
            return _AST_BINOPS[type(node.op)](left, right)
    raise ValueError("unsupported expression")


def eval_math_or_convert(t, raw=""):
    """Safely evaluate basic math, percentages, square roots, and unit conversions offline."""
    low = (raw or t or "").lower().strip()
    if conv := _convert_units(low):
        return conv
    if m := re.search(r"(-?\d+(?:\.\d+)?)\s*(?:%|percent)\s+of\s+(-?\d+(?:\.\d+)?)", low):
        x, y = float(m.group(1)), float(m.group(2))
        return f"{_fmt_num(x)} percent of {_fmt_num(y)} is {_fmt_num(x / 100.0 * y)}."
    if m := re.search(r"(-?\d+(?:\.\d+)?)\s*(?:%|percent)\s+off\s+(-?\d+(?:\.\d+)?)", low):
        x, y = float(m.group(1)), float(m.group(2))
        return f"{_fmt_num(x)} percent off {_fmt_num(y)} is {_fmt_num(y * (1.0 - x / 100.0))}."
    if m := re.search(r"\b(?:the\s+)?square\s+root\s+of\s+(-?\d+(?:\.\d+)?)", low):
        x = float(m.group(1))
        if x < 0:
            return "Cannot take the real square root of a negative number."
        return f"The square root of {_fmt_num(x)} is {_fmt_num(math.sqrt(x))}."
    expr = re.sub(r"^(?:calculate|compute|evaluate|solve|what(?:'s| is)|how much is)\s+", "", low)
    expr = re.sub(r"[?,!.\s]+$", "", expr)
    for phrase, sym in (("divided by", "/"), ("multiplied by", "*"), ("to the power of", "**"),
                        ("raised to", "**"), ("plus", "+"), ("minus", "-"), ("times", "*"),
                        ("over", "/"), ("modulo", "%"), ("mod", "%"), ("^", "**")):
        expr = expr.replace(phrase, f" {sym} ")
    expr = re.sub(r"(?<=\d)\s*x\s*(?=\d)", " * ", expr)
    if not re.fullmatch(r"[\d\s+\-*/%().]+", expr) or not re.search(r"\d\s*(?:\*\*|[+\-*/%])\s*[+\-]?\s*[\d(]", expr):
        return None
    try:
        val = _eval_ast_node(ast.parse(expr, mode="eval"))
        return f"The answer is {_fmt_num(val)}."
    except ZeroDivisionError:
        return "You can't divide by zero."
    except Exception:
        return None


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
    # --- routines, mic calibration & dev commands ---
    if re.search(r"\b(recalibrate|calibrate)\b.*\b(mic|microphone|noise)\b", t):
        return ctx["recalibrate"]() if "recalibrate" in ctx else "Microphone calibration is only available in voice mode."
    if re.search(r"\b(list|show|what)\b.*\broutines?\b", t):
        return list_routines()
    if m := re.match(r"(?:delete|remove)\s+routine\s+(.+)", t):
        return delete_routine(m.group(1))
    if m := re.match(r"(?:create|save|add|make)\s+(?:a\s+)?routine\s+(?:called\s+|named\s+)?(.+?)\s+to\s+(.+)", t):
        steps = [s.strip() for s in re.split(r"\s+(?:and then|then|and)\s+|[,;]+", m.group(2)) if s.strip()]
        return save_routine(m.group(1), steps)
    if m := re.match(r"(?:run|start)\s+(?:the\s+)?routine\s+(.+)", t):
        return run_routine(m.group(1))
    if re.sub(r"^(?:start|run)\s+", "", t) in load_routines():
        return run_routine(re.sub(r"^(?:start|run)\s+", "", t))
    if m := re.search(r"\bgit status(?:\s+(?:in|for)\s+(.+))?$|\bwhat branch am i on\b", t):
        return git_status((m.group(1) or "") if m.lastindex else "")
    if re.search(r"\b(github status|check github|my pull requests)\b", t):
        return github_status()
    if m := re.search(r"(?:^|\s)(?:run|execute)\s+(?:the\s+)?command\s+(.+)$", raw.strip(), re.I):
        cmd = m.group(1).strip()
        return confirm(f"Run command {cmd}?", lambda: _exec_shell(cmd))

    # --- calendar & agenda ---
    if cal_mod and re.search(r"\b(calendar|schedule|agenda|meetings?|events?|appointments?)\b", t) \
            and re.search(r"\b(what|what's|any|list|show|check|my|today|tomorrow|week)\b", t):
        period = "week" if "week" in t else "tomorrow" if "tomorrow" in t else "today"
        return cal_mod.events_summary(period)
    if cal_mod and re.match(r"(?:add|schedule|create|book)\s+(?:a\s+|an\s+)?(?:calendar\s+)?(?:event|meeting|appointment)\b", t):
        c = parse_clock(re.sub(r"\bfor\b", "at", t, count=1))
        if c:
            dt, (a, b) = c
            title = re.sub(r"^(?:add|schedule|create|book)\s+(?:a\s+|an\s+)?(?:calendar\s+)?(?:event|meeting|appointment)\s*(?:called|named|for)?\s*", "", (t[:a] + " " + t[b:]).strip())
            title = re.sub(r"\b(?:tomorrow|today)\b", "", title).strip()
            if title:
                cal_mod.add_event(title, dt)
                return f"Added {title} to your calendar {clock_text(dt)}."

    # --- user identity, memory, screen & active window ---
    if re.search(r"\b(?:what(?:'s| is) my name|who am i|do you know my name)\b", t):
        uname = get_user_name()
        return f"Your name is {uname}." if uname else "You haven't told me your name yet. Just say my name is, followed by your name."
    if m := (re.match(r"remember (?:that )?my name is ([a-z][a-z'\- ]{1,30})$", t)
             or re.match(r"(?:actually |no )?my name is ([a-z][a-z'\- ]{1,30})$", t)
             or re.match(r"call me ([a-z][a-z'\- ]{1,25})$", t)):
        cand = m.group(1).strip()
        bad = {"back", "later", "soon", "when", "if", "not", "sure", "okay", "fine", "tired", "here", "ready"}
        if cand.split()[0] not in bad and not re.search(r"\b(?:is|are|was|the|not|working)\b", cand):
            return set_user_name(cand)
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
        if re.search(r"\b(clear|empty)\b", t):
            return clear_clipboard()
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
        return volume_mute(False if re.search(r"\bunmute\b", t) else True)
    if re.search(r"\bvolume\b|\blouder\b|\bquieter\b|\bsofter\b|\bturn (it|the (sound|music)) (up|down)\b", t):
        if re.search(r"\b(what|what's|check|current|how loud|tell me)\b.*\bvolume\b", t):
            return volume_status()
        n, by = re.search(r"\b(\d{1,3})\b", t), re.search(r"\bby (\d{1,3})\b", t)
        up = re.search(r"\b(up|higher|increase|raise|louder)\b", t)
        down = re.search(r"\b(down|lower|decrease|reduce|quieter|softer)\b", t)
        if re.search(r"\b(max|maximum|full)\b", t):
            return volume_set(100)
        if n and not by and (not (up or down) or re.search(r"\b(to|at)\s+\d", t)):
            return volume_set(int(n.group(1)))
        return volume_change("down" if down else "up", int(by.group(1)) if by else 10)
    if re.search(r"\bbrightness\b|\bbrighter\b|\bdimmer\b|\bdim the screen\b", t):
        if re.search(r"\b(what|what's|check|current|tell me)\b.*\bbrightness\b", t):
            return brightness_status()
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
    for pat, action in (
        (r"\bmaximi[sz]e\b.*\bwindow\b", "maximize"),
        (r"\bminimi[sz]e\b.*\bwindow\b", "minimize"),
        (r"\b(?:snap|move|tile)\b.*\bwindow\b.*\bleft\b", "snap_left"),
        (r"\b(?:snap|move|tile)\b.*\bwindow\b.*\bright\b", "snap_right"),
        (r"^(?:switch|next|alt tab) window$|^switch windows$", "switch_window"),
        (r"^(?:open |create )?(?:a )?new tab$", "new_tab"),
        (r"^close (?:this |the |current )?tab$", "close_tab"),
        (r"^(?:reopen|restore) (?:the )?(?:last |closed )?tab$", "reopen_tab"),
        (r"^(?:next|switch) tab$", "next_tab"),
        (r"^(?:previous|prev|last) tab$", "prev_tab"),
        (r"^(?:refresh|reload)(?: (?:this|the|current))?(?: page| tab| window)?$", "refresh"),
        (r"^find on (?:this )?page$", "find_on_page"),
        (r"^select all$", "select_all"),
        (r"^copy(?: that| this| selection)?$", "copy"),
        (r"^paste(?: that| this| here| clipboard)?$", "paste"),
        (r"^cut (?:that|this|selection)$", "cut"),
        (r"^undo (?:typing|last edit|edit|change)$", "undo_edit"),
        (r"^redo(?: typing| that| last)?$", "redo"),
        (r"^scroll down$", "scroll_down"),
        (r"^scroll up$", "scroll_up"),
        (r"^(?:scroll|go) (?:to )?(?:the )?top$", "scroll_top"),
        (r"^(?:scroll|go) (?:to )?(?:the )?bottom$", "scroll_bottom"),
    ):
        if re.search(pat, t):
            return window_action(action)
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
    if re.search(r"\b(ip address|local ip|my ip)\b", t):
        return local_ip()
    if re.search(r"\b(am i online|internet connection|check internet|is the internet)\b", t):
        return internet_status()
    if re.search(r"\b(what|which|list|show)\b.*\b(apps|programs|processes)\b.*\b(running|open)\b|\bwhat's running\b", t):
        return running_apps_text()
    if re.search(r"\b(summari[sz]e|read|explain)\b.*\b(link|url|webpage|web page|article)\b", t):
        m = re.search(r"https?://\S+", raw)
        url = m.group(0) if m else clipboard().strip()
        if not url.startswith(("http://", "https://")):
            return "Copy a link to your clipboard or include the URL, and I'll read it."
        page = read_webpage(url)
        if not ai_ready() or ai_blocked():
            return page[:300]
        return ask_ai(f"Summarize this webpage concisely in two or three spoken sentences:\n\n{page}") or page[:300]
    if re.search(r"\b(top (?:apps|processes)|using (?:the )?most (?:ram|memory)|slowing down my (?:pc|laptop|computer))\b", t):
        return top_processes()
    if re.search(r"\b(system diagnostics?|pc health|check my pc|memory usage|how much ram|uptime|how long has my (?:pc|laptop|computer) been on)\b", t):
        return system_diagnostics()
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
    if ans := eval_math_or_convert(t, raw):
        return ans
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
