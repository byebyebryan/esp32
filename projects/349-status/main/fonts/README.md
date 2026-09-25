# Symbol fallback fonts

`status_symbol_14.c` and `status_symbol_16.c` are static LVGL bitmap fonts.
The UI checks Montserrat, then LVGL's bundled Source Han Sans CJK subset, then
these fonts. They cover Latin-1, Latin Extended-A, punctuation and common signs
(U+00A0–017F, U+2000–206F, U+20A0–20CF, U+2100–214F) from Montserrat
Medium, plus arrows,
math and technical signs, shapes, and dingbats (U+2190–23FF, U+25A0–27BF)
from DejaVu Sans. Only glyphs present in the source fonts are emitted.
Unsupported characters still use LVGL's visible placeholder. Color emoji,
joined emoji sequences, and arbitrary Nerd Font icons are outside this subset.

The source TTFs are bundled with the locked LVGL component at
`managed_components/lvgl__lvgl/scripts/built_in_font/`. Their SHA-256 values
for this generation are:

- Montserrat-Medium.ttf: `421f26b23e2be6b98373d32acd3cb2897b154d4bf0a77d26534ce476e4cbed53`
- DejaVuSans.ttf: `3fdf69cabf06049ea70a00b5919340e2ce1e6d02b0cc3c4b44fb6801bd1e0d22`

Generated with `lv_font_conv` 1.5.3, 4 bpp, no compression or kerning. From
`projects/349-status`, regenerate each size by setting `SIZE` to `14` or `16`:

```sh
rtk npx --yes lv_font_conv@1.5.3 --bpp 4 --size "$SIZE" --no-compress --no-kerning \
  --font managed_components/lvgl__lvgl/scripts/built_in_font/Montserrat-Medium.ttf \
  -r 0x00A0-0x017F,0x2000-0x206F,0x20A0-0x20CF,0x2100-0x214F \
  --font managed_components/lvgl__lvgl/scripts/built_in_font/DejaVuSans.ttf \
  -r 0x2190-0x23FF,0x25A0-0x27BF --format lvgl \
  -o "main/fonts/status_symbol_${SIZE}.c" --lv-font-name "status_symbol_${SIZE}" \
  --lv-include lvgl.h
```

The source font licenses are included in `licenses/`.
After regeneration, run `rtk python tools/check_font_coverage.py` from the
project directory. That checks the generated glyph repertoire for both text
sizes; it does not verify shaping or physical legibility.
