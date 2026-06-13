#!/usr/bin/env python3
"""Build givenergy-dashboard-mac.zip with correct Unix permissions.

The .command files MUST keep their Unix 0755 execute bit and create_system=3
(Unix) so they are double-clickable on macOS, and must use LF line endings
(CRLF breaks the shebang). Regular files are added normally.

Run from repo root:  venv\\Scripts\\python.exe tools\\build_mac_zip.py
"""
import os
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT  = os.path.join(REPO, "website", "downloads", "givenergy-dashboard-mac.zip")

# (filename, is_executable_command)
PLAIN = [
    "VERSION",
    "dashboard_server.py",
    "dashboard.html",
    "manifest.json",
    "sw.js",
    "generate_icons.py",
    "config.ini.example",
    "MAC-README.txt",
]
COMMANDS = [
    "setup-mac.command",
    "start-dashboard.command",
    "stop-dashboard.command",
]


def add_plain(zf, name):
    with open(os.path.join(REPO, name), "rb") as f:
        data = f.read()
    zi = zipfile.ZipInfo(name)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = (0o100644 << 16)   # regular file, rw-r--r--
    zi.create_system = 3                  # Unix
    zf.writestr(zi, data)


def add_command(zf, name):
    # Read as text, force LF line endings (CRLF breaks the shebang on macOS)
    with open(os.path.join(REPO, name), "r", encoding="utf-8", newline="") as f:
        text = f.read()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    zi = zipfile.ZipInfo(name)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = (0o100755 << 16)   # regular file, rwxr-xr-x (executable)
    zi.create_system = 3                  # Unix
    zf.writestr(zi, text.encode("utf-8"))


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for n in PLAIN:
            add_plain(zf, n)
        for n in COMMANDS:
            add_command(zf, n)
    # Verify
    print(f"Built {OUT}")
    with zipfile.ZipFile(OUT) as zf:
        for zi in zf.infolist():
            perms = (zi.external_attr >> 16) & 0o777
            sysname = {0: "FAT", 3: "Unix"}.get(zi.create_system, zi.create_system)
            print(f"  {oct(perms)}  {sysname:5s}  {zi.filename}")


if __name__ == "__main__":
    main()
