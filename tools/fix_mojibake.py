#!/usr/bin/env python3
"""Fix UTF-8-as-Windows-1252 mojibake in the website HTML files.

Sequences like "â€”" are an em-dash (—) whose UTF-8 bytes (E2 80 94) were
decoded as Windows-1252 (â € ") and re-saved. This replaces each exact
mojibake sequence with the correct character. Line endings are preserved.

Run from repo root:  venv\\Scripts\\python.exe tools\\fix_mojibake.py
"""
import os
import glob

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB  = os.path.join(REPO, "website")

# mojibake (3 chars: U+00E2 U+20AC X) -> correct character
FIX = {
    "â€”": "—",  # â€"  -> — em-dash
    "â€“": "–",  # â€"  -> – en-dash
    "â€¦": "…",  # â€¦  -> … ellipsis
    "â€¹": "‹",  # â€¹  -> ‹ single left angle quote
    "â€º": "›",  # â€º  -> › single right angle quote
    "â†’": "→",  # â†'  -> → right arrow
    # defensive (harmless if absent):
    "â€™": "’",  # â€™  -> ' apostrophe / right single quote
    "â€œ": "“",  # â€œ  -> " left double quote
    "â€¢": "•",  # â€¢  -> • bullet
}

total = 0
for path in sorted(glob.glob(os.path.join(WEB, "*.html"))):
    with open(path, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    orig = text
    n = 0
    for bad, good in FIX.items():
        c = text.count(bad)
        if c:
            text = text.replace(bad, good)
            n += c
    if text != orig:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        total += n
        print(f"  fixed {n:4d}  {os.path.basename(path)}")
    # report any remaining mojibake lead bytes not covered above
    for lead in ("â", "Â", "Ã"):
        if lead in text:
            print(f"  !! STILL HAS '{lead}' mojibake: {os.path.basename(path)}")
            break

print(f"Total replacements: {total}")
