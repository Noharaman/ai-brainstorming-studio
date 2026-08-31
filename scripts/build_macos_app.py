#!/usr/bin/env python3
"""Build and install macOS Application bundle for AI Brainstorming Studio."""

import os
import subprocess
import sys
from pathlib import Path
from PIL import Image

PROJECT_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = PROJECT_DIR / "assets"
APP_NAME = "AI Brainstorming Studio"
APP_DIR = Path("/Applications") / f"{APP_NAME}.app"
if not os.access(Path("/Applications"), os.W_OK):
    APP_DIR = Path.home() / "Applications" / f"{APP_NAME}.app"


def generate_icon_assets(source_png_path: Path):
    """Generate AppIcon.icns from 1024x1024 PNG."""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    iconset_dir = ASSETS_DIR / "AppIcon.iconset"
    iconset_dir.mkdir(parents=True, exist_ok=True)

    img = Image.open(source_png_path).convert("RGBA")

    # Icon sizes required for macOS icns
    sizes = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]

    for size, filename in sizes:
        resized = img.resize((size, size), Image.Resampling.LANCZOS)
        resized.save(iconset_dir / filename)

    icns_path = ASSETS_DIR / "AppIcon.icns"
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)],
        check=True,
    )
    print(f"Generated ICNS: {icns_path}")
    return icns_path


def create_app_bundle(icns_path: Path):
    """Create .app directory structure and files."""
    contents_dir = APP_DIR / "Contents"
    macos_dir = contents_dir / "MacOS"
    resources_dir = contents_dir / "Resources"

    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)

    # 1. Info.plist
    info_plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>launcher</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>com.aibrainstorm.studio</string>
    <key>CFBundleName</key>
    <string>AI Brainstorming Studio</string>
    <key>CFBundleDisplayName</key>
    <string>AI Brainstorming Studio</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSSupportsAutomaticGraphicsSwitching</key>
    <true/>
</dict>
</plist>
"""
    (contents_dir / "Info.plist").write_text(info_plist, encoding="utf-8")

    # 2. Launcher script
    # Finds python3 and preserves environment (PATH etc.)
    launcher_script = f"""#!/bin/zsh

# Load user profiles to get full PATH (Homebrew, nvm, cargo, agy, claude, codex, etc.)
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$HOME/.local/bin:$PATH"
if [ -f "$HOME/.zprofile" ]; then
    source "$HOME/.zprofile" 2>/dev/null || true
fi
if [ -f "$HOME/.zshrc" ]; then
    source "$HOME/.zshrc" 2>/dev/null || true
fi

PROJECT_DIR="{PROJECT_DIR}"
cd "$PROJECT_DIR" || exit 1

# Detect Python 3.13 / Homebrew Python / Default Python
if [ -x "/opt/homebrew/opt/python@3.13/libexec/bin/python3" ]; then
    PYTHON_EXEC="/opt/homebrew/opt/python@3.13/libexec/bin/python3"
elif [ -x "/opt/homebrew/bin/python3" ]; then
    PYTHON_EXEC="/opt/homebrew/bin/python3"
else
    PYTHON_EXEC="$(which python3)"
fi

export PYTHONPATH="$PROJECT_DIR"
exec "$PYTHON_EXEC" -m src.main
"""
    launcher_path = macos_dir / "launcher"
    launcher_path.write_text(launcher_script, encoding="utf-8")
    launcher_path.chmod(0o755)

    # 3. Copy icns
    dest_icns = resources_dir / "AppIcon.icns"
    dest_icns.write_bytes(icns_path.read_bytes())

    # Touch the app bundle so LaunchServices / Finder refreshes it
    subprocess.run(["touch", str(APP_DIR)], check=False)
    print(f"Created macOS App Bundle: {APP_DIR}")


def add_to_dock(app_path: Path):
    """Add the .app to macOS Dock if not already pinned."""
    app_str = str(app_path)

    # Check if already in Dock
    check_cmd = ["defaults", "read", "com.apple.dock", "persistent-apps"]
    res = subprocess.run(check_cmd, capture_output=True, text=True)
    if app_str in res.stdout or APP_NAME in res.stdout:
        print(f"App is already pinned in Dock: {app_path}")
        return

    # Add to persistent-apps
    xml_entry = f"<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>{app_str}</string><key>_CFURLStringType</key><integer>0</integer></dict></dict></dict>"
    add_cmd = [
        "defaults",
        "write",
        "com.apple.dock",
        "persistent-apps",
        "-array-add",
        xml_entry,
    ]
    subprocess.run(add_cmd, check=True)
    subprocess.run(["killall", "Dock"], check=True)
    print(f"Added {APP_NAME} to macOS Dock!")


def main():
    # The master icon lives in the repository. It used to be read from an
    # absolute path under one developer's home directory, which meant the
    # build only worked on that machine — and put that path into a file
    # intended for publication.
    source_icon = ASSETS_DIR / "app_icon.png"
    if not source_icon.exists():
        print(f"Source icon not found at {source_icon}")
        print("Place a 1024x1024 PNG there and re-run.")
        sys.exit(1)

    icns_path = generate_icon_assets(source_icon)
    create_app_bundle(icns_path)
    add_to_dock(APP_DIR)
    print("\nSUCCESS! You can now launch AI Brainstorming Studio from Applications, Spotlight, or the Dock.")


if __name__ == "__main__":
    main()
