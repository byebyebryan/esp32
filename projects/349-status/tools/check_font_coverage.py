#!/usr/bin/env python3
"""Check the glyph repertoire compiled into both notification font sizes."""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
LVGL_FONTS = PROJECT / "managed_components/lvgl__lvgl/src/font"
FONT_GLYPH = re.compile(r"/\* U\+([0-9A-F]{4,6})")


def glyphs(path: Path) -> set[int]:
    return {int(code, 16) for code in FONT_GLYPH.findall(path.read_text())}


def repertoire(size: int) -> set[int]:
    return (
        glyphs(LVGL_FONTS / f"lv_font_montserrat_{size}.c")
        | glyphs(LVGL_FONTS / f"lv_font_source_han_sans_sc_{size}_cjk.c")
        | glyphs(PROJECT / f"main/fonts/status_symbol_{size}.c")
    )


REQUIRED = {
    "ASCII": set(range(0x20, 0x7F)),
    "Latin-1 and Latin Extended-A": set(range(0xA0, 0x180)),
    "common punctuation and symbols": {
        ord(char)
        for char in "‘’“”–—…·•‰′″°±×÷−≈≠≤≥∞€£¥₹₽©®™§¶†‡←↑→↓↔✓✔✗✘✕★☆♥❤⚠⚙☑☒☐"
    },
    "CJK smoke sample": {ord(char) for char in "東京が日本語你好世界"},
}


def main() -> int:
    failed = False
    for size in (14, 16):
        available = repertoire(size)
        print(f"{size} px: {len(available)} distinct glyphs")
        for label, required in REQUIRED.items():
            missing = sorted(required - available)
            if missing:
                failed = True
                details = ", ".join(f"U+{code:04X} {unicodedata.name(chr(code), '?')}" for code in missing)
                print(f"  {label}: missing {details}")
            else:
                print(f"  {label}: covered")
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
