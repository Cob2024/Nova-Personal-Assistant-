# Nova Laptop Command Reference

Everything listed under **Offline Laptop Commands** works **100% locally on your Windows laptop with zero AI API credits**.  
Optional **Internet** commands fetch live data (weather, Wikipedia summaries, webpage text) without spending AI credits.  
**AI Brain** commands require an active API plan/credits (or local free Ollama).

---

## 1. Waking Nova, Hotkeys & Session Control

| Action | Voice Phrase / Shortcut | Notes |
|---|---|---|
| Wake Nova by voice | `"Nova"` or `"Nova, <command>"` | Checks the first 5 words (`nova`, `novah`, `novo`, `nover`, `no va`, `nowa`). |
| Instant wake / interrupt speech | `Ctrl + Shift + Space` | Wakes Nova directly without saying `"Nova"`, or stops speaking mid-reply. |
| Quick-Ask floating bar | `Ctrl + Shift + J` | Type any command or question (`Enter` = silent inline reply, `Shift + Enter` = speak aloud). Also available via right-click on the Dynamic Island orb. |
| Left-click orb | Click the floating orb | Interrupts speech or hides the orb. |
| Stop current conversation | `"Thanks"`, `"Thank you"`, `"That's all"`, `"Goodbye"`, `"Bye"`, `"Never mind"` | Ends the active follow-up session cleanly. |
| Cancel a pending prompt | `"Cancel"`, `"Never mind"`, `"Forget it"`, `"Forget about that one"`, `"Skip"`, `"No"` | Cancels confirmations or number pickers safely. |
| Recalibrate microphone noise floor | `"Recalibrate microphone"` | Re-measures background room noise live in voice mode. |
| Shut down Nova | `"Quit Nova"`, `"Exit Nova"`, `"Shut down Nova"` | Stops Nova cleanly. |

> **CLI shortcuts** (run from terminal):
> - `py -3.12 assistant.py --ask "battery"` — send any single command to running Nova (or run standalone)
> - `py -3.12 assistant.py --quick-ask-ui` — open the quick-ask bar manually
> - `py -3.12 assistant.py --selftest` — verify packages, audio, Start Menu apps, and AI status
> - `py -3.12 assistant.py --mictest` / `--voicetest` — test microphone input levels or TTS output
> - `py -3.12 assistant.py --enroll` — enroll your voiceprint so Nova ignores other speakers (requires `sherpa-onnx`)

---

## 2. Opening & Closing Apps, Websites, Settings & Folders (Offline)

