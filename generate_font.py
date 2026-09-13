#!/usr/bin/env python3
# Copyright (c) 2026 Pavel Dubrova <pashadubrova@gmail.com>
# SPDX-License-Identifier: MIT
"""
Generates a bitmap font PNG for the minui/healthd charger (AOSP offline
charger) percent_display renderer.

Format expected by gr_init_font() (bootable/recovery/minui/graphics.cpp):
  - A single PNG laid out as a 96-column x 2-row grid
  - Columns correspond to ASCII 0x20 (space) .. 0x7F, in order
  - Row 0 is the regular glyph, row 1 is the "bold" glyph. percent_display
    never selects the bold row, but both rows must still be present, or
    the loader computes char_height (image_height / 2) incorrectly.
  - All 96 cells must be exactly the same size.
  - IMPORTANT: the file must be 8-bit GRAYSCALE, 1 channel, no alpha. It
    is loaded by res_create_alpha_surface() (bootable/recovery/minui/
    resources.cpp), which explicitly rejects any PNG where
    channels != 1 - so RGB/RGBA input is refused outright. The actual
    on-screen text color is not set here at all; it comes from
    animation.txt ("percent_display: X Y R G B A ..."). The font file
    only carries per-pixel glyph coverage as a grayscale value, which is
    why this script intentionally has no --color option.

Only two ranges of glyphs are ever actually used to draw the battery
percentage:
    '0'-'9'  -> 0x30-0x39
    '%'      -> 0x25
Every other cell is left blank (value 0 = black, i.e. no coverage).
Blank cells cost almost nothing in the saved PNG and don't affect
rendering, but they must still exist for the grid geometry to come out
right.

Usage:
    python3 generate_font.py --ttf /path/to/font.ttf --size 26 \
        --out font_map.png

If --ttf is not given, the script falls back to a short list of default
system font paths.
"""

import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

DEFAULT_FONT_CANDIDATES = [
    "/home/pavel/dev/android-16/external/roboto-mono/fonts/ttf/RobotoMono-Regular.ttf",
    "/home/pavel/dev/android-17/external/roboto-mono/fonts/ttf/RobotoMono-Regular.ttf",
]

ASCII_FIRST = 0x20
ASCII_LAST = 0x7F  # inclusive
NUM_COLS = ASCII_LAST - ASCII_FIRST + 1  # 96

NEEDED_CHARS = set("%0123456789")


def find_default_font():
    """Returns the first existing path from DEFAULT_FONT_CANDIDATES, or
    None if none of them exist on this machine."""
    for path in DEFAULT_FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def find_cell_size(font, chars):
    """Measures every glyph in `chars` and returns the smallest cell
    (width, height) that fits the largest glyph in each dimension.

    Only the glyphs percent_display actually uses (digits and '%') are
    measured - every other cell in the 96-column grid is left blank
    regardless, so sizing the cell around them would be wasted space.
    """
    cell_w = 0
    cell_h = 0
    max_char_w = None
    max_char_h = None

    for ch in chars:
        bbox = font.getbbox(ch)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]

        if w > cell_w:
            cell_w = w
            max_char_w = ch

        if h > cell_h:
            cell_h = h
            max_char_h = ch

    print(f"  max glyph width : {cell_w}px ({max_char_w!r})")
    print(f"  max glyph height: {cell_h}px ({max_char_h!r})")

    return cell_w, cell_h


def draw_font_sheet(font, cell_w, cell_h, only_digits_percent):
    """Draws the full 96x2 glyph grid on a single grayscale canvas.

    This is the only function in the script that draws a glyph - main()
    just measures the font first, calls this once, and saves the result
    after. Kept in "L" mode (8-bit grayscale, no alpha) throughout,
    since minui's res_create_alpha_surface() rejects anything else (see
    module docstring).
    """
    img = Image.new("L", (cell_w * NUM_COLS, cell_h * 2), 0)
    draw = ImageDraw.Draw(img)
    glyph_value = 255  # full coverage; actual color comes from animation.txt

    for code in range(ASCII_FIRST, ASCII_LAST + 1):
        ch = chr(code)
        col = code - ASCII_FIRST

        if only_digits_percent and ch not in NEEDED_CHARS:
            continue

        x0 = col * cell_w
        y0 = 0

        bbox = draw.textbbox((0, 0), ch, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]

        tx = x0 + (cell_w - w) // 2 - bbox[0]
        ty = y0 + (cell_h - h) // 2 - bbox[1]

        draw.text((tx, ty), ch, font=font, fill=glyph_value)

    return img


def parse_args():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    ap.add_argument("--ttf",
                    help="Path to a TTF/OTF font file")
    ap.add_argument("--size",
                    type=int, default=28,
                    help="Font size in points, used to render the glyphs")
    ap.add_argument("--cell-w",
                    type=int, default=None,
                    help="Fixed cell width, px (default: auto-measured from "
                         "the '%%0123456789:' glyphs)")
    ap.add_argument("--cell-h",
                    type=int, default=None,
                    help="Fixed cell height, px (default: auto-measured from "
                         "the '%%0123456789:' glyphs)")
    ap.add_argument("--only-digits-percent",
                    action="store_true",
                    help="Don't render any glyph outside '%%0123456789:' "
                         "(faster, lighter output file)")
    ap.add_argument("--out",
                    default="font_map.png",
                    help="Output PNG path")

    args = ap.parse_args()

    if args.size <= 0:
        ap.error("--size must be > 0")
    if (args.cell_w is None) != (args.cell_h is None):
        ap.error("--cell-w and --cell-h must be given together")
    if args.cell_w is not None and args.cell_w <= 0:
        ap.error("--cell-w must be > 0")
    if args.cell_h is not None and args.cell_h <= 0:
        ap.error("--cell-h must be > 0")

    args.ttf = args.ttf or find_default_font()

    if not args.ttf or not os.path.exists(args.ttf):
        ap.error("no TTF font found - pass --ttf /path/to/font.ttf")

    return args


def main():
    """Builds the digits+percent bitmap font, in the fixed grid geometry
    gr_init_font() expects, and saves it under a single filename.

    All glyph *drawing* happens in draw_font_sheet() above - this
    function only measures the font first (to size the cells, unless a
    fixed size was given) and saves the canvas draw_font_sheet()
    produced; it never draws a glyph itself.
    """
    args = parse_args()

    font = ImageFont.truetype(args.ttf, args.size)

    # Cell geometry
    if args.cell_w is not None:
        cell_w, cell_h = args.cell_w, args.cell_h
        print(f"Using fixed cell size: {cell_w}x{cell_h}")
    else:
        cell_w, cell_h = find_cell_size(font, NEEDED_CHARS)

    # Draw + save
    sheet = draw_font_sheet(font, cell_w, cell_h, args.only_digits_percent)
    sheet.save(args.out)

    print(
        f"  {args.out} "
        f"({sheet.width}x{sheet.height}, cell {cell_w}x{cell_h}, "
        f"mode={sheet.mode})"
    )
    print(f"  char_width  will be = {sheet.width}  // 96 = {sheet.width // 96}")
    print(f"  char_height will be = {sheet.height} // 2  = {sheet.height // 2}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
