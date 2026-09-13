#!/usr/bin/env python3
# Copyright (c) 2026 Pavel Dubrova <pashadubrova@gmail.com>
# SPDX-License-Identifier: MIT
"""
Generates battery_scale.png sprite sheet (and a matching battery_fail.png)
for healthd/charger (AOSP offline charger).

Format expected by AnimationParser and minui's res_create_multi_display_surface():
  - A single PNG, frames stacked VERTICALLY on top of each other
  - IMPORTANT: the loader does NOT infer the frame count from the file's
    pixel height on its own. It reads the frame count from a PNG text
    chunk with the keyword "Frames" embedded in the file's metadata.
    This script embeds that chunk directly via Pillow when saving.
  - The battery-level ranges for each frame are defined SEPARATELY, via
    "frame: <disp_time> <min_level> <max_level>" lines in animation.txt

All generated PNG images are RGB (3 channels). No alpha channel is used,
because minui's PngHandler does not accept RGBA PNGs.

Usage:
    python3 generate_battery_sprite.py \
        --frames 25 \
        --width 300 \
        --height 520 \
        --corner-radius 25 \
        --outline 18 \
        --inner-padding 12 \
        --charging-bolt \
        --out-path charger/images
"""

import argparse
import numpy
import os

from PIL import Image, ImageFont, ImageDraw
from PIL.PngImagePlugin import PngInfo

COLOR_RED = (235, 70, 70)
COLOR_YELLOW = (240, 190, 60)
COLOR_GREEN = (85, 200, 110)


def lerp(a, b, t):
    """Linearly interpolate between a and b by t."""
    return a + (b - a) * t


def level_color(level_pct):
    """Return a color interpolated red->yellow->green.
    0-50% red->yellow
    50-100% yellow->green.
    """

    if level_pct <= 50:
        t = level_pct / 50
        return tuple(int(lerp(a, b, t)) for a, b in zip(COLOR_RED, COLOR_YELLOW))

    t = (level_pct - 50) / 50
    return tuple(int(lerp(a, b, t)) for a, b in zip(COLOR_YELLOW, COLOR_GREEN))


def draw_charging_bolt_glyph(w, h, top, bottom):
    cx = w // 2
    cy = (top + bottom) // 2
    bw = int(w * 0.24)
    bh = int(h * 0.26)

    return [
        (cx + bw * 0.15, cy - bh / 2),
        (cx - bw * 0.35, cy + bh * 0.08),
        (cx - bw * 0.05, cy + bh * 0.08),
        (cx - bw * 0.15, cy + bh / 2),
        (cx + bw * 0.35, cy - bh * 0.08),
        (cx + bw * 0.05, cy - bh * 0.08),
    ]


def frame_level(index, frames):
    """The single place that maps a frame index -> its displayed
    battery percentage, so every caller (drawing loop, animation.txt
    ranges) agrees on the same numbers."""
    return min(round((index + 1) * (100 / frames)), 100)


