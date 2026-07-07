#!/usr/bin/env python3
"""Build givenergy-dashboard.zip (the Linux / Raspberry Pi release asset).

Uses Python's zipfile so paths use forward slashes and .sh scripts keep LF line
endings + a Unix 0755 bit — unlike PowerShell Compress-Archive, which writes
backslash paths that can extract as literal filenames on Linux.

Stable filename: do NOT version it — install-linux.html hardcodes
`wget .../downloads/givenergy-dashboard.zip`. The VERSION file inside carries the version.

Run from repo root:  venv\\Scripts\\python.exe tools\\build_linux_zip.py
"""
import os
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT  = os.path.join(REPO, "website", "downloads", "givenergy-dashboard.zip")

# Regular files (rw-r--r--)
PLAIN = [
    "VERSION",
    "dashboard_server.py",
    "dashboard.html",
    "manifest.json",
    "sw.js",
    "config.ini.example",
    "generate_icons.py",
    "start_dashboard.bat",
    "stop_dashboard.bat",
]
# Shell scripts — force LF and set the executable bit (rwxr-xr-x)
SCRIPTS = [
    "setup.sh",
    "update.sh",
]


def add_plain(zf, name):
    with open(os.path.join(REPO, name), "rb") as f:
        data = f.read()
    zi = zipfile.ZipInfo(name.replace("\\", "/"))
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = (0o100644 << 16)   # regular file, rw-r--r--
    zi.create_system = 3                  # Unix
    zf.writestr(zi, data)


def add_script(zf, name):
    with open(os.path.join(REPO, name), "r", encoding="utf-8", newline="") as f:
        text = f.read()
    text = text.replace("\r\n", "\n").replace("\r", "\n")   # LF (CRLF breaks shebang)
    zi = zipfile.ZipInfo(name)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = (0o100755 << 16)   # rwxr-xr-x
    zi.create_system = 3                  # Unix
    zf.writestr(zi, text.encode("utf-8"))


def add_dir(zf, rel_dir):
    abs_dir = os.path.join(REPO, rel_dir)
    for fname in sorted(os.listdir(abs_dir)):
        src = os.path.join(abs_dir, fname)
        if os.path.isfile(src):
            add_plain(zf, os.path.join(rel_dir, fname).replace("\\", "/"))


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for n in PLAIN:
            add_plain(zf, n)
        for n in SCRIPTS:
            add_script(zf, n)
        add_dir(zf, "icons/weather")
    print(f"Built {OUT}")
    with zipfile.ZipFile(OUT) as zf:
        names = zf.namelist()
        print(f"  {len(names)} files")
        for zi in zf.infolist():
            perms = (zi.external_attr >> 16) & 0o777
            print(f"  {oct(perms)}  {zi.filename}")


if __name__ == "__main__":
    main()
