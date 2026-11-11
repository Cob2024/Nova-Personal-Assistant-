"""
Windows Desktop Application Installer & Startup Manager for Nova / Custom Personal Assistant.

Run:
    py -3.12 install_nova.py               Install app to LocalAppData + Start Menu + Desktop + Startup
    py -3.12 install_nova.py --customize   Also open Customization UI (name, user, voice, API key)
    py -3.12 install_nova.py --no-desktop  Skip Desktop shortcut
    py -3.12 install_nova.py --no-startup  Skip auto-start at Windows login
    py -3.12 install_nova.py --remove      Remove Windows login auto-start only
    py -3.12 install_nova.py --uninstall   Remove shortcuts, startup entry, registry entry, and installed files
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "Nova Assistant"
APP_ID = "NovaAssistant"
APP_VERSION = "1.1.0"
RUNTIME_FILES = (
    "assistant.py",
    "nova_skills.py",
    "nova_voice.py",
    "nova_memory.py",
    "nova_calendar.py",
    "install_nova.py",
    "Install_Nova.bat",
    ".env.example",
    "README.md",
    "COMMANDS.md",
)

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _here():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def default_install_dir():
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return base / "Programs" / APP_ID


def startup_vbs_path():
    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Nova.vbs"


def start_menu_dir():
    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME


def desktop_shortcut_path():
    userprofile = Path(os.environ.get("USERPROFILE") or Path.home())
    return userprofile / "Desktop" / f"{APP_NAME}.lnk"


def _resolve_launcher(target_dir):
    """Return (executable, prefix_args) for launching the assistant cleanly."""
    target_dir = Path(target_dir)
    frozen_exe = target_dir / f"{APP_ID}.exe"
    if frozen_exe.exists():
        return frozen_exe, []
    venv_pyw = target_dir / ".venv" / "Scripts" / "pythonw.exe"
    if venv_pyw.exists():
        return venv_pyw, [str(target_dir / "assistant.py")]
    venv_py = target_dir / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        return venv_py, [str(target_dir / "assistant.py")]
    if getattr(sys, "frozen", False):
        return Path(sys.executable), []
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    return (pyw if pyw.exists() else py), [str(target_dir / "assistant.py")]


def _format_cmd(exe, args):
    return " ".join(f'"{p}"' for p in [str(exe), *args])


def _ps(script, **env_vars):
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, creationflags=NOWIN,
        env={**os.environ, **{k: str(v) for k, v in env_vars.items()}},
    )


def create_shortcut(lnk_path, target_exe, arguments="", work_dir="", description=""):
    """Create a Windows .lnk shortcut using WScript.Shell COM."""
    lnk_path = Path(lnk_path)
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    ps_script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$sc = $ws.CreateShortcut($env:NOVA_LNK); "
        "$sc.TargetPath = $env:NOVA_TARGET; "
        "$sc.Arguments = $env:NOVA_ARGS; "
        "$sc.WorkingDirectory = $env:NOVA_DIR; "
        "$sc.Description = $env:NOVA_DESC; "
        "$sc.Save()"
    )
    res = _ps(
        ps_script,
        NOVA_LNK=str(lnk_path),
        NOVA_TARGET=str(target_exe),
        NOVA_ARGS=arguments,
        NOVA_DIR=str(work_dir or Path(target_exe).parent),
        NOVA_DESC=description or APP_NAME,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Failed creating shortcut {lnk_path.name}: {res.stderr.strip()}")
    return lnk_path


def set_autostart(enabled=True, target_dir=None):
    """Enable or disable hidden background startup on Windows login."""
    vbs = startup_vbs_path()
    if not enabled:
        vbs.unlink(missing_ok=True)
        return False
    tdir = Path(target_dir or _here())
    exe, base_args = _resolve_launcher(tdir)
    cmd = _format_cmd(exe, [*base_args, "--background"])
    vbs.parent.mkdir(parents=True, exist_ok=True)
    vbs_escaped = cmd.replace('"', '""')
    vbs.write_text(f'CreateObject("Wscript.Shell").Run "{vbs_escaped}", 0, False\n', encoding="utf-8")
    return True


def register_uninstall(target_dir, uninstall_cmd):
    """Register per-user uninstall metadata so Windows Apps & Features can uninstall cleanly."""
    if os.name != "nt":
        return
    import winreg
    key_path = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "Cobby")
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(target_dir))
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, uninstall_cmd)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def unregister_uninstall():
    if os.name != "nt":
        return
    import winreg
    key_path = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
    except OSError:
        pass


def _copy_app_files(src_dir, dest_dir):
    src, dest = Path(src_dir).resolve(), Path(dest_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    if src == dest:
        return
    if getattr(sys, "frozen", False):
        shutil.copytree(src, dest, dirs_exist_ok=True)
        return
    for name in RUNTIME_FILES:
        f = src / name
        if f.exists() and f.is_file():
            shutil.copy2(f, dest / name)


def install_app(
    src_dir=None,
    dest_dir=None,
    autostart=True,
    desktop=True,
    launch_now=True,
    open_customize=False,
):
    """Install app files, Start Menu/Desktop shortcuts, Add/Remove Programs entry, and startup."""
    if os.name != "nt":
        raise RuntimeError("This installer is for Windows only.")
    src = Path(src_dir or _here()).resolve()
    dest = Path(dest_dir or default_install_dir()).resolve()
    if not getattr(sys, "frozen", False) and not (src / "assistant.py").exists():
        raise FileNotFoundError(f"Cannot find assistant.py in {src}")

    _copy_app_files(src, dest)
    exe, base_args = _resolve_launcher(dest)
    py_console = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists() else exe

    def args_str(*flags):
        return " ".join(f'"{a}"' for a in [*base_args, *flags])

    sm = start_menu_dir()
    create_shortcut(sm / f"{APP_NAME}.lnk", exe, args_str("--background"), dest, "Launch Voice Assistant")
    create_shortcut(sm / "Quick Ask.lnk", exe, args_str("--quick-ask-ui"), dest, "Open Quick Ask Bar")
    create_shortcut(sm / "Customize Assistant.lnk", exe, args_str("--customize"), dest, "Customize Name, User & Voice")

    uninst_target = exe if getattr(sys, "frozen", False) or (dest / f"{APP_ID}.exe").exists() else py_console
    uninst_args = '"--uninstall"' if uninst_target.name.lower() == f"{APP_ID.lower()}.exe" else f'"{dest / "install_nova.py"}" --uninstall'
    create_shortcut(sm / f"Uninstall {APP_NAME}.lnk", uninst_target, uninst_args, dest, f"Uninstall {APP_NAME}")

    if desktop and desktop_shortcut_path().parent.exists():
        create_shortcut(desktop_shortcut_path(), exe, args_str("--background"), dest, "Launch Voice Assistant")

    register_uninstall(dest, f'"{uninst_target}" {uninst_args}')
    set_autostart(autostart, target_dir=dest)

    if open_customize:
        subprocess.Popen([str(exe), *base_args, "--customize"], cwd=str(dest), creationflags=NOWIN)
    if launch_now:
        subprocess.Popen([str(exe), *base_args, "--background"], cwd=str(dest), creationflags=NOWIN)
    return dest


def uninstall_app(dest_dir=None):
    """Stop assistant, remove shortcuts/startup/registry entries, and clean app folder."""
    dest = Path(dest_dir or default_install_dir()).resolve()
    if os.name == "nt":
        _ps(
            "Get-CimInstance Win32_Process | Where-Object { "
            "$_.CommandLine -like '*assistant.py*' -or $_.Name -eq 'NovaAssistant.exe' "
            "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
    set_autostart(False)
    desktop_shortcut_path().unlink(missing_ok=True)
    sm = start_menu_dir()
    if sm.exists():
        shutil.rmtree(sm, ignore_errors=True)
    unregister_uninstall()
    if dest.exists():
        try:
            shutil.rmtree(dest, ignore_errors=True)
        except OSError:
            pass
    if os.name == "nt" and dest.exists():
        subprocess.Popen(["cmd", "/c", "ping", "127.0.0.1", "-n", "3", ">nul", "&", "rmdir", "/s", "/q", str(dest)],
                         creationflags=NOWIN)
    return dest


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if os.name != "nt":
        sys.exit("This installer is for Windows only.")
    dest_dir = None
    if "--dest" in args:
        i = args.index("--dest")
        if i + 1 < len(args):
            dest_dir = args[i + 1]

    if "--uninstall" in args:
        removed = uninstall_app(dest_dir)
        print(f"Uninstalled {APP_NAME} ({removed}).")
        return
    if "--remove" in args:
        set_autostart(False)
        print("Nova will no longer start automatically on Windows login.")
        return

    dest = install_app(
        dest_dir=dest_dir,
        autostart="--no-startup" not in args,
        desktop="--no-desktop" not in args,
        launch_now="--no-start" not in args,
        open_customize="--customize" in args,
    )
    print(f"Installed {APP_NAME} cleanly to: {dest}")
    print("Start Menu & Desktop shortcuts created.")


if __name__ == "__main__":
    main()
