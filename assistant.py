#!/usr/bin/env python3
"""
Nova: a voice assistant that lives on your laptop (offline).
Say "Nova" -> it answers out loud, then you talk back and forth.
Setup:  pip install faster-whisper sounddevice numpy pyttsx3
Extras: pip install edge-tts sherpa-onnx pillow
Run:    python assistant.py              (voice)
        python assistant.py --text       (typing, no microphone)
        python assistant.py --mictest    (check your microphone)
        python assistant.py --voicetest  (check that Nova can speak)
        python assistant.py --enroll     (only listen to your voice; needs sherpa-onnx)
        python assistant.py --background (hidden mode used at startup, logs to nova.log)
        python assistant.py --no-overlay (voice without the popup window)
        python assistant.py --setup-ai   (connect Claude, OpenAI, Gemini, or local Ollama)
        python assistant.py --selftest   (check packages and Windows features)
"""
import atexit
import json
import math
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

NAME = "Nova"
WAKE_ALIASES = {"nova", "novah", "novo", "nover", "no va", "nowa"}  # how Whisper may write it
MODEL = "small.en"        # more accurate; use "base.en" if Nova feels slow
COMMAND_HINT = ("Add a task. List my tasks. Mark task one as done. Delete task two. "
                "Organize my downloads. Find the invoice file. Undo. What time is it?")
SENSITIVITY = 1.0        # raise (1.5, 2.0) to ignore more background noise; lower if Nova misses you
STAY_AWAKE = True        # False = say 'Nova' before every command (fewer accidental pickups)
VOICE_ENGINE = "auto"    # "auto" = British neural voice + SAPI backup; "neural", "sapi", or "basic"
VOICE_NAME = "en-GB-RyanNeural"
VOICE_LOCK = True        # True = once enrolled (--enroll), ignore other people's voices
HOTKEY = "ctrl+shift+space"  # global Windows hotkey to wake Nova or interrupt speech (None to disable)
ISLAND_RENDERER = "auto"    # "auto" = smooth edges if possible; "classic" = simple window (use if the island misbehaves)
ISLAND_PILL = False        # True = put the orb and text inside a black Dynamic Island pill; False = they float on your desktop
SHOW_TEXT = False          # False = only the orb appears and Nova speaks its replies; True = also show what you said and the reply
ORB_BODY_ALPHA = 0.5       # without the pill: how solid the orb's dark glass is (0 = only the bright bands, 1 = solid)
ISLAND_SCALE = 1.0        # make the Dynamic Island bigger (1.25, 1.5) on high-resolution screens
SESSION_TIMEOUT = 8      # seconds of silence before it goes back to sleep
MIC_DEVICE = None        # set to a device number if the wrong mic is used

try:
    import nova_skills as skills       # Nova's extra abilities (keep this file next to assistant.py)
except ImportError:
    skills = None

try:
    import nova_voice as voice_mod     # Jarvis-style British voice + speaker lock
except ImportError:
    voice_mod = None

try:
    import nova_memory as memory_mod   # persistent facts + cross-session memory
except ImportError:
    memory_mod = None

HOME = Path.home()
DATA = HOME / ".laptop_assistant"
TASKS_FILE = DATA / "tasks.json"
UNDO_FILE = DATA / "last_move.json"
DATA.mkdir(exist_ok=True)

CATEGORIES = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".svg", ".bmp"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt",
                  ".ppt", ".pptx", ".xls", ".xlsx", ".csv"},
    "Videos": {".mp4", ".mov", ".mkv", ".avi", ".webm"},
    "Audio": {".mp3", ".wav", ".m4a", ".flac", ".ogg"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "Code": {".py", ".js", ".ts", ".html", ".css", ".json", ".java", ".c", ".cpp", ".sh"},
    "Installers": {".exe", ".msi", ".dmg", ".pkg", ".deb", ".apk"},
}
NUMBERS = {w: i + 1 for i, w in enumerate(
    "one two three four five six seven eight nine ten".split())}
NUMBER_ALIASES = {"to": 2, "too": 2, "won": 1}   # common mishearings of the last word

awaiting = None          # what the assistant is waiting for you to answer
_tts_warned = False


class EndSession(Exception):
    pass


class NullUI:
    """Stand-in used when there is no popup window (text mode, --no-overlay)."""
    level = 0.0

    def post(self, kind, text=""):
        pass


UI = NullUI()


# ---------- speaking ----------
_failed = set()
_speech_lock = threading.Lock()
_speaking = threading.Event()
_wake_event = threading.Event()
_voice = None


def _get_voice():
    global _voice
    if _voice is None and voice_mod and VOICE_ENGINE != "basic":
        _voice = voice_mod.Voice(engine=VOICE_ENGINE, name=VOICE_NAME)
    return _voice


def stop_speaking():
    """Interrupt any ongoing Nova speech immediately (hotkey or click)."""
    v = _voice
    if v is not None and hasattr(v, "stop"):
        v.stop()


def _close_voice():
    if _voice is not None:
        _voice.close()


atexit.register(_close_voice)


def _speak_pyttsx3(text):
    import pyttsx3
    engine = pyttsx3.init()
    engine.setProperty("rate", 180)
    for v in engine.getProperty("voices"):
        if any(n in v.name for n in ("Zira", "Aria", "Samantha", "Female")):
            engine.setProperty("voice", v.id)
            break
    engine.say(text)
    engine.runAndWait()
    engine.stop()


