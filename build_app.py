"""
Build clean, downloadable Windows installers and portable bundles for Nova Assistant in ./dist:
    py -3.12 build_app.py                Build NovaAssistant-Portable-Windows.zip + NovaAssistant-Setup.exe
    py -3.12 build_app.py --pyinstaller  Also build standalone EXE bundle (requires pyinstaller)

Never bundles local secrets (.env) or personal user data (memory.db, logs, voiceprints).
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
REDIST_FILES = (
    "assistant.py",
    "nova_skills.py",
    "nova_voice.py",
    "nova_memory.py",
    "nova_calendar.py",
    "nova_tasks.py",
    "nova_context.py",
    "install_nova.py",
    "Install_Nova.bat",
    ".env.example",
    "README.md",
    "COMMANDS.md",
)


def stage_clean_files(dest_dir):
    """Copy only clean redistributable app files (no .env secrets or user DBs) into dest_dir."""
    dest = Path(dest_dir)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for name in REDIST_FILES:
        src = ROOT / name
        if not src.exists():
            raise FileNotFoundError(f"Missing required redistributable file: {src}")
        shutil.copy2(src, dest / name)
    return dest


def build_portable_zip(stage_dir, out_dir=DIST):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / "NovaAssistant-Portable-Windows.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(Path(stage_dir).iterdir()):
            if p.is_file():
                zf.write(p, arcname=f"NovaAssistant/{p.name}")
    return zip_path


def build_iexpress_exe(stage_dir, out_dir=DIST):
    """Build a single-file self-extracting Windows .exe installer using built-in iexpress.exe."""
    iexpress = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "iexpress.exe"
    if os.name != "nt" or not iexpress.exists():
        return None
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    exe_path = out / "NovaAssistant-Setup.exe"
    files = [p.name for p in sorted(Path(stage_dir).iterdir()) if p.is_file()]
    if "Install_Nova.bat" not in files:
        raise FileNotFoundError("Install_Nova.bat missing in staged folder")

    with tempfile.TemporaryDirectory() as tmp:
        sed_path = Path(tmp) / "nova_setup.sed"
        lines = [
            "[Version]",
            "Class=IEXPRESS",
            "SEDVersion=3",
            "[Options]",
            "PackagePurpose=InstallApp",
            "ShowInstallProgramWindow=1",
            "HideExtractAnimation=0",
            "UseLongFileName=1",
            "InsideCompressed=0",
            "CAB_FixedSize=0",
            "CAB_ResvCodeSigning=0",
            "RebootMode=N",
            "InstallPrompt=%InstallPrompt%",
            "DisplayLicense=%DisplayLicense%",
            "FinishMessage=%FinishMessage%",
            "TargetName=%TargetName%",
            "FriendlyName=%FriendlyName%",
            "AppLaunched=%AppLaunched%",
            "PostInstallCmd=%PostInstallCmd%",
            "AdminQuietInstCmd=%AdminQuietInstCmd%",
            "UserQuietInstCmd=%UserQuietInstCmd%",
            "SourceFiles=SourceFiles",
            "[Strings]",
            "InstallPrompt=",
            "DisplayLicense=",
            "FinishMessage=",
            f"TargetName={exe_path}",
            "FriendlyName=Nova Personal Assistant Installer",
            "AppLaunched=cmd.exe /c Install_Nova.bat",
            "PostInstallCmd=<None>",
            "AdminQuietInstCmd=",
            "UserQuietInstCmd=",
        ]
        for idx, fname in enumerate(files):
            lines.append(f'FILE{idx}="{fname}"')
        lines.extend(["[SourceFiles]", f"SourceFiles0={Path(stage_dir).resolve()}", "[SourceFiles0]"])
        for idx in range(len(files)):
            lines.append(f"%FILE{idx}%=")
        sed_path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

        res = subprocess.run([str(iexpress), "/N", "/Q", str(sed_path)], capture_output=True, text=True)
        if res.returncode != 0 or not exe_path.exists():
            raise RuntimeError(f"iexpress failed ({res.returncode}): {res.stdout} {res.stderr}")
    return exe_path


def build_pyinstaller_zip(out_dir=DIST):
    """Build a standalone zero-Python-required Windows bundle using PyInstaller."""
    import importlib.util
    if not importlib.util.find_spec("PyInstaller"):
        raise RuntimeError("PyInstaller is not installed. Run: py -3.12 -m pip install pyinstaller")
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "work"
        dist_py = Path(tmp) / "dist"
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--onedir", "--windowed",
            "--name", "NovaAssistant",
            "--workpath", str(work),
            "--distpath", str(dist_py),
            "--specpath", str(Path(tmp)),
            "--collect-all", "faster_whisper",
            "--collect-all", "ctranslate2",
            "--collect-all", "tokenizers",
            "--collect-all", "huggingface_hub",
            "--collect-all", "sounddevice",
            "--collect-all", "edge_tts",
            "--collect-all", "pyttsx3",
            "--collect-all", "PIL",
            "--add-data", f"{ROOT / '.env.example'};.",
            "--add-data", f"{ROOT / 'README.md'};.",
            "--add-data", f"{ROOT / 'COMMANDS.md'};.",
            "--add-data", f"{ROOT / 'install_nova.py'};.",
            str(ROOT / "assistant.py"),
        ]
        res = subprocess.run(cmd, cwd=str(ROOT))
        if res.returncode != 0:
            raise RuntimeError(f"PyInstaller failed with exit code {res.returncode}")
        bundle = dist_py / "NovaAssistant"
        zip_path = out / "NovaAssistant-Standalone-Windows.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in bundle.rglob("*"):
                if p.is_file():
                    zf.write(p, arcname=str(Path("NovaAssistant") / p.relative_to(bundle)))
    return zip_path


def main():
    ap = argparse.ArgumentParser(description="Package Nova Personal Assistant for Windows distribution")
    ap.add_argument("--pyinstaller", action="store_true", help="Also build standalone PyInstaller zip bundle")
    ap.add_argument("--out", default=str(DIST), help="Output directory (default: ./dist)")
    args = ap.parse_args()

    out_dir = Path(args.out).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        staged = stage_clean_files(Path(tmp) / "stage")
        z = build_portable_zip(staged, out_dir=out_dir)
        print("Built portable zip:", z)
        exe = build_iexpress_exe(staged, out_dir=out_dir)
        if exe:
            print("Built Windows installer EXE:", exe)
    if args.pyinstaller:
        standalone = build_pyinstaller_zip(out_dir=out_dir)
        print("Built standalone PyInstaller bundle:", standalone)


if __name__ == "__main__":
    main()
