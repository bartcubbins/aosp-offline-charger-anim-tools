# AOSP Offline Charger Animation Tools

Small set of standalone Python scripts for building and previewing
`battery_scale.png`/`font_map.png` assets for AOSP's offline
charger (`minui`/`healthd_mode_charger`), without needing a full
AOSP tree or `pngcrush`/ImageMagick.

| Script                     | Purpose                                                        |
|----------------------------|-----------------------------------------------------------------|
| `generate_battery_sprite.py` | Draws `battery_scale.png` (+ `battery_fail.png` + a preview sheet) and prints the matching `animation.txt` frame ranges |
| `generate_font.py`          | Draws `font_map.png`, the 96x2 glyph grid used by `percent_display` |
| `view_animation.py`         | Plays back a sprite sheet + `animation.txt` exactly as the real charger would, for a given battery level - and can export the result as a GIF or MP4 |

All three are pure Python + [Pillow](https://pillow.readthedocs.io/);
no AOSP source tree, `pngcrush`, or ImageMagick required.

## Motivation

The charger's asset format has a few non-obvious constraints baked
into `bootable/recovery/minui`:

- `battery_scale.png` must embed a `"Frames"` PNG text chunk - the
  loader does **not** infer frame count from pixel height.
- Frames are stored **row-interleaved**, not stacked as contiguous
  blocks (`res_create_multi_display_surface()` reads row `y` as
  belonging to frame `y % frames`).
- `battery_scale.png` must be **RGB**, no alpha.
- `font_map.png` must be **8-bit grayscale, single channel** - the
  loader explicitly rejects anything else. Text color is set
  separately, in `animation.txt`.

Getting any of these wrong produces a garbled or refused asset with a
cryptic (or no) error on-device. These scripts bake the constraints in
so you don't have to remember them, and `view_animation.py` lets you
sanity-check the result on your desktop before flashing anything.

## Requirements

```
pip install Pillow numpy
```

`view_animation.py` also needs Tkinter (`python3-tk`) - but only for
the interactive window. `--no-window` (export-only) mode works without
Tkinter installed at all. `--save-video` additionally needs an
`ffmpeg` binary.

## Typical workflow

```bash
# 1. Generate the battery sprite sheet + battery_fail.png + preview
python3 generate_battery_sprite.py \
    --frames 25 --width 300 --height 520 --charging-bolt \
    --out-path example

# 2. Generate the percent-display font
python3 generate_font.py \
    --ttf /home/pavel/dev/android-17/external/roboto-mono/fonts/ttf/RobotoMono-Regular.ttf \
    --size 52 --only-digits-percent \
    --out example/font_map.png

# 3. Preview the animation before pushing it to device
python3 view_animation.py \
    example/battery_scale.png \
    example/animation.txt \
    10   # battery level to preview, 0-100

# ...or skip the window and export it straight to a GIF/MP4
python3 view_animation.py \
    example/battery_scale.png \
    example/animation.txt \
    10 --save-gif preview.gif --no-window
```

Step 1 prints the `frame:` lines to add to `animation.txt` - paste
them in as-is.

## `generate_battery_sprite.py`

Draws each battery frame from scratch (rounded/sharp outline, colored
fill interpolated red -> yellow -> green, optional charging bolt),
packs them into the row-interleaved sprite layout, and writes:

- `battery_scale.png` - the animation sprite sheet, with the `Frames`
  metadata chunk embedded
- `battery_fail.png` - the same battery body with a red X overlay,
  for the charger's failure icon
- `battery_scale_preview.png` - a labeled contact sheet of every
  frame, for a quick visual sanity check

```
--width N              Frame width, px (default: 300)
--height N             Frame height, px (default: 520)
--frames N             Number of battery-level frames (default: 6, min 2)
--charging-bolt        Draw a lightning bolt over the fill
--corner-radius N      Body corner radius, px (default: 0, sharp)
--outline-width N      Wall thickness, px (default: auto, ~7% of width)
--inner-padding N      Gap between wall and fill (default: auto, 2x outline)
--fill-effect          Emit overlapping animation.txt ranges instead of
                       disjoint step ranges
--out-path DIR         Output directory (default: current directory)
```

> Frame count and range scheme affect on-device playback more than it
> looks: the real charger only re-picks a starting frame when a cycle
> restarts, then just walks forward to the last frame. Near 100%
> charge, few frames remain after the match, so the animation can look
> almost static unless you use more frames near the top of the range.

## `generate_font.py`

Renders the digits `0-9`, `%` and `:` (plus, optionally, the full
printable ASCII range) into the fixed 96-column x 2-row grid
`gr_init_font()` expects, as an 8-bit grayscale PNG.

```
--ttf PATH             TTF/OTF font to render glyphs from
--size N               Font size in points (default: 28)
--cell-w N             Fixed cell width, px - must be given with --cell-h
--cell-h N             Fixed cell height, px - must be given with --cell-w
                       (default: both auto-measured from the glyphs
                       actually used)
--only-digits-percent  Skip every glyph outside '%0123456789:'
                       (smaller file, faster to generate)
--out PATH             Output PNG path (default: font_map.png)
```

## `view_animation.py`

```
python3 view_animation.py <battery_scale.png> <animation.txt> [level]
```

`level` (0-100, default 50) selects which `animation.txt` range to
start playback from - the same lookup the real charger does.

Whether it actually animates or holds still from there is decided
purely by the ranges in `animation.txt`, not by a switch: before
stepping to the next frame, the viewer checks whether *that* frame's
own range still covers `level`.

- **Overlapping ranges** (as `--fill-effect` produces, always
  `0 <threshold>`) keep matching as the threshold rises, so playback
  walks all the way to the last frame and loops - a "filling up"
  animation.
- **Disjoint/step ranges** stop matching as soon as the next frame's
  range moves past `level`, so playback holds on the single matched
  frame - a static preview.

Either way, Left/Right still step through frames manually and Space
still pauses/resumes, regardless of what's playing on its own.

```
--save-gif PATH        Export the matched playback sequence as an
                       animated GIF
--save-video PATH      Export the matched playback sequence as an MP4
                       (requires ffmpeg on PATH)
--no-window            Don't open the interactive viewer - only useful
                       together with --save-gif/--save-video
```

`--save-gif`/`--save-video` export exactly what the interactive window
would show for `level`: a single static frame for disjoint ranges, or
the full looping sequence for overlapping ones.

Controls:

| Key         | Action                |
|-------------|-----------------------|
| Space       | Pause / resume        |
| Left/Right  | Step one frame back/forward |
| R           | Restart from the matched frame |
| Esc         | Quit                  |

The viewer refuses to run if the sprite's embedded `Frames` count
doesn't match the number of `frame:` lines in `animation.txt` - that
mismatch is exactly the kind of thing that's silent and broken
on-device but loud and obvious here.

## License

MIT - see the header in each script.