def _speak_windows(text):
    """Windows' built-in voice through PowerShell: needs no extra packages."""
    script = ("Add-Type -AssemblyName System.Speech;"
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              "try { $s.SelectVoiceByHints('Female') } catch {};"
              "$s.Speak($env:NOVA_TEXT)")
    subprocess.run(["powershell", "-NoProfile", "-Command", script],
                   env={**os.environ, "NOVA_TEXT": text}, check=True, timeout=120,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,   # hidden (pythonw) runs
                   stderr=subprocess.DEVNULL,                            # have no console handles
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def say(text):
    print("🤖", text, flush=True)
    UI.post("say", text)
    with _speech_lock:
        _speaking.set()
        try:
            v = _get_voice()
            if v is not None:
                used = v.say(text)
                if used:
                    return used
            methods = [("pyttsx3", _speak_pyttsx3)]
            if os.name == "nt":
                methods.append(("windows-voice", _speak_windows))
            for name, fn in methods:
                if name in _failed:
                    continue
                try:
                    fn(text)
                    return name
                except Exception as err:
                    _failed.add(name)              # report each failure once, then skip it
                    print(f"   ({name} could not speak: {err})", flush=True)
        finally:
            _speaking.clear()
    return None


def run_voicetest():
    print("Voice test. You should hear a sentence.")
    used = say("Hello, this is Nova. If you can hear me, my voice is working.")
    print("Voice used:", used or "none, I could not speak")


# ---------- listening ----------
class Mic:
    sr = 16000

    def __init__(self, voice_lock=None):
        import numpy as np
        import sounddevice as sd
        from faster_whisper import WhisperModel
        self.np = np
        self.voice_lock = voice_lock
        self.model = WhisperModel(MODEL, compute_type="int8")
        self.stream = sd.InputStream(samplerate=self.sr, channels=1,
                                     dtype="float32", device=MIC_DEVICE)
        self.stream.start()
        self.ambient = self._level(self.stream.read(self.sr)[0])   # room noise

    def _level(self, block):
        return float(self.np.abs(block).mean())

    def listen(self, wait=8, max_seconds=15, commands=False, wake_event=None):
        """Wait up to `wait` seconds for speech; return what was said ('' if nothing)."""
        np = self.np
        while _speaking.is_set():
            time.sleep(0.05)
        avail = self.stream.read_available
        if avail:
            self.stream.read(avail)          # drop audio recorded while I was talking
        pre, chunks, started, quiet, loud = deque(maxlen=3), [], False, 0, 0
        t0 = t_start = time.time()
        while True:
            if wake_event is not None and wake_event.is_set() and not started:
                return ""
            if _speaking.is_set():           # an alert started speaking while listening: pause and reset
                while _speaking.is_set():
                    time.sleep(0.05)
                avail = self.stream.read_available
                if avail:
                    self.stream.read(avail)
                pre.clear(); chunks.clear(); started = False; quiet = loud = 0
                UI.level = 0.0
                t0 = time.time()
                continue
            block, _ = self.stream.read(self.sr // 10)
            level = self._level(block)
            UI.level = level
            if level > max(0.004, self.ambient * 3.5) * SENSITIVITY:
                if not started:
                    chunks, started, t_start = list(pre), True, time.time()
                quiet, loud = 0, loud + 1
            else:
                quiet += 1
                if not started:              # keep tracking background noise
                    self.ambient = 0.95 * self.ambient + 0.05 * level
            if started:
                chunks.append(block)
                if quiet > 10 or time.time() - t_start > max_seconds:
                    break
            else:
                pre.append(block)
                if time.time() - t0 > wait:
                    return ""
        if loud < 2:                         # a click or a cough, not speech
            return ""
        audio = np.concatenate(chunks).flatten()
        if self.voice_lock and self.voice_lock.ready and voice_mod:
            speech = voice_mod.trim_silence(audio, self.sr)
            if len(speech) == 0:
                return ""
            if len(speech) >= int(self.sr * 0.4):
                score = self.voice_lock.score(speech)
                if score is not None and score < self.voice_lock.threshold:
                    print(f"   (voice lock: ignored {score:.2f} < {self.voice_lock.threshold:.2f})")
                    return ""
        segments, _ = self.model.transcribe(
            audio, language="en", vad_filter=True, vad_parameters={"threshold": 0.3},
            beam_size=5, initial_prompt=COMMAND_HINT if commands else None)
        # keep only segments Whisper is confident are real speech
        text = " ".join(x.text for x in segments
                        if x.no_speech_prob < 0.6 and x.avg_logprob > -1.0).strip()
        if not text:
            print("   (heard a sound, but no clear words)")
        return text


# ---------- tasks ----------
def load_tasks():
    return json.loads(TASKS_FILE.read_text()) if TASKS_FILE.exists() else []


def save_tasks(tasks):
    TASKS_FILE.write_text(json.dumps(tasks, indent=2))


def find_number(text):
    words = text.split()
    for w in words:
        n = int(w) if w.isdigit() else NUMBERS.get(w)
        if n:
            return n
    return NUMBER_ALIASES.get(words[-1]) if words else None


def add_task(text):
    tasks = load_tasks()
    tasks.append({"text": text.strip(), "done": False})
    save_tasks(tasks)
    return f"Got it. I've added {text.strip()} to your list."


def list_tasks():
    open_ = [(i + 1, t) for i, t in enumerate(load_tasks()) if not t["done"]]
    if not open_:
        return "You're all caught up. No open tasks."
    parts = ". ".join(f"Number {i}: {t['text']}" for i, t in open_)
    return f"You have {len(open_)} open task{'s' if len(open_) > 1 else ''}. {parts}."


def change_task(n, delete=False):
    tasks = load_tasks()
    if not n or n > len(tasks):
        return "I couldn't find a task with that number."
    if delete:
        gone = tasks.pop(n - 1)
        save_tasks(tasks)
        return f"Okay, I deleted {gone['text']}."
    tasks[n - 1]["done"] = True
    save_tasks(tasks)
    return f"Nice work. I've marked {tasks[n - 1]['text']} as done."


# ---------- files ----------
def named_folder(t):
    for key, name in (("download", "Downloads"), ("desktop", "Desktop"),
                      ("document", "Documents"), ("picture", "Pictures")):
        if key in t:
            return HOME / name
    return None


def plan_organize(folder):
    moves = []
    if folder.exists():
        for f in folder.iterdir():
            if f.is_file() and not f.name.startswith("."):
                cat = next((c for c, e in CATEGORIES.items()
                            if f.suffix.lower() in e), "Other")
                moves.append((f, folder / cat / f.name))
    return moves


def unique(path):
    p, i = path, 1
    while p.exists():
        p = path.with_name(f"{path.stem} ({i}){path.suffix}")
        i += 1
    return p


def run_moves(moves):
    done = []
    for src, dst in moves:
        dst.parent.mkdir(exist_ok=True)
        dst = unique(dst)
        shutil.move(str(src), str(dst))
        done.append([str(src), str(dst)])
    UNDO_FILE.write_text(json.dumps(done))
    return f"All done. I sorted {len(done)} files. Just say undo if you change your mind."


def undo_last():
    if not UNDO_FILE.exists():
        return "There's nothing to undo."
    count = 0
    for src, dst in reversed(json.loads(UNDO_FILE.read_text())):
        if Path(dst).exists() and not Path(src).exists():
            shutil.move(dst, src)
            count += 1
        try:
            Path(dst).parent.rmdir()
        except OSError:
            pass
    UNDO_FILE.unlink()
    return f"Done. I put {count} file{'s' * (count != 1)} back where they were."


def find_files(query):
    words = [w for w in re.split(r"\W+", query.lower())
             if w and w not in ("file", "files", "the", "a", "my", "called", "named")]
    if not words:
        return "What file should I look for?"
    hits = []
    for base in ("Desktop", "Documents", "Downloads"):
        for root, dirs, files in os.walk(HOME / base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                name = re.sub(r"[_\-.]+", " ", f.lower())
                if all(w in name for w in words):
                    hits.append(Path(root) / f)
    if not hits:
        return f"I couldn't find anything matching {' '.join(words)}."
    for h in hits[:5]:
        print("   ", h)
    names = ", ".join(h.name for h in hits[:3])
    if len(hits) == 1:
        return f"I found {names} in {hits[0].parent.name}. I've printed the full path."
    return f"I found {len(hits)} matches. The first ones are {names}. I've printed the paths."


# ---------- conversation ----------
def clean(s):
    return " ".join(re.sub(r"[^\w\s']", " ", s.lower()).split())


def ask(question, then):
    global awaiting
    awaiting = then
    return question


def is_yes(t):
    return bool(re.search(r"\b(yes|yeah|yep|sure|okay|ok|go ahead|do it|please do)\b", t)) \
        and not re.search(r"\b(no|don't|dont|stop)\b", t)


def start_organize(folder):
    moves = plan_organize(folder)
    if not moves:
        return f"Your {folder.name} folder is already tidy."
    return ask(f"I found {len(moves)} files in {folder.name}. Want me to sort them into folders?",
               lambda x: run_moves(moves) if is_yes(x) else "No problem, I won't touch anything.")


FILLER = re.compile(r"^(?:(?:hey |ok |okay )?(?:please|can you|could you|would you|will you|"
                    r"i want you to|i'd like you to|i would like you to|i need you to|"
                    r"go ahead and|i want to)\s+)+")
DONE_WORDS = r"\b(done|complete|completed|finish|finished|check off|tick off|cross off)\b"


def new_task(text):
    text = re.sub(r"^(to|that|about)\s+", "", text.strip())
    return add_task(text) if text else ask("Sure. What should the task say?", new_task)


def handle(raw):
    global awaiting
    t = clean(raw)
    if not t:
        return None
    if skills and skills.dictating():
        return skills.dictate(raw, t)
    if re.fullmatch(r"(?:please )?(?:quit|exit)(?: nova)?(?: please)?", t) or \
            re.search(r"\b(shut ?down|turn off|power off|quit|exit) nova\b", t):
        raise SystemExit("Okay, shutting down. Goodbye!")
    if awaiting:
        step, awaiting = awaiting, None
        if re.search(r"\b(cancel|never ?mind|forget it)\b", t):
            return "Okay, cancelled."
        return step(t)
    t = re.sub(r"\s+please$", "", FILLER.sub("", t))
    if len(t.split()) <= 5 and re.search(
            r"\b(thanks|thank you|that's all|that is all|never ?mind|goodbye|bye)\b", t):
        raise EndSession(random.choice(["You're welcome!", "Anytime.", "Happy to help."]))

    if skills:
        reply = skills.route(t, raw)
        if reply is not None:
            return reply

    # small talk and info
    if re.search(r"\b(your name|who are you)\b", t):
        return "I'm Nova, your assistant. I live on your laptop and help with your files and tasks."
    if len(t.split()) <= 3 and re.match(r"(hello|hi|hey|good (morning|afternoon|evening))\b", t):
        return random.choice(["Hi there! What can I do for you?", "Hello! How can I help?"])
    if re.search(r"how are you", t):
        return "I'm doing great, thanks for asking. What can I help with?"
    if re.search(r"\bwhat('s| is)? the time\b|\bwhat time\b|\bcurrent time\b|\bthe time\b", t):
        return f"It's {datetime.now().strftime('%I:%M %p').lstrip('0')}."
    if re.search(r"\b(what('s| is)? the date|what day|today's date|the date)\b", t):
        n = datetime.now()
        return f"Today is {n:%A}, {n:%B} {n.day}."
    if re.match(r"(help$|help me$|what can you do)", t):
        return ("I can open apps and websites, control volume and music, set timers and reminders, "
                "organize and move files, take notes, check the weather, manage your tasks, "
                "and answer questions if my AI brain is set up. Just ask.")

    # adding tasks, in many phrasings
    if m := re.search(r"\b(?:add|create|new|make|put)\b (?:a |an |another |new )*(?:task|to ?do|reminder)s?"
                      r"(?: to| for| called| named| that says| about)?\s*(.*)", t):
        return new_task(m.group(1))
    if m := re.search(r"\b(?:add|put) (.+?) (?:to|on|onto|in) (?:my |the )?(?:tasks?|to ?do|todo|reminders?)(?: list)?$", t):
        return new_task(m.group(1))
    if m := re.search(r"\bremind me (?:to|about) (.+)", t):
        return new_task(m.group(1))
    if m := re.search(r"\b(?:i need to|i have to|i must|don't forget to|dont forget to|remember to|note to self) (.+)", t):
        return new_task(m.group(1))

    # finishing / deleting tasks
    if re.search(DONE_WORDS, t) or re.match(r"mark\b", t):
        n = find_number(t)
        return change_task(n) if n else ask("Which task number?", lambda x: change_task(find_number(x)))
    if re.search(r"\b(delete|remove|get rid of|scrap)\b", t):
        n = find_number(t)
        return change_task(n, True) if n else ask("Which task number?", lambda x: change_task(find_number(x), True))

    # listing tasks
    if (re.search(r"\b(tasks?|to ?do|todos?|reminders?)\b", t)
            and re.search(r"\b(list|show|read|tell|what|what's|any|have|got|open|pending|remaining|check|see|my)\b", t)) \
            or re.search(r"what do i (need|have) to do", t):
        return list_tasks()

    # files
    if re.search(r"\b(organi[sz]e|clean up|clean out|tidy|declutter|sort|arrange)\b", t):
        folder = named_folder(t)
        if folder:
            return start_organize(folder)
        return ask("Sure. Which folder? Downloads, desktop, or documents?",
                   lambda x: start_organize(named_folder(x)) if named_folder(x)
                   else "Sorry, I only know downloads, desktop, or documents.")
    if re.search(r"\bundo\b|\bput (them|everything|it|those) back\b", t):
        return undo_last()
    if m := re.search(r"\b(?:find|locate|search for|look for|where is|where's)\b(?: me)?(.+)", t):
        return find_files(m.group(1))

    global _ai_hinted
    if skills and 2 <= len(t.split()) <= 200:
        reply = skills.ask_ai(raw.strip())       # the AI brain handles anything I don't recognise
        if reply:
            return reply
    if 2 <= len(t.split()) <= 10:           # ignore one-word noise and long background chatter
        msg = f"I heard {t}, but I'm not sure what to do with that yet. Say help to hear what I can do."
        if skills and not skills.ai_ready() and not _ai_hinted:
            _ai_hinted = True
            msg += " " + skills.AI_HINT
        return msg
    return None


def find_wake(heard):
    """Text after the name ('' if nothing follows), or None if Nova wasn't called.
    The name must be within the first 3 words, so it ignores 'nova' mid-sentence."""
    words = heard.split()
    for i, w in enumerate(words[:3]):
        if " ".join(words[i:i + 2]) in WAKE_ALIASES:
            return " ".join(words[i + 2:])
        if w in WAKE_ALIASES or SequenceMatcher(None, w, "nova").ratio() >= 0.8:
            return " ".join(words[i + 1:])
    return None


def alert(kind, text):
    """Called by timers/reminders when they go off."""
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1000, 200)
    except Exception:
        pass
    say(text)
    UI.post("hide")


def setup_skills():
    if not skills:
        print("(nova_skills.py not found next to assistant.py: only the basic commands are available)")
        return
    skills.configure(ask=ask, is_yes=is_yes, alert=alert, load_tasks=load_tasks,
                     add_task=add_task, list_tasks=list_tasks, change_task=change_task)
    skills.start_scheduler()


_ai_hinted = False


def respond(text):
    """Handle one utterance. Returns True if the conversation should end."""
    try:
        reply = handle(text)
        if reply:
            say(reply)
        return False
    except EndSession as e:
        say(str(e))
        return True
    except Exception:                          # a bug in one ability must never kill Nova
        import traceback
        traceback.print_exc()
        say("Sorry, something went wrong with that.")
        return False


# ---------- the Dynamic Island ----------
def _mix(a, b, k):
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


def _hex(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in c)


def _clamp(x):
    return max(0.0, min(1.0, x))


def _rounded(x1, y1, x2, y2, r, steps=10):
    """Points of a rounded rectangle (true circular corners)."""
    r = max(1.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = []
    for cx, cy, a0 in ((x2 - r, y1 + r, -90), (x2 - r, y2 - r, 0), (x1 + r, y2 - r, 90), (x1 + r, y1 + r, 180)):
        for k in range(steps + 1):
            a = math.radians(a0 + 90 * k / steps)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return pts


# ---------- Nova's orb (a port of the Siri "liquid orb" Metal shader) ----------
_ORB_SHELL = dict(shellInner=(1.0, 1.0, 1.0), shellMidC=(0.6078, 0.9569, 1.0), shellEdgeC=(0.7725, 0.6627, 1.0),
                  sheenC=(0.9176, 0.9569, 1.0), specC=(0.8627, 0.9176, 1.0), canvas=(0.0118, 0.0157, 0.0353))
ORB_SEEDS = {   # "calm" = the idle look, "vivid" = the thinking look (values from the original shader)
    "calm": dict(speed=0.246, zoom=0.3384, warp=1.664, ridge=0.24, shade=0.12, sheen=0.28, gloss=0.24,
                 shellMid=0.18, shellEdge=0.18, exposure=2.3, glassOp=0.44,   # exposure 1.36 in the original; brighter so a small orb is visible
                 
                 colorA=(0.7098, 0.6510, 0.4549), colorB=(0.3686, 0.5294, 0.5804),
                 colorC=(0.6039, 0.3922, 0.5412), colorD=(0.3882, 0.3569, 0.5412),
                 highlight=(0.7137, 0.7686, 0.8235), **_ORB_SHELL),
    "vivid": dict(speed=0.82, zoom=0.36, warp=3.2, ridge=0.5, shade=0.12, sheen=0.28, gloss=0.24,
                  shellMid=0.18, shellEdge=0.18, exposure=3.0, glassOp=0.44,   # 2.0 in the original
                  colorA=(1.0, 0.8471, 0.4196), colorB=(0.5098, 0.9569, 1.0),
                  colorC=(1.0, 0.4824, 0.8353), colorD=(0.5569, 0.4235, 1.0),
                  highlight=(1.0, 1.0, 1.0), **_ORB_SHELL)}
ORB_COLOR_KEYS = ("colorA", "colorB", "colorC", "colorD", "highlight", "shellInner", "shellMidC",
                  "shellEdgeC", "sheenC", "specC", "canvas")


def _srgb_to_lin(v):
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _lin_to_srgb(v):
    return v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


class OrbDriver:
    """Blends between the calm and vivid looks (colours mixed in linear light, like the original),
    and applies the audio bands: low/mid/high/all in 0..1."""

    def __init__(self):
        self.mode, self.src, self.dst = "calm", ORB_SEEDS["calm"], ORB_SEEDS["calm"]
        self.t0, self.dur, self.t = time.time(), 0.0, 0.0

    def _sample(self, now):
        raw = 1.0 if self.dur <= 0 else _clamp((now - self.t0) / self.dur)
        k = 1 - (1 - raw) ** 3 if self.mode == "vivid" else raw * raw * (3 - 2 * raw)   # quick in, gentle out
        out = {}
        for key, a in self.src.items():
            b = self.dst[key]
            if key in ORB_COLOR_KEYS:
                out[key] = tuple(_lin_to_srgb(_srgb_to_lin(x) + (_srgb_to_lin(y) - _srgb_to_lin(x)) * k)
                                 for x, y in zip(a, b))
            else:
                out[key] = a + (b - a) * k
        return out

    def advance(self, dt, mode, bands):
        now = time.time()
        if mode != self.mode:
            self.src, self.dst, self.mode = self._sample(now), ORB_SEEDS[mode], mode
            self.t0, self.dur = now, 0.22 if mode == "vivid" else 0.65
        P = self._sample(now)
        low, mid, high, allv = (0.8 * _clamp(b) for b in bands)           # 0.8 = the Siri style's audio strength
        P["speed"] *= 1 + 0.7 * allv
        P["warp"] += 0.85 * mid
        P["sheen"] += 0.16 * high
        P["exposure"] *= 1 + 0.12 * allv
        self.t += dt * P["speed"]
        return P, self.t


def siri_orb(n, t, P):
    """Renders the orb as an (n, n, 4) float array (straight alpha, 0..1). numpy port of the
    Metal 'Siri' fluid + glass sphere: refraction, colour split, rim light, soft edge."""
    import numpy as np

    def sm(a, b, x):
        u = np.clip((x - a) / (b - a), 0.0, 1.0)
        return u * u * (3.0 - 2.0 * u)

    def col(key):
        return np.array(P[key], np.float32)
    A, B, C, D, H = col("colorA"), col("colorB"), col("colorC"), col("colorD"), col("highlight")
    ii = np.arange(n, dtype=np.float32) + 0.5
    px, py = np.meshgrid((2 * ii - n) / n * 1.04, (n - 2 * ii) / n * 1.04)   # the ball fills the tile

    def fluid(x0, y0):
        sc = 0.74 + P["zoom"] * 0.34
        qx, qy = x0 / sc, y0 / sc
        env = np.cos(1.5707964 * np.minimum(np.abs(0.9 * qx), 1.0)) ** 2
        low = 0.5 + 0.5 * np.cos(t * 0.37)
        mid = 0.5 + 0.5 * np.sin(t * 0.51 + 1.2)
        high = 0.5 + 0.5 * np.cos(t * 0.73 + 2.1)
        drift = t * 2.4
        main_amp = 0.25 + P["ridge"] * 0.075 + low * 0.018
        band_amp = main_amp + mid * 0.025 + high * 0.018
        main_y = main_amp * env * np.sin(qx * 1.1 + drift)
        sep = 1.85 + P["warp"] * 0.2 + mid * 0.28
        soft = 0.035 + (1 - P["ridge"]) * 0.018 + mid * 0.006
        ws = []
        for off in (-sep, -sep * 0.34, sep * 0.34, sep):
            y = band_amp * env * np.sin(qx + drift + off)
            d = np.abs(qy - y)
            line = 0.018 / (np.sqrt(d * d + soft * soft) + 0.026)
            bd = np.maximum(0.0, np.maximum(qy - np.maximum(main_y, y), np.minimum(main_y, y) - qy))
            ws.append(line + 0.018 / (bd + 0.075))
        dom = [w * w for w in ws]
        dom_sum = np.maximum(sum(dom), 1e-4)[..., None]
        spectral = (A * dom[0][..., None] + C * dom[1][..., None] + B * dom[2][..., None]
                    + D * dom[3][..., None]) / dom_sum
        energy = ((1 - np.exp(-sum(ws) * 0.58)) * env)[..., None]
        white = (np.exp(-((qy - main_y) ** 2) / 0.0028) * env)[..., None]
        k = sm(-0.7, 0.7, qy)[..., None]
        c = (D + (B - D) * k) * 0.018 + spectral * energy * 1.14
        c = c + H * white * (0.18 + 0.1 * low)
        c = c / (1 + c * 0.18)
        shade = P["shade"]
        c = c + (H - c) * (shade * 0.22 * sm(0.15, 1.15, px_dot(x0, y0, -0.32, 0.78)))[..., None]
        c = c * (1 - shade * 0.34 * sm(-0.1, 1.2, px_dot(x0, y0, 0.45, -0.62)))[..., None]
        c = c * (1 - shade * 0.22 * sm(0.72, 1.08, np.hypot(x0, y0)))[..., None]
        return np.clip(c, 0.0, 1.0)

    def px_dot(x0, y0, dx, dy):
        return x0 * dx + y0 * dy

    pd = np.hypot(px, py)
    safe = np.maximum(pd, 1e-4)
    nx, ny = np.where(pd > 1e-4, px / safe, 0.0), np.where(pd > 1e-4, py / safe, 0.0)
    clear = 1.0 - sm(0.995, 1.04, pd)
    depth = np.maximum(1.0 - pd, 0.0)
    width = 0.015 + 0.95 * float(np.clip(P["shellMid"], 0, 1))
    tt = np.clip(depth / max(width, 0.001), 0.0, 1.0)
    prof = np.power(1.0 - np.sqrt(np.maximum(1.0 - (1.0 - tt) ** 2, 0.0)), 0.68)
    gop = float(np.clip(P["glassOp"], 0, 1))
    refr = 1.6 * gop * prof
    rx, ry = px - nx * refr, py - ny * refr
    split = 0.14 * float(np.clip(P["gloss"], 0, 2)) * gop * prof
    f = np.dstack([fluid(rx - nx * split, ry - ny * split)[..., 0], fluid(rx, ry)[..., 1],
                   fluid(rx + nx * split, ry + ny * split)[..., 2]])         # colour split at the glass edge
    lum = (f @ np.array([0.213, 0.715, 0.072], np.float32))[..., None]
    sat = np.clip(lum + (f - lum) * 1.22, 0.0, 1.0)
    canvas = col("canvas")

    def over(dst, src, a):
        k = np.clip(a, 0.0, 1.0)[..., None]
        return src * k + dst * (1.0 - k)

    def lobe(dx, dy, cut, power):
        m = np.hypot(dx, dy)
        return np.power(np.clip((nx * dx / m + ny * dy / m - cut) / max(1.0 - cut, 0.001), 0.0, 1.0), power)
    k = (0.99 * clear)[..., None]
    c = sat * k + canvas * (1.0 - k)
    rim = np.power((1.0 - sm(0.0, 0.026 + 0.055 * float(np.clip(P["shellEdge"], 0, 1)), depth)) * clear, 1.8)
    c = over(c, col("shellInner"), rim * gop * 0.45)
    disp = rim * float(np.clip(P["gloss"], 0, 2)) * (0.8 + 0.8 * P["shellEdge"])
    c = over(c, col("shellMidC"), disp * lobe(0.84, 0.54, -0.32, 1.8))
    c = over(c, col("shellEdgeC"), disp * lobe(-0.62, -0.78, -0.28, 2.0))
    shadow = rim * (0.015 + 0.15 * P["shellEdge"]) * (0.15 + 0.85 * np.maximum(nx * 0.45 - ny * 0.89, 0.0))
    c = c * (1.0 - shadow)[..., None]
    sheen = float(np.clip(P["sheen"], 0, 2))
    c = over(c, col("sheenC"), rim * lobe(-0.68, 0.73, 0.2, 2.8) * sheen * 1.4)
    c = over(c, col("specC"), rim * lobe(0.74, -0.67, 0.4, 3.6) * sheen)
    ball = 1.0 - sm(0.99, 1.01, pd)
    c = np.clip(c * max(P["exposure"], 0.0), 0.0, 1.0) * ball[..., None]
    ball_s = np.maximum(ball, 1e-4)[..., None]
    straight = np.where(ball[..., None] > 1e-4, c / ball_s, 0.0)
    signal = np.clip(c.max(axis=2) / ball_s[..., 0], 0.0, 1.0)          # how bright each pixel is
    body = float(P.get("bodyA", 1.0))                                   # dark glass is see-through, bright parts are solid
    alpha = np.clip(ball * np.clip(body + signal * 1.6, 0.0, 1.0), 0.0, 1.0)
    return np.dstack([np.clip(straight, 0.0, 1.0), alpha]).astype(np.float32)


def _numpy_ok():
    try:
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


class Spring:
    """Under-damped (a little overshoot) when growing, critically damped (snappy) when shrinking."""

    def __init__(self, x):
        self.x, self.v, self.target = x, 0.0, x

    def step(self, dt, bouncy, stiffness=380.0):
        damping = 2 * math.sqrt(stiffness) * (0.5 if bouncy else 1.0)
        for _ in range(2):
            self.v += (stiffness * (self.target - self.x) - damping * self.v) * dt / 2
            self.x += self.v * dt / 2


class IslandCore:
    """Everything about Nova's Dynamic Island except the window: states, springs, and the
    list of shapes to draw. A renderer (smooth or classic) turns that list into pixels."""
    HIDDEN, COMPACT, EXPANDED_W = (60, 14), (150, 54), 460      # pixels, multiplied by the scale
    ORB = 46                                                    # diameter of Nova's orb
    supports_orb = False                                        # the smooth renderer turns this on
    PALETTE = [(0, 200, 255), (60, 110, 255), (170, 80, 255), (255, 60, 170), (255, 150, 60)]
    TINT = {"listening": (90, 140, 255), "thinking": (170, 110, 255), "speaking": (80, 220, 200)}
    LABELS = {"listening": "Listening…", "thinking": "Thinking…", "speaking": "Nova"}
    FONT_PX = {"body": 14, "you": 13, "label": 12}
    FONT_BOLD = {"body": True, "you": False, "label": True}

    def __init__(self, scale):
        S = self.S = scale
        self.q = queue.Queue()
        self.level = self.lvl = 0.0
        self.state, self.you, self.reply = "listening", "", ""
        self.alpha, self.shown, self.hide_at = 0.0, False, None
        self.w, self.h = Spring(self.HIDDEN[0] * S), Spring(self.HIDDEN[1] * S)
        self.last = time.time()
        self.WIN_W, self.WIN_H, self.TOP = int(520 * S), int(250 * S), int(6 * S)
        self._wrap_cache = {}
        self.orb, self._voiced, self._dt = OrbDriver(), False, 0.016
        self.pill = ISLAND_PILL
        self.show_text = SHOW_TEXT

    # ---- messages from Nova (thread-safe) ----
    def post(self, kind, text=""):
        self.q.put((kind, text))

    def _pump(self):
        """Handle queued messages. Returns False when asked to quit."""
        try:
            while True:
                kind, text = self.q.get_nowait()
                if kind == "quit":
                    return False
                self._handle(kind, text)
        except queue.Empty:
            return True

    def _handle(self, kind, text):
        if kind == "hide":
            self.hide_at = time.time() + 1.5           # let you finish reading
            return
        self.hide_at = None
        if kind == "hide_now":
            if self.state == "speaking":
                stop_speaking()
            self.shown = False
            return
        if not self.shown:
            self.shown = True
            if self.alpha < 0.03:                      # emerge from the little pill
                self.w.x, self.h.x, self.w.v, self.h.v = \
                    self.HIDDEN[0] * self.S, self.HIDDEN[1] * self.S, 0.0, 0.0
        if kind == "wake":
            self.state, self.you, self.reply, self._voiced = "listening", "", "", False
        elif kind == "listening":
            self.state = "listening"
        elif kind == "heard":
            self.state, self.you, self.reply = "thinking", text, ""
        elif kind == "say":
            self.state, self.reply = "speaking", text

    # ---- text measuring ----
    def _measure(self, key, text):
        return len(text) * self.FONT_PX[key] * self.S * 0.53

    def _wrap(self, s, key, width):
        if (s, key, int(width)) not in self._wrap_cache:
            lines, cur = [], ""
            for word in s.split():
                trial = (cur + " " + word).strip()
                if self._measure(key, trial) <= width:
                    cur = trial
                else:
                    if cur:
                        lines.append(cur)
                    cur = word
            if cur:
                lines.append(cur)
            self._wrap_cache = {(s, key, int(width)): lines or [""]}
        return self._wrap_cache[(s, key, int(width))]

    def _fit(self, s, key, width):
        if self._measure(key, s) <= width:
            return s
        while len(s) > 1 and self._measure(key, s + "…") > width:
            s = s[:-1]
        return s.rstrip() + "…"

    def _expanded_height(self):
        S, width = self.S, (self.EXPANDED_W - 44) * self.S
        h = 60 + (22 if self.you else 0)
        if self.reply:
            h += 22 * min(4, len(self._wrap(self.reply[:200], "body", width)))
        return (h + 14) * S

    # ---- time ----
    def _advance(self, dt):
        """Move the animation forward. Returns False when the island is fully gone."""
        S = self.S
        if self.hide_at and time.time() >= self.hide_at:
            self.shown, self.hide_at = False, None
        self.alpha += ((1.0 if self.shown else 0.0) - self.alpha) * (1 - math.exp(-(14 if self.shown else 5) * dt))
        if not self.shown and self.alpha < 0.03:
            self.alpha = 0.0
            return False
        if not self.shown:
            tw, th = self.HIDDEN[0] * S, self.HIDDEN[1] * S
        elif self.show_text and (self.you or self.reply):
            tw, th = self.EXPANDED_W * S, self._expanded_height()
        else:
            tw, th = self.COMPACT[0] * S, self.COMPACT[1] * S
        for spring, target in ((self.w, tw), (self.h, th)):
            spring.target = target
            spring.step(dt, bouncy=target > spring.x)
        self.lvl += (self.level - self.lvl) * (1 - math.exp(-12 * dt))
        self._dt = dt
        return True

    def _orb_drive(self):
        """Decide how the orb should look right now: calm or vivid, and how loud."""
        st, lvl, now = self.state, self.lvl, time.time()
        if st == "listening":                          # colours wake up when you talk, settle when you stop
            if lvl > 0.03:
                self._voiced = True
            elif lvl < 0.012:
                self._voiced = False
            a = _clamp(lvl * 10)
            bands = (a, a * (0.55 + 0.45 * math.sin(now * 3.1) ** 2), a * (0.5 + 0.5 * math.sin(now * 5.3 + 1) ** 2), a)
            mode = "vivid" if self._voiced else "calm"
        elif st == "thinking":
            mode, bands = "vivid", (0.0, 0.0, 0.0, 0.0)
        else:                                          # speaking: pulse as if following the voice
            a = 0.5 + 0.35 * abs(math.sin(now * 6.3))
            mode, bands = "vivid", (a, a * (0.6 + 0.4 * math.sin(now * 4.1) ** 2),
                                    a * (0.5 + 0.5 * math.sin(now * 7.7) ** 2), a)
        return self.orb.advance(self._dt, mode, bands)

    # ---- what to draw ----
    def _c(self, color, op):
        """A colour faded by `op`: towards black on a pill, towards transparent when floating."""
        if self.pill:
            return _hex(_mix((0, 0, 0), color, _clamp(op)))
        return tuple(int(max(0, min(255, v))) for v in color) + (int(255 * _clamp(op)),)

    def _scene(self):
        """The island as a list of shapes. The first is the pill (or, without a pill, an invisible box)."""
        S, t, st, items = self.S, time.time(), self.state, []
        w, h = min(self.w.x, self.WIN_W - 4), min(self.h.x, self.WIN_H - self.TOP - 4)
        cx, y0 = self.WIN_W / 2, self.TOP
        x1, x2 = cx - w / 2, cx + w / 2
        items.append(("poly", _rounded(x1, y0, x2, y0 + h, min(h / 2, 34 * S)),
                      "#000000" if self.pill else None,
                      _hex(_mix((0, 0, 0), self.TINT[st], .5)) if self.pill else None))   # pill + keyline tint
        cop = _clamp((w - 100 * S) / (80 * S)) * _clamp((h - 22 * S) / (14 * S))   # content fades in
        hy, lvl, P_ = y0 + self.COMPACT[1] / 2 * S, self.lvl, self.PALETTE
        # the orb: centred; inside a pill it glides to the left as the island opens
        ex = _clamp((w - self.COMPACT[0] * S) / ((self.EXPANDED_W - self.COMPACT[0]) * S))
        ex = ex * ex * (3 - 2 * ex)
        left = cx - self.EXPANDED_W * S / 2 + 38 * S
        ox = cx + (left - cx) * ex if self.pill else cx
        if self.supports_orb:
            P, ot = self._orb_drive()
            P["bodyA"] = 1.0 if self.pill else ORB_BODY_ALPHA
            items.append(("orb", ox, hy, self.ORB * S, P, ot, cop))
        else:                                                                       # simple orb (no numpy)
            pulse = {"listening": min(4, lvl * 60), "thinking": 1.2 * math.sin(t * 6),
                     "speaking": 2 * abs(math.sin(t * 6))}[st]
            r = (11 + pulse) * S
            items.append(("oval", ox - r, hy - r, ox + r, hy + r, self._c(P_[2], cop)))
            speed = {"listening": 2, "thinking": 7, "speaking": 4}[st]
            for i, col in enumerate((P_[0], P_[3], P_[4])):
                a = t * speed + i * 2.094
                bx, by, rr = ox + math.cos(a) * r * 0.4, hy + math.sin(a) * r * 0.4, r * 0.6
                items.append(("oval", bx - rr, by - rr, bx + rr, by + rr, self._c(col, cop)))
        if not (self.show_text and (self.you or self.reply)):
            return items                                                            # orb only
        pe = _clamp((h - (self.COMPACT[1] + 10) * S) / max(1.0, self.h.target - (self.COMPACT[1] + 10) * S))
        width = (self.EXPANDED_W - 44) * S
        if self.pill:                                                               # text inside the island
            if pe > 0.2:
                items.append(("text", cx, hy, self.LABELS[st], "center", self._c((150, 150, 165), (pe - .3) / .4),
                              "label", None))
            pad, ty = cx - self.EXPANDED_W * S / 2 + 22 * S, y0 + 60 * S
            if self.you:
                if pe > 0.5:
                    items.append(("text", pad, ty, self._fit(f"“{self.you}”", "you", width), "nw",
                                  self._c((160, 160, 175), (pe - .5) / .4), "you", None))
                ty += 22 * S
            if self.reply and pe > 0.65:
                items.append(("text", pad, ty, self.reply[:200], "nw", self._c((255, 255, 255), (pe - .65) / .35),
                              "body", width))
        else:                                                                       # floating text under the orb
            ty = hy + self.ORB * S / 2 + 14 * S
            if self.you:
                if pe > 0.5:
                    items.append(("text", cx, ty, self._fit(f"“{self.you}”", "you", width), "n",
                                  self._c((225, 225, 240), (pe - .5) / .4), "you", None))
                ty += 22 * S
            if self.reply and pe > 0.65:
                items.append(("text", cx, ty, self.reply[:200], "n", self._c((255, 255, 255), (pe - .65) / .35),
                              "body", width))
        return items


class PilRender:
    """Draws the island with Pillow at 3x size, then shrinks it: smooth, anti-aliased edges and text."""
    SS = 3
    _fonts = {}
    supports_orb = _numpy_ok()

    def _font(self, key):
        size = max(6, int(self.FONT_PX[key] * self.S * self.SS))
        if (key, size) not in self._fonts:
            from PIL import ImageFont
            names = (("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf") if self.FONT_BOLD[key]
                     else ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"))
            font = None
            for name in names:
                try:
                    font = ImageFont.truetype(name, size)
                    break
                except OSError:
                    continue
            self._fonts[(key, size)] = font or ImageFont.load_default(size)
        return self._fonts[(key, size)]

    def _measure(self, key, text):
        return self._font(key).getlength(text) / self.SS

    def _render_pil(self, items):
        """Returns (RGBA image, x, y): where the image goes, in island coordinates."""
        from PIL import Image, ImageDraw
        SS, S = self.SS, self.S
        pts = items[0][1]
        bx0, by0 = int(min(pts[0::2])) - 3, int(min(pts[1::2])) - 3
        bx1, by1 = int(max(pts[0::2])) + 4, int(max(pts[1::2])) + 4
        img = Image.new("RGBA", ((bx1 - bx0) * SS, (by1 - by0) * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        texts = []

        def X(v):
            return (v - bx0) * SS

        def Y(v):
            return (v - by0) * SS
        for it in items:
            kind = it[0]
            if kind == "poly":
                xy = [(X(x), Y(y)) for x, y in zip(it[1][0::2], it[1][1::2])]
                if it[2]:
                    d.polygon(xy, fill=it[2])
                if it[3]:
                    d.line(xy + [xy[0]], fill=it[3], width=SS, joint="curve")
            elif kind == "oval":
                d.ellipse([X(it[1]), Y(it[2]), X(it[3]), Y(it[4])], fill=it[5])
            elif kind == "orb":
                _, ox, oy, diam, P, ot, op = it
                if op < 0.02:
                    continue
                import numpy as np
                arr = siri_orb(max(24, int(diam * 1.04 * 2)), ot, P)        # drawn at 2x, then placed at 3x
                arr[..., 3] *= op
                big = max(8, int(round(diam * 1.04 * SS)))
                tile = Image.fromarray((arr * 255 + 0.5).astype(np.uint8)).resize(
                    (big, big), getattr(Image, "Resampling", Image).BICUBIC)
                tx, ty = int(X(ox) - big / 2), int(Y(oy) - big / 2)
                try:
                    img.alpha_composite(tile, (max(tx, 0), max(ty, 0)), (max(-tx, 0), max(-ty, 0)))
                except ValueError:
                    pass
            elif kind == "line":
                _, x0, y0, x1, y1, wd, fill = it
                d.line([(X(x0), Y(y0)), (X(x1), Y(y1))], fill=fill, width=int(wd * SS))
                rr = wd * SS / 2
                for ex, ey in ((x0, y0), (x1, y1)):                    # round caps
                    d.ellipse([X(ex) - rr, Y(ey) - rr, X(ex) + rr, Y(ey) + rr], fill=fill)
            elif kind == "text" and not self.pill:
                texts.append(it)                                       # drawn last, with a soft halo
            elif kind == "text":
                _, x, y, s, anchor, fill, key, width = it
                f = self._font(key)
                if anchor == "center":
                    d.text((X(x), Y(y)), s, font=f, fill=fill, anchor="mm")
                else:
                    for i, line in enumerate(self._wrap(s, key, width) if width else [s]):
                        d.text((X(x), Y(y) + i * 22 * S * SS), line, font=f, fill=fill, anchor="la")
        if texts:
            self._floating_text(img, texts, X, Y)
        resample = getattr(Image, "Resampling", Image).LANCZOS
        return img.resize((bx1 - bx0, by1 - by0), resample), bx0, by0

    def _floating_text(self, img, texts, X, Y):
        """Text with no pill behind it: a soft dark halo first (so it reads on any wallpaper), then the letters."""
        from PIL import Image, ImageDraw, ImageFilter
        SS, S, size = self.SS, self.S, img.size

        def draw_all(draw, only=None, **kw):
            for it in (texts if only is None else [only]):
                _, x, y, s, anchor, fill, key, width = it
                f = self._font(key)
                if anchor == "center":
                    draw.text((X(x), Y(y)), s, font=f, fill=255, anchor="mm", **kw)
                else:
                    a = "ma" if anchor == "n" else "la"
                    for i, line in enumerate(self._wrap(s, key, width) if width else [s]):
                        draw.text((X(x), Y(y) + i * 22 * S * SS), line, font=f, fill=255, anchor=a, **kw)
        halo = Image.new("L", size, 0)
        draw_all(ImageDraw.Draw(halo), stroke_width=int(2.2 * SS))
        halo = halo.filter(ImageFilter.GaussianBlur(1.1 * SS)).point(lambda v: min(255, int(v * 0.95)))
        shadow = Image.new("RGBA", size, (0, 0, 0, 0))
        shadow.putalpha(halo)
        img.alpha_composite(shadow)
        for it in texts:
            mask = Image.new("L", size, 0)
            draw_all(ImageDraw.Draw(mask), only=it)
            r, g, b, a = it[5]
            layer = Image.new("RGBA", size, (r, g, b, 0))
            layer.putalpha(mask.point(lambda v, a=a: v * a // 255))
            img.alpha_composite(layer)

    @staticmethod
    def _bgra_premultiplied(img):
        from PIL import Image
        r, g, b, a = img.convert("RGBa").split()                   # premultiplied alpha, as Windows wants
        return Image.merge("RGBA", (b, g, r, a)).tobytes()


class TkOverlay(IslandCore):
    """The classic island: a plain Tk window (edges are not smoothed). Used as a fallback."""

    def __init__(self):
        import tkinter as tk
        super().__init__(ISLAND_SCALE)
        self.pill = True                                           # this window can't do see-through text
        S, self.visible = self.S, False
        self.fonts = {k: ("Segoe UI", -int(px * S), "bold" if self.FONT_BOLD[k] else "normal")
                      for k, px in self.FONT_PX.items()}
        self.root = r = tk.Tk()
        r.withdraw()
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-alpha", 0.0)
        try:
            r.attributes("-transparentcolor", "#010101")           # everything not drawn is see-through
        except tk.TclError:
            pass
        r.geometry(f"{self.WIN_W}x{self.WIN_H}+{(r.winfo_screenwidth() - self.WIN_W) // 2}+0")
        self.cv = tk.Canvas(r, width=self.WIN_W, height=self.WIN_H, bg="#010101", highlightthickness=0)
        self.cv.pack()
        self.cv.bind("<Button-1>", lambda e: self.post("hide_now"))
        try:                                                       # real font metrics, so text wraps exactly
            from tkinter import font as tkfont
            self.tkfonts = {k: tkfont.Font(root=r, family=f[0], size=f[1], weight=f[2]) for k, f in self.fonts.items()}
        except Exception:
            self.tkfonts = {}

    def _measure(self, key, text):
        f = self.tkfonts.get(key)
        return f.measure(text) if f else super()._measure(key, text)

    def run(self):
        self.root.after(16, self._tick)
        self.root.mainloop()

    def _tick(self):
        now = time.time()
        dt, self.last = min(0.05, max(0.001, now - self.last)), now
        if not self._pump():
            self.root.destroy()
            return
        if self._advance(dt):
            if not self.visible:
                self.root.deiconify()
                self.root.attributes("-topmost", True)
                self.visible = True
            self.root.attributes("-alpha", self.alpha)
            self._paint(self._scene())
        elif self.visible:
            self.root.withdraw()
            self.visible = False
        self.root.after(16, self._tick)

    def _paint(self, items):
        c = self.cv
        c.delete("all")
        for it in items:
            kind = it[0]
            if kind == "poly":
                c.create_polygon(it[1], fill=it[2], outline=it[3], width=1)
            elif kind == "oval":
                c.create_oval(it[1], it[2], it[3], it[4], fill=it[5], outline="")
            elif kind == "line":
                c.create_line(it[1], it[2], it[3], it[4], width=int(it[5]), fill=it[6], capstyle="round")
            else:
                _, x, y, s, anchor, fill, key, width = it
                extra = {"width": width} if width else {}
                if anchor in ("n", "center"):
                    extra["justify"] = "center"
                c.create_text(x, y, text=s, anchor=anchor, fill=fill, font=self.fonts[key], **extra)


class WinOverlay(PilRender, IslandCore):
    """The smooth island: a real Windows layered window with per-pixel transparency
    (anti-aliased edges, crisp text, no jaggies). Needs Pillow; falls back to the classic island if not."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt
        import PIL.Image                                           # ImportError -> classic island
        self._ct, self._wt = ctypes, wt
        u, g, kern = (ctypes.WinDLL(n, use_last_error=True) for n in ("user32", "gdi32", "kernel32"))
        self.u, self.g = u, g
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)         # draw in real pixels, not stretched ones
        except Exception:
            try:
                u.SetProcessDPIAware()
            except Exception:
                pass
        try:
            dpi = u.GetDpiForSystem()
        except Exception:
            dpi = 96
        super().__init__(ISLAND_SCALE * max(96, dpi) / 96)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HANDLE),
                        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE),
                        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                        ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]
        self._BIH, self._BLEND = BITMAPINFOHEADER, BLENDFUNCTION
        i = ctypes.c_int
        for fn, args, res in (
                (u.DefWindowProcW, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], LRESULT),
                (u.RegisterClassW, [ctypes.POINTER(WNDCLASSW)], wt.ATOM),
                (u.CreateWindowExW, [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, i, i, i, i,
                                     wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID], wt.HWND),
                (u.GetDC, [wt.HWND], wt.HDC),
                (u.UpdateLayeredWindow, [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
                                         wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF,
                                         ctypes.POINTER(BLENDFUNCTION), wt.DWORD], wt.BOOL),
                (u.ShowWindow, [wt.HWND, i], wt.BOOL),
                (u.SetWindowPos, [wt.HWND, wt.HWND, i, i, i, i, wt.UINT], wt.BOOL),
                (u.PeekMessageW, [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT], wt.BOOL),
                (u.TranslateMessage, [ctypes.POINTER(wt.MSG)], wt.BOOL),
                (u.DispatchMessageW, [ctypes.POINTER(wt.MSG)], LRESULT),
                (u.DestroyWindow, [wt.HWND], wt.BOOL),
                (u.GetSystemMetrics, [i], i),
                (g.CreateCompatibleDC, [wt.HDC], wt.HDC),
                (g.CreateDIBSection, [wt.HDC, ctypes.POINTER(BITMAPINFOHEADER), wt.UINT,
                                      ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD], wt.HBITMAP),
                (g.SelectObject, [wt.HDC, wt.HGDIOBJ], wt.HGDIOBJ),
                (g.DeleteObject, [wt.HGDIOBJ], wt.BOOL),
                (kern.GetModuleHandleW, [wt.LPCWSTR], wt.HMODULE)):
            fn.argtypes, fn.restype = args, res

        def proc(hwnd, msg, wparam, lparam):
            if msg == 0x0201:                                      # click on the island dismisses it
                self.post("hide_now")
                return 0
            return u.DefWindowProcW(hwnd, msg, wparam, lparam)
        self._proc = WNDPROC(proc)                                 # keep a reference so it isn't garbage collected
        hinst = kern.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._proc, hinst, "NovaIsland"
        if not u.RegisterClassW(ctypes.byref(wc)) and ctypes.get_last_error() != 1410:   # 1410 = already registered
            raise OSError(f"RegisterClass failed ({ctypes.get_last_error()})")
        # layered + topmost + tool window (no taskbar button) + never takes focus
        self.hwnd = u.CreateWindowExW(0x80000 | 0x8 | 0x80 | 0x08000000, "NovaIsland", "Nova", 0x80000000,
                                      0, 0, 1, 1, None, None, hinst, None)
        if not self.hwnd:
            raise OSError(f"CreateWindow failed ({ctypes.get_last_error()})")
        self.screen_dc = u.GetDC(None)
        self.mem_dc = g.CreateCompatibleDC(self.screen_dc)
        self.screen_w = u.GetSystemMetrics(0)
        self.visible, self.alive, self._bmp, self._bits, self._hbm = False, True, None, ctypes.c_void_p(), None
        self._present(PIL.Image.new("RGBA", (4, 4), (0, 0, 0, 0)), 0, 0, 255)   # self-test: raises if Windows refuses

    def _present(self, img, sx, sy, alpha):
        ct, wt, u, g = self._ct, self._wt, self.u, self.g
        w, h = img.size
        if self._bmp != (w, h):                                    # (re)create the pixel buffer when the size changes
            bih = self._BIH(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)    # negative height = top-down rows
            hbm = g.CreateDIBSection(self.mem_dc, ct.byref(bih), 0, ct.byref(self._bits), None, 0)
            if not hbm:
                raise OSError("CreateDIBSection failed")
            g.SelectObject(self.mem_dc, hbm)
            if self._hbm:
                g.DeleteObject(self._hbm)
            self._hbm, self._bmp = hbm, (w, h)
        data = self._bgra_premultiplied(img)
        ct.memmove(self._bits, data, len(data))
        ok = u.UpdateLayeredWindow(self.hwnd, self.screen_dc, ct.byref(wt.POINT(sx, sy)), ct.byref(wt.SIZE(w, h)),
                                   self.mem_dc, ct.byref(wt.POINT(0, 0)), 0,
                                   ct.byref(self._BLEND(0, 0, max(0, min(255, alpha)), 1)), 2)   # ULW_ALPHA
        if not ok:
            raise OSError(f"UpdateLayeredWindow failed ({ct.get_last_error()})")

    def _frame(self, dt):
        u = self.u
        if not self._advance(dt):
            if self.visible:
                u.ShowWindow(self.hwnd, 0)
                self.visible = False
            return
        img, ox, oy = self._render_pil(self._scene())
        self._present(img, int(self.screen_w / 2 + (ox - self.WIN_W / 2)), oy, int(self.alpha * 255))
        if not self.visible:
            u.ShowWindow(self.hwnd, 4)                             # SW_SHOWNOACTIVATE
            u.SetWindowPos(self.hwnd, self._ct.c_void_p(-1), 0, 0, 0, 0, 0x13)   # topmost, no move/size/activate
            self.visible = True

    def run(self):
        ct, u, msg, errors = self._ct, self.u, self._wt.MSG(), 0
        while self.alive:
            t0 = time.time()
            while u.PeekMessageW(ct.byref(msg), None, 0, 0, 1):
                u.TranslateMessage(ct.byref(msg))
                u.DispatchMessageW(ct.byref(msg))
            dt, self.last = min(0.05, max(0.001, t0 - self.last)), t0
            if not self._pump():
                break
            try:
                self._frame(dt)
                errors = 0
            except Exception:
                errors += 1
                if errors in (1, 50):
                    import traceback
                    traceback.print_exc()
            time.sleep(max(0.0, (1 / 45 if self.visible else 1 / 20) - (time.time() - t0)))
        u.DestroyWindow(self.hwnd)


Overlay = TkOverlay                                                # old name, kept for compatibility


def make_overlay():
    if ISLAND_RENDERER != "classic" and os.name == "nt":
        try:
            return WinOverlay()
        except Exception as err:
            print(f"(smooth island unavailable, using the classic one: {err!r}. "
                  "For smooth edges run: pip install pillow)")
    return TkOverlay()


# ---------- run modes ----------
def session(mic):
    global awaiting
    last = time.time()
    while True:
        if skills and skills.dictating():
            last = time.time()                 # stay awake while dictating
        remaining = SESSION_TIMEOUT - (time.time() - last)
        if remaining <= 0:
            awaiting = None
            say("Okay, I'll be here if you need me.")
            return
        UI.post("listening")
        time.sleep(0.25)                       # let my own voice fade before listening
        heard = mic.listen(wait=remaining, commands=True)
        if not heard:
            continue
        print("You said:", heard)
        UI.post("heard", heard)
        if respond(heard):
            return
        last = time.time()                     # timer restarts after I finish speaking


WARM_PHRASES = (
    "One moment, getting ready.", "Nova is online.",
    f"I'm ready. Just say {NAME} when you need me.",
    "Yes?", "I'm listening.", "Hi! What do you need?",
    "Okay, I'll be here if you need me.",
    "You're welcome!", "Anytime.", "Happy to help.",
    "Your timer is done.",
)


def _parse_hotkey(spec):
    """Parse a combo like 'ctrl+shift+space' into (win32_modifiers, virtual_key)."""
    if not spec:
        return None
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    mods_map = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
    keys_map = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B}
    for i in range(1, 13):
        keys_map[f"f{i}"] = 0x6F + i
    mods, vk = 0x4000, None                          # MOD_NOREPEAT
    for p in parts:
        if p in mods_map:
            mods |= mods_map[p]
        elif p in keys_map:
            vk = keys_map[p]
        elif len(p) == 1 and p.isalnum():
            vk = ord(p.upper())
        else:
            return None
    return (mods, vk) if vk else None


def _start_hotkey():
    """Register a global Windows hotkey that stops Nova mid-speech or wakes listening."""
    if os.name != "nt" or not HOTKEY:
        return
    parsed = _parse_hotkey(HOTKEY)
    if not parsed:
        return
    mods, vk = parsed

    def loop():
        import ctypes
        from ctypes import wintypes as wt
        u = ctypes.windll.user32
        if not u.RegisterHotKey(None, 1, mods, vk):
            print(f"   (hotkey {HOTKEY} could not be registered — may already be in use)")
            return
        print(f"   (hotkey active: {HOTKEY} to wake or interrupt Nova)")
        msg = wt.MSG()
        try:
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:             # WM_HOTKEY
                    if _speaking.is_set():
                        stop_speaking()
                    else:
                        _wake_event.set()
        finally:
            u.UnregisterHotKey(None, 1)

    threading.Thread(target=loop, daemon=True).start()


def run_voice():
    background = "--background" in sys.argv
    v = _get_voice()
    if v is not None:
        threading.Thread(target=v.warm, args=(WARM_PHRASES,), daemon=True).start()
    _start_hotkey()
    if not background:
        say("One moment, getting ready.")
    voice_lock = None
    if VOICE_LOCK and voice_mod:
        vl = voice_mod.VoiceLock()
        if vl.enrolled():
            if vl.load():
                voice_lock = vl
                print(f"   (voice lock active, threshold {vl.threshold:.2f})")
            elif vl.error:
                print(f"   (voice lock disabled: {vl.error})")
    mic = Mic(voice_lock=voice_lock)
    say("Nova is online." if background else f"I'm ready. Just say {NAME} when you need me.")
    if not skills:
        say("My abilities file is missing, so I can only do the basics. Put nova skills dot pie next to my main file.")
    UI.post("hide")
    while True:
        heard = clean(mic.listen(wait=30, wake_event=_wake_event))
        if _wake_event.is_set():
            _wake_event.clear()
            UI.post("wake")
            say(random.choice(["Yes?", "I'm listening.", "Hi! What do you need?"]))
            session(mic)
            if skills:
                skills.reset_ai()
            UI.post("hide")
            continue
        if heard:
            print("   (heard:", heard + ")")
        rest = find_wake(heard)
        if rest is None:
            continue
        UI.post("wake")
        if rest:
            UI.post("heard", rest)
            ended = respond(rest)
            if ended or (not STAY_AWAKE and not awaiting):
                UI.post("hide")
                continue
        else:
            say(random.choice(["Yes?", "I'm listening.", "Hi! What do you need?"]))
        session(mic)
        if skills:
            skills.reset_ai()
        UI.post("hide")


def run_enroll():
    if not voice_mod:
        print("nova_voice.py not found next to assistant.py.")
        return
    try:
        import numpy as np
        import sounddevice as sd
        import sherpa_onnx  # noqa: F401
    except ImportError as err:
        print(f"Voice enrollment needs {err.name}. Run: pip install sherpa-onnx sounddevice numpy")
        return
    lock = voice_mod.VoiceLock()
    lock.load()
    if lock.extractor is None:
        print(f"Could not start speaker recognition: {lock.error or 'unknown error'}")
        return

    def beep():
        try:
            import winsound
            winsound.Beep(880, 140)
        except Exception:
            print("   *BEEP*", flush=True)

    with sd.InputStream(samplerate=16000, channels=1, dtype="float32", device=MIC_DEVICE) as stream:
        def record(seconds):
            avail = stream.read_available
            if avail:
                stream.read(avail)
            beep()
            data, _ = stream.read(int(16000 * seconds))
            return np.asarray(data, np.float32).flatten()

        voice_mod.enroll(lock, record, out=print)


def run_mictest():
    import numpy as np
    import sounddevice as sd
    print("Mic test, 10 seconds. Talk normally. The bar should jump when you speak.")
    with sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                        device=MIC_DEVICE) as stream:
        t0 = time.time()
        while time.time() - t0 < 10:
            lvl = float(np.abs(stream.read(3200)[0]).mean())
            print(f"{lvl:.4f} " + "#" * min(60, int(lvl * 1500)))


def run_text():
    say("Text mode. Type a command.")
    while True:
        try:
            respond(input("\n> "))
        except (KeyboardInterrupt, EOFError):
            break


def setup_logging():
    """Hidden/background runs have no window, so write messages to a log file."""
    if sys.stdout is None or "--background" in sys.argv:
        log = DATA / "nova.log"
        if log.exists() and log.stat().st_size > 1_000_000:
            log.unlink()
        sys.stdout = sys.stderr = open(log, "a", buffering=1, encoding="utf-8")
        print("\n---", datetime.now(), "Nova starting ---")


_lock = None


def already_running():
    """Only one Nova at a time (so startup + manual launch don't double up)."""
    global _lock
    import socket
    _lock = socket.socket()
    try:
        _lock.bind(("127.0.0.1", 47653))
        return False
    except OSError:
        return True


def start_voice():
    global UI
    if "--no-overlay" not in sys.argv:
        try:
            UI = make_overlay()
        except Exception as err:               # e.g. tkinter missing
            print("(popup unavailable:", err, ")")
    if not isinstance(UI, IslandCore):
        run_voice()
        return
    import threading
    import traceback

    def worker():
        try:
            run_voice()
        except SystemExit as bye:
            say(str(bye))
        except BaseException:
            traceback.print_exc()
        finally:
            UI.post("quit")

    threading.Thread(target=worker, daemon=True).start()
    UI.run()                                   # the window must live on the main thread


def run_selftest():
    def row(name, ok, note=""):
        print(f"  {'OK     ' if ok else 'MISSING'}  {name} {note}")

    def has(mod):
        try:
            __import__(mod)
            return True
        except Exception:
            return False
    print("Nova self-test")
    row("Windows", os.name == "nt")
    row("nova_skills.py next to assistant.py", skills is not None)
    row("nova_voice.py next to assistant.py", voice_mod is not None)
    row("nova_memory.py next to assistant.py", memory_mod is not None)
    for mod, why in (("faster_whisper", "hearing"), ("sounddevice", "microphone"), ("numpy", "audio and the Siri orb"),
                     ("edge_tts", "Jarvis British voice (Windows SAPI is the backup)"),
                     ("sherpa_onnx", "voice lock (only listen to you)"),
                     ("pyttsx3", "legacy fallback voice"), ("PIL", "smooth island edges"),
                     ("tkinter", "classic island"), ("anthropic", "optional Claude SDK (HTTPS works without it)")):
        row(mod, has(mod), f"- {why}")
    if voice_mod:
        row("Voiceprint enrolled", voice_mod.VoiceLock().enrolled(), "- run --enroll to set up")
    if skills:
        ready, provider, model = skills.ai_status()
        note = f"({provider}: {model})" if ready else "- run --setup-ai"
        row("AI brain configured", ready, note)
        if os.name == "nt":
            try:
                row("PowerShell works", "ok" in skills.ps("'ok'").stdout)
                row("Start menu apps found", len(skills.start_apps()) > 0, f"({len(skills.start_apps())} apps)")
            except Exception as err:
                row("PowerShell works", False, repr(err))
    if os.name == "nt" and has("PIL"):
        try:
            o = WinOverlay()
            o.u.DestroyWindow(o.hwnd)
            row("Smooth island window", True)
        except Exception as err:
            row("Smooth island window", False, repr(err))
    print("Done. Send me any MISSING lines that matter.")


def main():
    setup_logging()
    if "--selftest" in sys.argv:
        run_selftest()
        return
    if "--setup-ai" in sys.argv:
        skills.setup_ai() if skills else print("nova_skills.py not found.")
        return
    if "--enroll" in sys.argv:
        run_enroll()
        return
    if not any(f in sys.argv for f in ("--text", "--mictest", "--voicetest")) and already_running():
        print("Nova is already running.")
        return
    if not any(f in sys.argv for f in ("--mictest", "--voicetest")):
        setup_skills()
    try:
        if "--voicetest" in sys.argv:
            run_voicetest()
        elif "--mictest" in sys.argv:
            run_mictest()
        elif "--text" in sys.argv:
            run_text()
        else:
            start_voice()
    except SystemExit as bye:
        say(str(bye))
    except KeyboardInterrupt:
        pass
    except ImportError as err:
        print(f"Missing package: {err}. Run: pip install faster-whisper sounddevice numpy pyttsx3")
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        _close_voice()


if __name__ == "__main__":
    main()
