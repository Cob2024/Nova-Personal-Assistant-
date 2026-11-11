"""
Nova's voice: a Jarvis-style speaking voice, and "only listen to me" speaker recognition.
Keep this file next to assistant.py. Optional extras:
    pip install edge-tts       a natural British neural voice (needs internet; falls back to Windows' George)
    pip install sherpa-onnx    lets Nova listen only to your voice (needs one-time `--enroll`)
"""
import asyncio
import base64
import ctypes
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
DATA = HOME / ".laptop_assistant"
CACHE, MODELS, VOICEPRINT = DATA / "tts_cache", DATA / "models", DATA / "voiceprint.json"
for _d in (DATA, CACHE, MODELS):
    _d.mkdir(exist_ok=True)
IS_WIN = os.name == "nt"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

for _f in CACHE.glob("*.mp3"):                          # keep the phrase cache tidy
    try:
        if time.time() - _f.stat().st_mtime > 60 * 86400:
            _f.unlink()
    except OSError:
        pass


# =============================== speaking ===============================
# One long-lived PowerShell process holds Windows' speech engine, so each reply starts instantly
# instead of paying ~0.5s to launch PowerShell every time.
_SAPI_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$names = $s.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { $_.VoiceInfo.Name }
foreach ($h in ($env:NOVA_VOICE_HINTS -split ',')) {
    $m = $names | Where-Object { $_ -like "*$h*" } | Select-Object -First 1
    if ($m) { $s.SelectVoice($m); break }
}
$s.Rate = [int]$env:NOVA_RATE
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::Out.WriteLine('ready: ' + $s.Voice.Name); [Console]::Out.Flush()
while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if ($line.Trim() -ne '') {
        try {
            $esc = [System.Security.SecurityElement]::Escape($line)
            $lang = $s.Voice.Culture.Name
            $s.SpeakSsml("<speak version='1.0' xml:lang='$lang' xmlns='http://www.w3.org/2001/10/synthesis'><prosody pitch='$($env:NOVA_PITCH)'>$esc</prosody></speak>")
        } catch { try { $s.Speak($line) } catch {} }
    }
    [Console]::Out.WriteLine('done'); [Console]::Out.Flush()
}
'''


class _Sapi:
    def __init__(self, hints, rate, pitch):
        encoded = base64.b64encode(_SAPI_SCRIPT.encode("utf-16le")).decode()
        self.p = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", bufsize=1, creationflags=NOWIN,
            env={**os.environ, "NOVA_VOICE_HINTS": ",".join(hints), "NOVA_RATE": str(rate), "NOVA_PITCH": pitch})
        first = self.p.stdout.readline()
        if not first.startswith("ready"):
            raise OSError("Windows speech engine did not start")
        self.voice_name = first.partition(":")[2].strip()

    def say(self, text):
        self.p.stdin.write(" ".join(text.split()) + "\n")
        self.p.stdin.flush()
        if self.p.stdout.readline() == "":
            raise OSError("Windows speech engine stopped")

    def close(self):
        try:
            self.p.stdin.close()
            self.p.terminate()
        except Exception:
            pass


def _play_mp3(path, stop_event=None):
    """Play an mp3 with Windows' built-in MCI player (interruptible when stop_event is set)."""
    if not IS_WIN:
        raise OSError("mp3 playback needs Windows")
    mci = ctypes.windll.winmm.mciSendStringW
    mci.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
    mci.restype = ctypes.c_uint
    alias = f"nova{threading.get_ident()}"
    mci(f"close {alias}", None, 0, None)
    if stop_event is None:
        for cmd in (f'open "{path}" type mpegvideo alias {alias}', f"play {alias} wait"):
            err = mci(cmd, None, 0, None)
            if err:
                mci(f"close {alias}", None, 0, None)
                raise OSError(f"audio player error {err}")
        mci(f"close {alias}", None, 0, None)
        return
    for cmd in (f'open "{path}" type mpegvideo alias {alias}', f"play {alias}"):
        err = mci(cmd, None, 0, None)
        if err:
            mci(f"close {alias}", None, 0, None)
            raise OSError(f"audio player error {err}")
    buf = ctypes.create_unicode_buffer(64)
    try:
        while not stop_event.is_set():
            if mci(f"status {alias} mode", buf, 64, None):
                break
            if buf.value.strip().lower() not in ("playing", "paused"):
                break
            stop_event.wait(0.03)
    finally:
        mci(f"stop {alias}", None, 0, None)
        mci(f"close {alias}", None, 0, None)


def _split_sentences(text):
    text = " ".join(text.split())
    if len(text) <= 120:
        return [text] if text else []
    raw = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    out = []
    for part in raw:
        if out and len(out[-1]) < 24:
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return out or [text]


class Voice:
    """Speaks text. Engines: 'neural' (edge-tts British voice, online) and 'sapi' (Windows' own voice, instant)."""

    def __init__(self, engine="auto", name="en-GB-RyanNeural", rate="-4%", pitch="-6Hz",
                 sapi_hints=("George", "Ryan", "Hazel", "Susan", "David"), sapi_pitch="low", sapi_rate=0):
        self.engine, self.name, self.rate, self.pitch = engine, name, rate, pitch
        self.sapi_args = (sapi_hints, sapi_rate, sapi_pitch)
        self._sapi, self.failed, self._neural_retry_at, self.last_engine = None, set(), 0.0, None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def stop(self):
        """Interrupt any active speech immediately."""
        self._stop.set()
        sapi = self._sapi
        if sapi is not None:
            sapi.close()

    def say(self, text):
        """Speak and wait until finished (or interrupted via stop()). Returns the engine used, or None."""
        order = {"neural": ["neural"], "sapi": ["sapi"]}.get(self.engine, ["neural", "sapi"])
        chunks = _split_sentences(text)
        if not chunks:
            return None
        with self._lock:
            self._stop.clear()
            for eng in order:
                if eng in self.failed or (eng == "neural" and time.time() < self._neural_retry_at):
                    continue
                try:
                    if eng == "neural":
                        self._speak_neural_chunks(chunks)
                    else:
                        for chunk in chunks:
                            if self._stop.is_set():
                                break
                            self._windows(chunk)
                    self.last_engine = eng
                    return eng
                except ImportError:                       # edge-tts isn't installed: don't keep trying
                    self.failed.add(eng)
                    print("   (edge-tts is not installed, so I'm using the Windows voice. "
                          "For the natural British voice: pip install edge-tts)", flush=True)
                except Exception as err:
                    if self._stop.is_set():               # interrupted cleanly via stop()
                        self.last_engine = eng
                        return eng
                    if eng == "neural":                   # probably offline: skip it for a minute
                        self._neural_retry_at = time.time() + 60
                    else:
                        self.failed.add(eng)
                    print(f"   (voice engine {eng} failed: {err})", flush=True)
        return None

    def _speak_neural_chunks(self, chunks):
        for i, chunk in enumerate(chunks):
            if self._stop.is_set():
                break
            prefetch = None
            if i + 1 < len(chunks):
                nxt = chunks[i + 1]
                prefetch = threading.Thread(target=self._fetch_neural, args=(nxt,), daemon=True)
                prefetch.start()
            self._neural(chunk)
            if prefetch is not None and not self._stop.is_set():
                prefetch.join(timeout=0.5)

    def _windows(self, text):
        if not IS_WIN:
            raise OSError("the Windows voice needs Windows")
        if self._stop.is_set():
            return
        if self._sapi is None or self._sapi.p.poll() is not None:
            self._sapi = _Sapi(*self.sapi_args)
        self._sapi.say(text)

    def _fetch_neural(self, text):
        import edge_tts
        key = hashlib.sha1(f"{self.name}|{self.rate}|{self.pitch}|{text}".encode("utf-8")).hexdigest()
        path = CACHE / f"{key}.mp3"
        if not path.exists():
            tmp = CACHE / f"{key}.{threading.get_ident()}.part"

            async def fetch():
                await asyncio.wait_for(
                    edge_tts.Communicate(text, self.name, rate=self.rate, pitch=self.pitch).save(str(tmp)), 10)
            asyncio.run(fetch())
            if not path.exists():
                os.replace(tmp, path)
            else:
                tmp.unlink(missing_ok=True)
        return path

    def _neural(self, text):
        path = self._fetch_neural(text)
        if not self._stop.is_set():
            _play_mp3(str(path), stop_event=self._stop)

    def warm(self, phrases):
        """Pre-render common phrases (neural voice only) so they play instantly later."""
        if self.engine == "sapi":
            return
        try:
            import edge_tts
        except ImportError:
            return
        for text in phrases:
            key = hashlib.sha1(f"{self.name}|{self.rate}|{self.pitch}|{text}".encode("utf-8")).hexdigest()
            path = CACHE / f"{key}.mp3"
            if path.exists():
                continue
            try:
                tmp = CACHE / f"{key}.{threading.get_ident()}.part"

                async def fetch():
                    await asyncio.wait_for(
                        edge_tts.Communicate(text, self.name, rate=self.rate, pitch=self.pitch).save(str(tmp)), 10)
                asyncio.run(fetch())
                if not path.exists():
                    os.replace(tmp, path)
                else:
                    tmp.unlink(missing_ok=True)
            except Exception:
                return

    def close(self):
        if self._sapi:
            self._sapi.close()


# ========================= "only listen to me" =========================
MODEL_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
MODEL_FILES = ("3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx",      # small, English-trained (28 MB)
               "wespeaker_en_voxceleb_resnet34_LM.onnx",
               "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")
ENROLL_LINES = ("Nova, what's the weather like today?",
                "Nova, open my recycle bin.",
                "Nova, set a timer for ten minutes.",
                "Nova, add a task to call the bank tomorrow.",
                "Nova, turn the volume down a little, please.",
                "Nova, remind me at five to stretch my legs.")


def ensure_model():
    """The speaker-recognition model file (downloaded once, about 28 MB)."""
    for name in MODEL_FILES:
        path = MODELS / name
        if path.exists():
            return path
    last = None
    for name in MODEL_FILES:
        path, tmp = MODELS / name, MODELS / (name + ".part")
        try:
            print(f"Downloading the voice model {name} (one time)...", flush=True)

            def hook(blocks, size, total):
                if total > 0 and blocks % 40 == 0:
                    print(f"   {min(100, blocks * size * 100 // total)}%", flush=True)
            urllib.request.urlretrieve(MODEL_BASE + name, tmp, hook)
            os.replace(tmp, path)
            return path
        except Exception as err:
            last = err
    raise RuntimeError(f"could not download the voice model ({last})")


def trim_silence(audio, sr=16000):
    """Cut the quiet parts off both ends of a recording."""
    import numpy as np
    frame = sr // 50
    n = len(audio) // frame
    if n < 3:
        return audio
    energy = np.abs(audio[:n * frame]).reshape(n, frame).mean(axis=1)
    thr = max(0.008, float(np.percentile(energy, 95)) * 0.15)
    loud = np.where(energy > thr)[0]
    if not len(loud):
        return audio[:0]
    return audio[max(0, (loud[0] - 5) * frame):(loud[-1] + 6) * frame]


class VoiceLock:
    """Compares each utterance with your voiceprint (a speaker embedding), so Nova ignores everyone else."""

    def __init__(self):
        self.extractor, self.vp, self.np, self.error, self.threshold = None, None, None, None, 0.5

    def enrolled(self):
        return VOICEPRINT.exists()

    @property
    def ready(self):
        return self.extractor is not None and self.vp is not None

    def load(self):
        """Start the recogniser (downloads the model the first time). Returns True if the lock is usable."""
        try:
            import numpy as np
            import sherpa_onnx
            path = ensure_model()
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(path), num_threads=2, debug=False,
                                                              provider="cpu")
            if hasattr(cfg, "validate") and not cfg.validate():
                raise RuntimeError("the voice model file is not valid")
            self.extractor, self.np = sherpa_onnx.SpeakerEmbeddingExtractor(cfg), np
            if VOICEPRINT.exists():
                data = json.loads(VOICEPRINT.read_text())
                if data.get("model") == path.name:
                    self.vp = np.array(data["embedding"], np.float32)
                    self.threshold = float(data.get("threshold", 0.5))
                else:
                    self.error = "the voice model changed, so please run --enroll again"
        except Exception as err:
            self.error = repr(err)
            self.extractor = None
        return self.ready

    def embed(self, audio):
        np = self.np
        stream = self.extractor.create_stream()
        stream.accept_waveform(16000, np.asarray(audio, np.float32))
        stream.input_finished()
        e = np.array(self.extractor.compute(stream), np.float32)
        return e / max(float(np.linalg.norm(e)), 1e-6)

    def score(self, audio):
        """Cosine similarity with your voiceprint (about 0.5-0.9 for you, much lower for others)."""
        if not self.ready:
            return None
        return float(self.np.dot(self.embed(audio), self.vp))

    def save_voiceprint(self, takes):
        np = self.np
        embs = [self.embed(t) for t in takes]
        vp = np.mean(embs, axis=0)
        vp = vp / max(float(np.linalg.norm(vp)), 1e-6)
        scores = [float(np.dot(e, vp)) for e in embs]
        threshold = round(min(0.55, max(0.30, sum(scores) / len(scores) - 0.25)), 2)
        VOICEPRINT.write_text(json.dumps({"model": next(m for m in MODEL_FILES if (MODELS / m).exists()),
                                          "embedding": [float(x) for x in vp], "scores": scores,
                                          "threshold": threshold, "created": time.time()}))
        self.vp, self.threshold = vp.astype(np.float32), threshold
        return scores, threshold


def enroll(lock, record, out=print):
    """Guided enrolment. `record(seconds)` must return mono 16 kHz float32 audio."""
    takes = []
    out("I'll ask you to read six short lines. After each beep, read the line out loud, naturally.")
    for i, line in enumerate(ENROLL_LINES, 1):
        for attempt in range(3):
            out(f"\n({i}/{len(ENROLL_LINES)})  Say:   {line}")
            speech = trim_silence(record(6.0))
            if len(speech) >= 16000 * 1.2:
                takes.append(speech)
                break
            out("   I didn't catch enough of that. Let's try that line again.")
    if len(takes) < 3:
        raise RuntimeError("not enough clear speech was recorded; check your microphone and try again")
    scores, threshold = lock.save_voiceprint(takes)
    out(f"\nSaved your voiceprint. Your lines matched it at {min(scores):.2f} to {max(scores):.2f}. "
        f"Nova will accept speech scoring above about {threshold:.2f}.")
    return scores, threshold