### Built-in Apps (`open <name>` / `launch <name>` / `start <name>`)
- **Browsers & Office**: `chrome` (`google chrome`), `edge` (`microsoft edge`), `firefox`, `word`, `excel`, `powerpoint`, `outlook`, `notepad`, `paint`
- **Dev & System**: `vs code` (`vscode`, `visual studio code`), `terminal`, `command prompt` (`cmd`), `task manager`, `file explorer` (`explorer`), `control panel`, `snipping tool`, `calculator`
- **Communication & Media**: `whatsapp` (also catches Whisper's `"what's up"` / `"whats up"`), `telegram`, `discord`, `slack`, `teams`, `zoom`, `spotify`, `vlc`, `camera`, `store`, `clock`
- **Any Start Menu app**: `"Open <installed app name>"` searches all installed Windows Start Menu apps automatically.
- **Close apps**: `"Close Chrome"`, `"Quit Notepad"`, `"Kill Spotify"`, `"Exit VS Code"`, `"Close WhatsApp"` *(checks built-in process map and all running `.exe` names politely without forcing unsaved data loss)*.
- **Switch to app window**: `"Switch to Chrome"`, `"Switch to VS Code"`.

### Websites (`open <site>` / `go to <site>` / `visit <site>`)
- Shortcuts: `youtube`, `google`, `gmail`, `github`, `facebook`, `instagram`, `twitter` (`x`), `linkedin`, `netflix`, `amazon`, `reddit`, `wikipedia`, `maps`, `claude`, `google drive`, `whatsapp web`
- Direct domains: `"Open example.com"` or `"Open github dot com"` (`.com`, `.org`, `.net`, `.io`, `.co`, `.edu`, `.gov`, `.ai`, `.app`)
- Web search: `"Google <query>"`, `"Look up <query>"`, `"Search the web for <query>"`

### Windows Settings Pages (`open <setting> settings`)
`wifi`, `network`, `bluetooth`, `display`, `sound`, `battery`, `power`, `update` (`windows update`), `privacy`, `microphone`, `camera`, `storage`, `apps`, `default apps`, `wallpaper` (`background`), `personalization`, `night light`, `about`, `notifications`, `accounts`, `taskbar`, `airplane mode`, `vpn`, `mouse`, `touchpad`

### System Places & Folders
- **Special Windows places**: `"Open Recycle Bin"`, `"Open This PC"`, `"Open Network"`, `"Open Startup folder"`, `"Open Device Manager"`, `"Open Disk Management"`, `"Open Services"`, `"Open Event Viewer"`, `"Open System Information"`, `"Open On Screen Keyboard"`, `"Open Magnifier"`
- **User folders**: `"Open Downloads"`, `"Open Desktop"`, `"Open Documents"`, `"Open Pictures"`, `"Open Videos"`, `"Open Music"`
- **Latest files & notes**: `"Open latest download"`, `"Open latest screenshot"`, `"Open my notes"`

---

## 3. Window, Browser Tab, Scrolling & Editing Controls (Offline)

| Command | Action |
|---|---|
| `"Take a screenshot"` | Saves full primary screen PNG into `Pictures/Screenshots` |
| `"Maximize window"` / `"Minimize window"` | Maximizes or minimizes/restores the active window (`Win+Up` / `Win+Down`) |
| `"Snap window left"` / `"Move window right"` / `"Tile window left"` | Tiles the active window to left or right half (`Win+Left` / `Win+Right`) |
| `"Minimize all windows"` / `"Show desktop"` | Minimizes everything (`Win+D`) |
| `"Close this window"` / `"Close it"` | Closes foreground window (`Alt+F4`) |
| `"Switch window"` / `"Alt tab"` | Switches between open windows (`Alt+Tab`) |
| `"New tab"` / `"Close tab"` / `"Reopen last tab"` | `Ctrl+T` / `Ctrl+W` / `Ctrl+Shift+T` |
| `"Next tab"` / `"Previous tab"` | `Ctrl+Tab` / `Ctrl+Shift+Tab` |
| `"Refresh page"` / `"Reload tab"` / `"Find on page"` | `Ctrl+R` / `Ctrl+F` |
| `"Copy"` / `"Paste"` / `"Cut that"` / `"Select all"` | `Ctrl+C` / `Ctrl+V` / `Ctrl+X` / `Ctrl+A` |
| `"Undo typing"` / `"Redo"` | `Ctrl+Z` / `Ctrl+Y` |
| `"Scroll down"` / `"Scroll up"` / `"Scroll to top"` / `"Scroll to bottom"` | `PageDown` / `PageUp` / `Ctrl+Home` / `Ctrl+End` |

---

## 4. Volume, Brightness & Media Control (Offline)

- **Volume query & control** (Windows Core Audio):
  - `"What's the volume?"` / `"Check volume"`
  - `"Volume up"` / `"Louder"` / `"Volume down"` / `"Quieter"`
  - `"Volume up by 20"` / `"Turn the sound down by 15"`
  - `"Set volume to 50"` / `"Max volume"`
  - `"Mute"` / `"Unmute"`
- **Brightness query & control** (laptop displays supporting WMI):
  - `"What's the brightness?"`
  - `"Brightness up"` / `"Dim the screen"` / `"Brightness down"`
  - `"Set brightness to 70"`
- **Media playback**:
  - `"Play music"` / `"Pause music"` / `"Resume video"`
  - `"Next song"` / `"Skip"`
  - `"Previous song"` / `"Go back"`
  - `"Play <song or artist> on YouTube"` / `"Play <song or artist> on Spotify"`

---

## 5. Power, Security & Laptop Health (Offline)

- **Lock & Power** (destructive actions ask yes/no confirmation first):
  - `"Lock the laptop"` / `"Lock screen"`
  - `"Put the laptop to sleep"` *(asks confirmation)*
  - `"Shut down the computer"` *(asks confirmation -> 30s countdown)*
  - `"Restart the laptop"` *(asks confirmation -> 30s countdown)*
  - `"Cancel shutdown"` / `"Cancel restart"`
- **Hardware, Network & Running Apps**:
  - `"Battery"` / `"How much battery is left?"` *(Nova also proactively warns below 15% when unplugged)*
  - `"How much disk space is left?"` / `"Storage space"`
  - `"System diagnostics"` / `"Check my PC"` / `"Memory usage"` / `"Uptime"`
  - `"Top apps"` / `"What is using the most RAM?"` / `"What's slowing down my laptop?"`
  - `"What apps are running?"` / `"List running apps"`
  - `"What window am I in?"` / `"Active window"`
  - `"What's my Wi-Fi?"` / `"What's my IP address?"` / `"Am I online?"`

---

## 6. Files, Folders, Notes, Dictation & Clipboard (Offline)

- **Organize & clean up**:
  - `"Organize my Downloads"` / `"Clean up Desktop"` / `"Tidy Documents"` *(sorts files by type: Images, Documents, Videos, Audio, Archives, Code, Installers, Other — asks confirmation first)*
  - `"Undo"` / `"Put them back"` *(reverses the last file move or trash action)*
  - `"Find the invoice file"` / `"Locate <name>"` / `"Search for <name>"` *(searches Desktop, Documents, Downloads, Pictures, Videos, Music)*
  - `"What are my biggest files?"`
  - `"Create a folder called Reports in Documents"` *(Desktop, Documents, Downloads, Pictures, Videos, Music)*
  - `"Move all PDFs from Downloads to Documents"` *(supports `word`, `excel`, `spreadsheet`, `powerpoint`, `presentation`, `text`, `pdf`, `image`/`photo`/`picture`/`screenshot`, `jpg`, `png`, `video`, `music`/`song`/`audio`, `zip`/`archive`, `installer`, `document`)*
  - `"Delete the file report"` *(asks confirmation and moves to Nova's undoable trash folder, not permanent delete)*
  - `"Empty Recycle Bin"` *(asks confirmation before permanent Windows Recycle Bin clear)*
- **Notes, Typing & Clipboard**:
  - `"Take a note that <text>"` / `"Jot down <text>"`
  - `"Read my notes"` / `"Show notes"`
  - `"Dictate"` / `"Start dictating"` -> types everything you speak into the focused window until `"Stop dictating"`
  - `"Type <text>"` -> types a single phrase directly
  - `"Read clipboard"` / `"What did I copy?"`
  - `"Clear clipboard"` / `"Empty clipboard"`

---

## 7. Time, Timers, Alarms, Reminders, Calendar & Tasks (Offline)

- **Clock**: `"What time is it?"`, `"What's the date?"`
- **Timers & Alarms**:
  - `"Set a timer for 10 minutes"` / `"Countdown 45 seconds"`
  - `"Set an alarm for 7 AM"` / `"Set an alarm in 2 hours"`
  - `"List my timers"` / `"How much time is left on my timer?"`
  - `"Cancel timers"` / `"Cancel all reminders"`
- **Reminders (one-time & recurring)**:
  - `"Remind me to stretch at 3 PM"`
  - `"Remind me in 30 minutes to drink water"`
  - `"Remind me every day at 9 AM to check email"`
  - `"Remind me every Monday at 10 AM to submit report"`
- **Calendar** (local events + `.ics` files in `~/.laptop_assistant/calendars/` or `CALENDAR_ICS_URL` in `.env`; alerts automatically 10 minutes before meetings):
  - `"What's on my calendar today?"` / `"Show schedule tomorrow"` / `"Calendar this week"`
  - `"Add calendar event Team Sync tomorrow at 2 PM"`
- **To-Do List**:
  - `"Add task finish slides"` / `"Remind me to call John"` *(when no time is specified)*
  - `"List my tasks"` / `"What do I need to do?"`
  - `"Mark task 1 as done"` / `"Complete task two"`
  - `"Delete task 3"`

---

## 8. Routines & Developer Tools (Offline)

- **Built-in routines** (say the name directly or `"Start <routine>"`):
  - `"Focus mode"` -> volume 25%, opens VS Code, sets 50-minute timer
  - `"Break time"` -> volume 50%, sets 10-minute timer, lists tasks
  - `"Morning routine"` -> daily briefing, lists tasks
  - `"Night mode"` -> brightness 25%, volume 25%
- **Manage custom routines**:
  - `"List routines"`
  - `"Create routine study time to set volume to 20 and set a timer for 25 minutes"`
  - `"Delete routine study time"`
- **Briefing, Git, GitHub & Shell**:
  - `"Daily briefing"` / `"Brief me"` / `"Good morning"` *(time, date, open tasks, today's reminders/calendar, low-battery warning, weather)*
  - `"Git status"` / `"Git status in Nova"` / `"What branch am I on?"` *(read-only)*
  - `"GitHub status"` / `"My pull requests"` *(read-only via `gh` CLI)*
  - `"Run command ipconfig"` / `"Execute command python --version"` *(always asks yes/no confirmation first)*

---

## 9. Offline Calculator & Unit Converter (Zero AI Credits)

- **Math**:
  - `"What is 15 percent of 240?"` / `"20% off 80"`
  - `"Square root of 144"`
  - `"What is 12 times 15?"`, `"Calculate 144 divided by 12"`, `"Calculate (5 + 3) * 4"`, `"2 to the power of 10"`
- **Unit conversions**:
  - Length: `"Convert 100 km to miles"`, `"12 feet in cm"`, `"24 inches to meters"` (`mm`, `cm`, `m`, `km`, `in`, `ft`, `yd`, `mi`)
  - Weight: `"10 kg to pounds"`, `"500 grams in ounces"` (`mg`, `g`, `kg`, `oz`, `lb`/`lbs`)
  - Temperature: `"72 fahrenheit to celsius"`, `"25 c to f"`, `"300 kelvin to celsius"`
  - Storage & Volume & Speed: `"64 gigabytes in megabytes"`, `"2 liters to cups"`, `"60 mph to kph"`

---

## 10. Memory, Web & Optional AI Features

- **Persistent local memory** (`~/.laptop_assistant/memory.db`, works offline):
  - `"Remember that my favorite coffee is espresso"` / `"Remember my Wi-Fi password is ..."`
  - `"What do you remember about coffee?"`
  - `"Forget favorite coffee"`
- **Live web (no AI credits required)**:
  - `"Weather"` / `"Weather in London"` / `"Will it rain?"`
  - `"Who is Ada Lovelace?"` / `"What is photosynthesis?"` / `"Define entropy"` *(uses Wikipedia REST automatically offline or while AI credits are on cooldown)*
  - `"Read webpage https://..."` / `"Summarize link"` *(reads clipboard URL or spoken URL offline when AI is unavailable)*
- **Requires AI credits or local Ollama** (`py -3.12 assistant.py --setup-ai`):
  - `"What's on my screen?"` / `"Describe this screen"` *(multimodal screen vision)*
  - `"Summarize my clipboard"` / `"Proofread my clipboard"` / `"Translate my clipboard"`
  - Open-ended Q&A, multi-step tool chaining, and conversational follow-ups