def draw_one_battery(
    w,
    h,
    level_pct,
    charging_bolt=False,
    outline_color=(255, 255, 255),
    corner_radius=0,
    outline_width=None,
    inner_padding=None,
    background=(0, 0, 0),
):
    """Draws a single battery frame on a fresh RGB w x h canvas.

    This is the ONLY function in the script that draws a battery body
    from scratch. Every battery image the script produces - each
    sprite-sheet frame and the base of the fail icon - is created by a
    call to this function; nothing else builds a canvas.

    corner_radius: corner rounding radius in px. 0 (default) = sharp,
                   perfectly rectangular edges.
    outline_width: wall thickness in px. None = auto, defaults to a
                   THICK wall (7% of width) rather than a thin line.
    inner_padding: gap in px between the inner wall edge and the fill
                   rectangle. None = auto (2x outline_width, so the
                   gap scales with wall thickness).
    """

    img = Image.new("RGB", (w, h), background)
    draw = ImageDraw.Draw(img)

    nub_h = int(h * 0.045)
    nub_w = int(w * 0.34)

    body_top = nub_h
    body_left = 0
    body_right = w - 1
    body_bottom = h - 1

    line_w = max(3, int(w * 0.07)) if outline_width is None else outline_width
    pad = line_w * 2 if inner_padding is None else inner_padding

    draw.rounded_rectangle(
        (body_left, body_top, body_right, body_bottom),
        radius=corner_radius,
        outline=outline_color,
        width=line_w,
    )

    inner_left = body_left + line_w + pad
    inner_right = body_right - line_w - pad
    inner_top = body_top + line_w + pad
    inner_bottom = body_bottom - line_w - pad

    if inner_right <= inner_left or inner_bottom <= inner_top:
        return img

    if level_pct > 0:
        inner_h = inner_bottom - inner_top
        fill_h = int(inner_h * (level_pct / 100.0))

        if fill_h > 2:
            fill_top = inner_bottom - fill_h
            color = level_color(level_pct)

            inner_r = max(0, corner_radius - line_w)

            draw.rounded_rectangle(
                (inner_left, inner_top, inner_right, inner_bottom),
                radius=inner_r,
                fill=color,
            )

            if fill_top > inner_top:
                draw.rectangle(
                    (inner_left, inner_top, inner_right, fill_top - 1),
                    fill=background)

    nub_x0 = (w - nub_w) // 2
    nub_radius = min(int(corner_radius * 0.6), nub_h // 2) if corner_radius > 0 else 0

    draw.rounded_rectangle(
        (nub_x0, body_left, nub_x0 + nub_w, body_top + corner_radius),
        radius=nub_radius,
        fill=outline_color,
    )

    if charging_bolt:
        draw.polygon(
            draw_charging_bolt_glyph(w, h, body_top, body_bottom),
            fill=(255, 255, 255)
        )

    return img


def draw_fail_glyph(img, w, h):
    """Overlay a red X on top of an already-drawn battery image to turn
    it into the failure/error icon. Takes an existing image instead of
    drawing a battery itself, so the body always comes from the single
    draw_one_battery() call site in main() - this function only adds
    the mark on top.
    """
    draw = ImageDraw.Draw(img)

    margin = int(w * 0.10)
    nub_h = int(h * 0.045)
    body_top = margin + nub_h
    body_bottom = h - margin

    cx = w // 2
    cy = (body_top + body_bottom) // 2
    r = int(min(w, h) * 0.15)
    line_w = max(4, int(w * 0.05))
    red = (235, 70, 70)

    draw.line((cx - r, cy - r, cx + r, cy + r), fill=red, width=line_w)
    draw.line((cx - r, cy + r, cx + r, cy - r), fill=red, width=line_w)

    return img


def append_metadata(img, frame_count):
    """Builds the metadata that minui's res_create_multi_display_surface()
    actually reads to know how many frames the file contains.

    Without this metadata the loader has no way to know how many equal
    horizontal slices to cut the image into - this is exactly what the
    classic community "battery_scale" build scripts achieve by piping
    the PNG through `pngcrush -text b "Frames" <N>` as a last step.
    Embedding it directly via Pillow's PngInfo skips the extra
    imagemagick/pngcrush dependency entirely.

    The image must already be RGB. No RGBA conversion is performed here.
    """
    if img.mode != "RGB":
        raise ValueError(
            f"append_metadata() expects RGB, got {img.mode}"
        )

    info = PngInfo()
    info.add_text("Frames", str(frame_count))

    return info


def compute_frame_ranges(frames, fill_effect):
    """Computes the (min_level, max_level) pairs to print for animation.txt.

    fill_effect=True: min_level is always 0, max_level is each frame's
    cumulative threshold. A frame stays a valid match for every battery
    level at or below its own threshold, so at any given charge level
    several consecutive frames (from the emptiest up to the one at or
    just above the current level) are simultaneously valid - useful for
    an overlapping/animated selection scheme.

    fill_effect=False: disjoint ranges, exactly one frame matches any
    given level - a plain step-change with no overlap.

    IMPORTANT caveat either way: AOSP's real frame-advance logic
    (healthd_mode_charger.cpp) does NOT re-check min/max on every step -
    it only searches for the first matching frame when a cycle restarts
    (cur_frame == 0), then just increments cur_frame unconditionally
    until the end of the array. So the practical animation you get is
    "play from the matched frame through to the LAST frame", regardless
    of scheme - the closer the current level is to the top of the range,
    the fewer frames remain to play, and near 100% it can look almost
    static purely because there's little array left after the match.
    More frames (finer granularity, especially near the top) is the way
    to keep visible motion at high charge levels.
    """
    step = 100 / frames
    ranges = []

    for i in range(frames):
        threshold = min(round((i + 1) * step), 100)

        if fill_effect:
            ranges.append((0, threshold))
            continue

        min_lvl = round(i * step)
        max_lvl = threshold - 1 if i != frames - 1 else threshold
        ranges.append((min_lvl, max_lvl))

    return ranges


def pack_sprite_sheet(frame_images, width, height):
    """Packs already-drawn frame images into a single PNG using the
    exact row-interleaved layout that AOSP's real loader expects.

    This function does NOT draw anything - it only rearranges pixels
    that draw_one_battery() already produced. Keeping drawing and
    packing separate means every image in the file is traceable back
    to a single draw_one_battery() call in main().

    This is NOT "frame 1 fully on top, frame 2 fully below it". The
    actual reader (bootable/recovery/minui/resources.cpp,
    res_create_multi_display_surface) does this while reading the file
    row by row:

        for (y = 0; y < height; ++y) {
            int frame = y % *frames;
            out_row = surface[frame]->data + (y / *frames) * surface[frame]->row_bytes;
            ...
        }

    i.e. output row y belongs to frame (y % frames), at row (y // frames)
    *within* that frame. So the file's rows cycle through all frames in
    round-robin order (row0->frame0, row1->frame1, ..., row(frames-1)
    ->frame(frames-1), row(frames)->frame0 again, ...), NOT one
    contiguous block per frame. This matches exactly what community
    build scripts do with ImageMagick's
    `-fx "u[j%FRAMES+1].p{i,int(j/FRAMES)}"` before pngcrush.

    Still not a GIF and nothing "plays" inside the file - it's one
    static image, just with this specific row order baked in so the
    real loader can de-interleave it back into the original frames.
    """
    frames = len(frame_images)
    frame_arrays = []

    for frame in frame_images:
        if frame.mode != "RGB":
            raise ValueError(f"Battery frame must be RGB, got {frame.mode}")
        frame_arrays.append(numpy.array(frame))

    out = numpy.zeros((height * frames, width, 3), dtype=numpy.uint8)

    for frame_index, frame_array in enumerate(frame_arrays):
        # rows f, f+frames, f+2*frames, ... <- this frame's rows 0,1,2,...
        out[frame_index::frames, :, :] = frame_array

    return Image.fromarray(out, "RGB")


def save_preview(
    frame_images,
    path,
    per_row=10,
    gap=20,
    background=(0, 0, 0),
    label_height=45,
):
    """Save a visual grid preview of all frames."""
    if not frame_images:
        return

    frame_w, frame_h = frame_images[0].size
    count = len(frame_images)

    rows = (count + per_row - 1) // per_row
    columns = min(per_row, count)

    preview_w = columns * frame_w + (columns + 1) * gap
    preview_h = rows * (frame_h + label_height) + (rows + 1) * (gap * 2)

    preview = Image.new("RGB", (preview_w, preview_h), background)
    draw = ImageDraw.Draw(preview)

    font = ImageFont.load_default(size=36)

    for i, frame in enumerate(frame_images):
        row, col = divmod(i, per_row)

        x = gap + col * (frame_w + gap)
        y = gap + row * (frame_h + label_height + gap)

        preview.paste(frame.convert("RGB"), (x, y))

        label = f"Frame: {i + 1}"

        bbox = draw.textbbox((0, 0), label, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        text_x = x + (frame_w - text_w) / 2
        text_y = gap / 2 + y + frame_h + (label_height - text_h) / 2 - bbox[1]

        draw.text(
            (text_x, text_y),
            label,
            font=font,
            fill=(255, 255, 255),
        )

    preview.save(path)


def parse_args():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    ap.add_argument("--width",
                    type=int, default=300,
                    help="Width of a single frame, px")
    ap.add_argument("--height",
                    type=int, default=520,
                    help="Height of a single frame, px")
    ap.add_argument("--frames",
                    type=int, default=6,
                    help="Number of battery-level frames to generate")
    ap.add_argument("--charging-bolt",
                    action="store_true",
                    help="Draw a lightning bolt over the fill")
    ap.add_argument("--corner-radius",
                    type=int, default=0,
                    help="Body corner radius, px")
    ap.add_argument("--outline-width",
                    type=int, default=None,
                    help="Body wall thickness, px")
    ap.add_argument("--inner-padding",
                    type=int, default=None,
                    help="Gap between wall and fill, px")
    ap.add_argument("--fill-effect",
                    action="store_true",
                    help="Use overlapping animation.txt ranges")
    ap.add_argument("--out-path",
                    default=".",
                    help="Directory where battery_scale.png, battery_fail.png and "
                         "battery_scale_preview.png are all written (created if missing). "
                         "The single output location for everything this script produces.")

    args = ap.parse_args()

    if args.width <= 0:
        ap.error("--width must be > 0")
    if args.height <= 0:
        ap.error("--height must be > 0")
    if args.frames < 2:
        ap.error("--frames must be >= 2")
    if args.corner_radius < 0:
        ap.error("--corner-radius must be >= 0")
    if args.outline_width is not None and args.outline_width <= 0:
        ap.error("--outline-width must be > 0")
    if args.inner_padding is not None and args.inner_padding < 0:
        ap.error("--inner-padding must be >= 0")

    return args


def main():
    """Generates battery_scale.png, battery_fail.png, and a preview
    contact sheet, all under fixed filenames inside `out_path`.

    All image *drawing* happens right here: each sprite-sheet frame and
    the fail-icon base are each produced by one draw_one_battery() call
    in the loops/lines below, so there's a single, uniform place to
    look if a battery ever needs to look different. Everything after
    that (marking, packing, saving, preview) just processes images
    that were already drawn here - it never draws a battery itself.
    """
    args = parse_args()

    os.makedirs(args.out_path, exist_ok=True)

    out_scale = os.path.join(args.out_path, "battery_scale.png")
    out_fail = os.path.join(args.out_path, "battery_fail.png")
    out_preview = os.path.join(args.out_path, "battery_scale_preview.png")

    draw_kwargs = dict(
        corner_radius=args.corner_radius,
        outline_width=args.outline_width,
        inner_padding=args.inner_padding,
    )

    # Draw every sprite-sheet frame.
    frame_images = [
        draw_one_battery(
            args.width,
            args.height,
            frame_level(i, args.frames),
            charging_bolt=args.charging_bolt,
            **draw_kwargs,
        )
        for i in range(args.frames)
    ]

    # Draw the fail icon (same primitive, then mark it).
    fail_img = draw_fail_glyph(
        draw_one_battery(args.width, args.height, level_pct=0, **draw_kwargs),
        args.width,
        args.height,
    )

    # Pack + save everything drawn above.
    sheet = pack_sprite_sheet(frame_images, args.width, args.height)
    sheet.save(out_scale, pnginfo=append_metadata(sheet, args.frames))
    print(
        f"  {out_scale} "
        f"({sheet.width}x{sheet.height}, "
        f"{args.frames} frames, "
        f"mode={sheet.mode}), "
        f"Metadata embedded: 'Frames'={args.frames}"
    )

    fail_img.save(out_fail)
    print(
        f"  {out_fail} "
        f"({fail_img.width}x{fail_img.height}, "
        f"mode={fail_img.mode})"
    )

    save_preview(frame_images, out_preview)
    print(f"  {out_preview} ({len(frame_images)} frames)")

    ranges = compute_frame_ranges(args.frames, args.fill_effect)
    print()

    if args.fill_effect:
        print("Add this to animation.txt (fill effect):")
    else:
        print("Add this to animation.txt (static):")

    for min_lvl, max_lvl in ranges:
        print(f"frame: 750 {min_lvl} {max_lvl}")


if __name__ == "__main__":
    main()
