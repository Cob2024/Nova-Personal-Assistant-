# Nova-Personal-Assistant-

Nova is a personal assistant being currently built to handle commands and tasks on your behalf on your Windows laptop — working offline for local voice recognition and system control, with an optional AI brain, long-term memory, and screen awareness.

---

## Features

- **Offline Voice Wake & Transcription**: Listens locally via `faster-whisper` (`small.en` / `base.en`) and `sounddevice`, with adaptive ambient noise tracking.
- **Jarvis-Style Voice & Speaker Lock (`nova_voice.py`)**: British neural TTS (`edge-tts`, `en-GB-RyanNeural`) with MP3 caching, instant persistent Windows SAPI fallback, sentence pre-fetching, barge-in interruption, and optional speaker-embedding verification (`sherpa-onnx`, `--enroll`).
- **Floating Dynamic Island Orb (`assistant.py`)**: Native Win32 layered window (`WinOverlay`) rendering a NumPy Siri-style liquid-glass orb reacting to audio levels, with Tkinter fallback (`TkOverlay`). Left-click stops speech / hides; right-click opens Quick Ask.
- **Streaming Multi-Provider AI Brain (`nova_skills.py`)**: Supports **Ollama Cloud** (`https://ollama.com/v1`, `deepseek-v4.1-flash`), **Anthropic Claude**, **OpenAI / OpenAI-compatible**, **Google Gemini**, and **local offline Ollama** — streaming replies sentence-by-sentence directly into speech with multi-step tool calling.
- **Persistent Personal Memory (`nova_memory.py`)**: SQLite-backed facts and cross-session conversation history (`~/.laptop_assistant/memory.db`) with voice/AI `remember`, `recall`, and `forget`.
- **Active-Window & Screen Awareness**: Reads active foreground window title/app (`ctypes`), captures screenshots for multimodal AI inspection (`"what's on my screen?"`), and summarizes web links (`read_webpage`).
- **Windows Control, Files & Proactive Monitors**: Native Windows Core Audio volume/mute (`IAudioEndpointVolume`), live RAM/uptime/battery/disk diagnostics, top memory process inspection, dynamic app launching/closing, undoable file sorting & trash, recurring reminders/timers/alarms, and proactive low-battery alerts.
- **Global Hotkeys & Local IPC**:
  - `Ctrl+Shift+Space`: Wake Nova directly (no wake word needed) or interrupt Nova mid-speech.
  - `Ctrl+Shift+J`: Open the floating Quick-Ask bar for silent typed commands.

---

## Setup (Python 3.12 on Windows)

```powershell
# Core voice, transcription & smooth orb UI
py -3.12 -m pip install faster-whisper sounddevice numpy pyttsx3 pillow

# Optional: British neural voice + "only listen to my voice" speaker lock
py -3.12 -m pip install edge-tts sherpa-onnx
```

### API Keys (`.env`)

Copy `.env.example` to `.env` (`.env` is gitignored so secrets are never pushed) and set your key, or run `--setup-ai`:

```powershell
py -3.12 assistant.py --setup-ai
```

---

## Usage

```powershell
py -3.12 assistant.py                 # Voice mode + Dynamic Island orb
py -3.12 assistant.py --text          # Text chat mode in terminal
py -3.12 assistant.py --ask "battery" # Send one command to running Nova (or standalone)
py -3.12 assistant.py --enroll        # Record 6-line voiceprint (requires sherpa-onnx)
py -3.12 assistant.py --selftest      # Check dependencies and Windows features
py -3.12 assistant.py --mictest       # Test microphone levels
py -3.12 assistant.py --voicetest     # Test speech output
py -3.12 install_nova.py              # Auto-start Nova silently on Windows login
```

---

## Tests

```powershell
py -3.12 test_nova.py
```
