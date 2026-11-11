"""
Makes Nova start by itself, hidden, every time you log in to Windows.
Run once:      py -3.12 install_nova.py
Undo it:       py -3.12 install_nova.py --remove
Keep this file in the same folder as assistant.py and nova_skills.py.
"""
import os
import subprocess
import sys
from pathlib import Path

if os.name != "nt":
    sys.exit("This installer is for Windows only.")

here = Path(__file__).resolve().parent
script = here / "assistant.py"
startup = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
launcher = startup / "Nova.vbs"

if "--remove" in sys.argv:
    launcher.unlink(missing_ok=True)
    sys.exit("Nova will no longer start automatically.")

if not script.exists():
    sys.exit(f"I can't find assistant.py next to this file ({here}).")

if not (here / "nova_skills.py").exists():
    print("Note: nova_skills.py is not in this folder. Nova will start, but without the new abilities")
    print("(apps, timers, reminders, file moves, notes, AI). Put it next to assistant.py and run this again.")

if not (here / "nova_voice.py").exists():
    print("Note: nova_voice.py is not in this folder. Nova will use basic speech instead of the")
    print("Jarvis voice and speaker lock.")

py = Path(sys.executable)
exe = py.with_name("pythonw.exe")
exe = exe if exe.exists() else py

# A tiny VBScript starts Nova with its window hidden (style 0).
launcher.write_text(
    f'CreateObject("Wscript.Shell").Run """{exe}"" ""{script}"" --background", 0, False\n')
print("Done. Nova will start automatically when you log in.")

if "--no-start" not in sys.argv:
    subprocess.Popen([str(exe), str(script), "--background"], creationflags=0x08000000)
    print("Starting Nova now. In a few seconds you should hear 'Nova is online.'")
