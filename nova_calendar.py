"""
Nova's calendar & agenda module: parses standard iCalendar (.ics) feeds/files and local events.
Zero external packages. Configure CALENDAR_ICS_URL in .env (optional) or add events by voice.
"""
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

DATA = Path.home() / ".laptop_assistant"
DATA.mkdir(exist_ok=True)
EVENTS_FILE = DATA / "events.json"
CAL_DIR = DATA / "calendars"
CONFIG_FILE = DATA / "config.json"

_lock = threading.Lock()
_ics_cache = {"url": "", "ts": 0.0, "events": []}
_alerted = set()


def _unfold_ics(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def _parse_ics_dt(raw_prop, val):
    """Parse an ICS DTSTART/DTEND value into a local naive datetime, or None."""
    val = val.strip()
    if re.fullmatch(r"\d{8}", val):
        return datetime.strptime(val, "%Y%m%d")
    if re.fullmatch(r"\d{8}T\d{6}Z", val):
        dt = datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone().replace(tzinfo=None)
    if re.fullmatch(r"\d{8}T\d{6}", val):
        return datetime.strptime(val, "%Y%m%dT%H%M%S")
    return None


def _unescape_ics(s):
    return s.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def parse_ics(text):
    """Parse VEVENT entries from an iCalendar (.ics) string into normalized dicts."""
    events = []
    cur = None
    for line in _unfold_ics(text).splitlines():
        line = line.strip()
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            if cur.get("title") and cur.get("start"):
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            prop, val = line.split(":", 1)
            name = prop.split(";", 1)[0].upper()
            if name == "SUMMARY":
                cur["title"] = _unescape_ics(val)
            elif name == "LOCATION":
                cur["location"] = _unescape_ics(val)
            elif name == "UID":
                cur["uid"] = val.strip()
            elif name == "DTSTART":
                dt = _parse_ics_dt(prop, val)
                if dt:
                    cur["start"] = dt.timestamp()
                    cur["all_day"] = "VALUE=DATE" in prop.upper() or len(val.strip()) == 8
            elif name == "DTEND":
                dt = _parse_ics_dt(prop, val)
                if dt:
                    cur["end"] = dt.timestamp()
    return events


def _ics_url():
    if os.environ.get("CALENDAR_ICS_URL"):
        return os.environ["CALENDAR_ICS_URL"].strip()
    try:
        return (json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("calendar_ics_url") or "").strip()
    except Exception:
        return ""


def _fetch_remote_events():
    url = _ics_url()
    if not url or not url.startswith(("http://", "https://", "webcal://")):
        return []
    if url.startswith("webcal://"):
        url = "https://" + url[9:]
    now = time.time()
    if _ics_cache["url"] == url and now - _ics_cache["ts"] < 600:
        return list(_ics_cache["events"])
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Nova/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            text = resp.read(2_000_000).decode("utf-8", errors="replace")
        evs = parse_ics(text)
        _ics_cache.update({"url": url, "ts": now, "events": evs})
        return list(evs)
    except Exception:
        return list(_ics_cache["events"]) if _ics_cache["url"] == url else []


def _load_local_events():
    events = []
    try:
        data = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            events.extend(data)
    except Exception:
        pass
    if CAL_DIR.exists():
        for p in CAL_DIR.glob("*.ics"):
            try:
                events.extend(parse_ics(p.read_text(encoding="utf-8", errors="replace")))
            except Exception:
                pass
    return events


def all_events():
    events = _load_local_events() + _fetch_remote_events()
    events.sort(key=lambda e: e.get("start", 0))
    return events


def add_event(title, start_dt, duration_mins=60, location=""):
    title = " ".join(title.strip().split())
    if not title:
        return None
    start_ts = start_dt.timestamp()
    entry = {
        "uid": f"local-{int(start_ts)}-{abs(hash(title)) % 100000}",
        "title": title,
        "start": start_ts,
        "end": start_ts + max(5, int(duration_mins)) * 60,
        "location": location.strip(),
        "all_day": False,
    }
    with _lock:
        try:
            items = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                items = []
        except Exception:
            items = []
        items.append(entry)
        EVENTS_FILE.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return entry


def events_between(start_dt, end_dt):
    a, b = start_dt.timestamp(), end_dt.timestamp()
    return [e for e in all_events() if a <= e.get("start", 0) < b]


def events_summary(period="today"):
    now = datetime.now()
    today0 = datetime.combine(now.date(), dtime(0))
    if period == "tomorrow":
        a, b, label = today0 + timedelta(days=1), today0 + timedelta(days=2), "tomorrow"
    elif period == "week":
        a, b, label = now, today0 + timedelta(days=7), "this week"
    else:
        a, b, label = now - timedelta(minutes=5), today0 + timedelta(days=1), "today"
    evs = events_between(a, b)
    if not evs:
        return f"You have no calendar events {label}."
    parts = []
    for e in evs[:5]:
        dt = datetime.fromtimestamp(e["start"])
        when = "all day" if e.get("all_day") else dt.strftime("%I:%M %p").lstrip("0")
        if period == "week":
            when = f"{dt:%A} at {when}" if not e.get("all_day") else f"{dt:%A}"
        loc = f" at {e['location']}" if e.get("location") else ""
        parts.append(f"{e['title']} {when}{loc}")
    return f"Your schedule {label}: " + "; ".join(parts) + "."


def due_meeting_alerts(window_mins=10):
    """Return list of spoken alert strings for events starting within `window_mins` (once per event)."""
    now = time.time()
    horizon = now + window_mins * 60
    alerts = []
    for e in all_events():
        if e.get("all_day"):
            continue
        start = e.get("start", 0)
        if now <= start <= horizon:
            key = e.get("uid") or f"{e.get('title')}|{int(start)}"
            if key not in _alerted:
                _alerted.add(key)
                mins = max(1, int(round((start - now) / 60)))
                alerts.append(f"Heads up, {e['title']} starts in {mins} minute{'s' * (mins != 1)}.")
    return alerts
