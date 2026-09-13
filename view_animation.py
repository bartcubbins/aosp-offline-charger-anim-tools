#!/usr/bin/env python3
# Copyright (c) 2026 Pavel Dubrova <pashadubrova@gmail.com>
# SPDX-License-Identifier: MIT
"""
Interactive viewer for AOSP minui/healthd charger battery animations.

Loads a row-interleaved battery_scale.png sprite sheet (as produced by
generate_battery_sprite.py) together with its animation.txt, and plays
the animation back the same way healthd_mode_charger.cpp would: it picks
the starting frame for a given battery level from the "frame: <disp_time>
<min_level> <max_level>" ranges in animation.txt, then steps through the
sprite sheet frame by frame at each frame's own duration.

Controls:
  Space       Pause/resume
  Left/Right  Previous/next frame
  R           Restart animation
  Esc         Exit

Usage:
    python3 view_animation.py battery_scale.png animation.txt [level]
    python3 view_animation.py battery_scale.png animation.txt 70 \
        --save-gif preview.gif --save-video preview.mp4 --no-window
"""

import argparse
import numpy
import os
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import Image


class AnimationFrame:
    """One "frame:" line from animation.txt: how long the frame is
    shown, and the inclusive battery-level range it applies to.
    """

    def __init__(self, duration_ms, min_level, max_level):
        self.duration_ms = max(1, duration_ms)
        self.min_level = min_level
        self.max_level = max_level

    def matches(self, level):
        return self.min_level <= level <= self.max_level


def parse_animation(path):
    """Parses the "frame:" lines out of an animation.txt.

    Every other AOSP directive (animation:, fail:, clock_display:,
    percent_display:, ...) is silently skipped - this viewer only cares
    about the frame timeline, not where healthd would draw overlays.
    """
    frames = []

    try:
        with open(path, "r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, 1):
                line = line.strip()

                if not line or line.startswith("#"):
                    continue

                parts = line.split()

                if parts[0] != "frame:":
                    continue

                if len(parts) != 4:
                    print(f"Invalid frame line {line_number}: {line}")
                    return None

                try:
                    duration = int(parts[1])
                    min_level = int(parts[2])
                    max_level = int(parts[3])
                except ValueError:
                    print(f"Invalid frame line {line_number}: {line}")
                    return None

                if duration <= 0:
                    print(
                        f"Invalid duration on line {line_number}: {duration}"
                    )
                    return None

                if not 0 <= min_level <= 100:
                    print(
                        f"Invalid minimum battery level on line "
                        f"{line_number}: {min_level}"
                    )
                    return None

                if not 0 <= max_level <= 100:
                    print(
                        f"Invalid maximum battery level on line "
                        f"{line_number}: {max_level}"
                    )
                    return None

                if min_level > max_level:
                    print(
                        f"Invalid battery range on line "
                        f"{line_number}: {min_level} > {max_level}"
                    )
                    return None

                frames.append(AnimationFrame(duration, min_level, max_level))

    except OSError as e:
        print(f"Failed to open animation file: {path}")
        print(e)
        return None

    if not frames:
        print(f"No animation frames found in {path}")
        return None

    return frames


def get_png_metadata(image):
    """Reads the AOSP minui PNG metadata that res_create_multi_display_
    surface() actually keys off: the "Frames" text chunk written by
    generate_battery_sprite.py's append_metadata().
    """

    frames_text = image.info.get("Frames")

    if frames_text is None:
        print("PNG does not contain a 'Frames' text chunk.")
        return None, None

    try:
        frame_count = int(frames_text)
    except (TypeError, ValueError):
        print(f"Invalid PNG Frames value: {frames_text!r}")
        return None, None

    if frame_count <= 0:
        print(f"Invalid PNG frame count: {frame_count}")
        return None, None

    return frame_count


def extract_frames(image, frame_count):
    """Decodes the exact AOSP/minui row-interleaved sprite layout that
    generate_battery_sprite.py's pack_sprite_sheet() produces:

        frame 0 row 0
        frame 1 row 0
        frame 2 row 0
        ...
        frame N row 0
        frame 0 row 1
        frame 1 row 1
        ...

    i.e. output row y belongs to frame (y % frame_count), at logical row
    (y // frame_count) within that frame - the inverse of the packing
    done by resources.cpp's res_create_multi_display_surface() reader.
    """

    width, total_height = image.size

    if frame_count <= 0:
        print("Invalid frame count.")
        return None

    if total_height % frame_count != 0:
        print(
            f"Sprite sheet height {total_height} is not divisible "
            f"by Frames={frame_count}"
        )
        return None

    frame_height = total_height // frame_count

    if frame_height <= 0:
        print("Calculated frame height is invalid.")
        return None

    # This is the exact inverse of pack_sprite_sheet() in
    # generate_battery_sprite.py, which builds row y of the sheet from
    # frame (y % frame_count)'s row (y // frame_count) via
    # out[frame_index::frames, :, :] = frame_array. Slicing every
    # frame_count-th row back out the same way is a single numpy view
    # per frame - no per-pixel Python loop, which is what made this
    # slow for anything but tiny sprites.
    array = numpy.array(image)

    return [
        Image.fromarray(array[frame_index::frame_count, :, :], image.mode)
        for frame_index in range(frame_count)
    ]


def find_frame_for_level(frames, battery_level):
    """Finds the AOSP animation frame associated with the battery level.

    For overlapping ranges (--fill-effect in generate_battery_sprite.py),
    the first matching frame wins, which is also the useful behavior for
    this viewer.
    """

    for index, frame in enumerate(frames):
        if frame.matches(battery_level):
            return index

    # No exact match - fall back to the closest range.
    if battery_level < frames[0].min_level:
        return 0

    if battery_level > frames[-1].max_level:
        return len(frames) - 1

    best_index = 0
    best_distance = float("inf")

    for index, frame in enumerate(frames):
        if battery_level < frame.min_level:
            distance = frame.min_level - battery_level
        elif battery_level > frame.max_level:
            distance = battery_level - frame.max_level
        else:
            distance = 0

        if distance < best_distance:
            best_distance = distance
            best_index = index

    return best_index


def build_playback_sequence(images, animation, battery_level):
    """Builds the finite list of (image, duration_ms) frames that one
    playback cycle would show for battery_level - the same walk
    BatteryViewer.update() does interactively, made explicit here so
    --save-gif/--save-video can export exactly what the window would
    display, without a display.

    Starts at the frame find_frame_for_level() matches, then keeps
    stepping forward exactly as long as BatteryViewer would: while the
    next frame's own range still covers battery_level. Stops (without
    wrapping) either when the sprite sheet runs out - one full "filling
    up" cycle - or as soon as a disjoint/step range breaks the match,
    which is also the frame the interactive viewer freezes on.
    """
    frame_count = len(images)
    start = find_frame_for_level(animation, battery_level)

    sequence = [(images[start], animation[start].duration_ms)]

    index = start
    while True:
        next_index = index + 1

        if next_index >= frame_count:
            break

        if not animation[next_index].matches(battery_level):
            break

        sequence.append((images[next_index], animation[next_index].duration_ms))
        index = next_index

    return sequence


def save_gif(sequence, path):
    """Saves the playback sequence as an animated GIF.

    GIF frames are independently palette-quantized by default, which
    makes a battery fill (a smooth red->yellow->green gradient across
    frames) visibly flicker between colors. Building one shared palette
    from every frame first and re-using it for each frame keeps the
    color stable across the whole animation.
    """
    frames_rgb = [image.convert("RGB") for image, _ in sequence]

    combined = Image.new(
        "RGB", (frames_rgb[0].width * len(frames_rgb), frames_rgb[0].height)
    )

    for i, frame in enumerate(frames_rgb):
        combined.paste(frame, (i * frame.width, 0))

    shared_palette = combined.quantize(colors=256, method=Image.MEDIANCUT)
    quantized = [frame.quantize(palette=shared_palette) for frame in frames_rgb]

    # GIF viewers commonly misrender per-frame delays below ~20ms as
    # 100ms - clamp so a very short animation.txt duration doesn't
    # surprise anyone.
    durations = [max(20, duration_ms) for _, duration_ms in sequence]

    quantized[0].save(
        path,
        save_all=True,
        append_images=quantized[1:],
        duration=durations,
        loop=0,
    )

    return True


def save_video(sequence, path):
    """Saves the playback sequence as an MP4 via ffmpeg's concat
    demuxer, so each frame keeps its own animation.txt duration instead
    of being resampled to a fixed frame rate.

    Shells out to a system `ffmpeg` binary rather than pulling in a
    Python video-encoding dependency for what is otherwise an optional,
    occasional-use feature.
    """
    ffmpeg = shutil.which("ffmpeg")

    if ffmpeg is None:
        print("ffmpeg not found on PATH - required for --save-video.")
        return False

    with tempfile.TemporaryDirectory() as tmp_dir:
        concat_path = os.path.join(tmp_dir, "concat.txt")
        lines = []

        for i, (image, duration_ms) in enumerate(sequence):
            frame_path = os.path.join(tmp_dir, f"frame_{i:04d}.png")
            image.convert("RGB").save(frame_path)
            lines.append(f"file '{frame_path}'")
            lines.append(f"duration {duration_ms / 1000.0:.3f}")

        # The concat demuxer ignores the very last "duration" line, so
        # the common workaround is to repeat the final frame once more
        # without one.
        last_frame_path = os.path.join(tmp_dir, f"frame_{len(sequence) - 1:04d}.png")
        lines.append(f"file '{last_frame_path}'")

        with open(concat_path, "w", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")

        result = subprocess.run(
            [
                ffmpeg, "-y",
                "-f", "concat", "-safe", "0", "-i", concat_path,
                "-vf", "format=yuv420p",
                "-c:v", "libx264",
                path,
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            print("ffmpeg failed:")
            print(result.stderr[-2000:])
            return False

    return True


class BatteryViewer:
    """Tk window that plays back the decoded frames, starting from the
    frame matching the requested battery level and looping from there.

    Whether it actually animates or just holds still is decided purely
    by the ranges in animation.txt, not by any command-line switch:
    before stepping to the next frame, update() checks whether that
    next frame's own range still covers battery_level.

      - Overlapping ranges (as generate_battery_sprite.py's
        --fill-effect produces, always "0 <threshold>") keep matching
        as the threshold rises, so playback walks all the way to the
        last frame and loops - a "filling up" animation.
      - Disjoint/step ranges stop matching as soon as the next frame's
        own range moves past battery_level, so playback holds on the
        single matched frame - a static preview.

    Either way, Left/Right still step through frames manually and
    Space still pauses/resumes, regardless of what's playing on its
    own.
    """

    def __init__(self, root, images, animation, battery_level):
        self.root = root
        self.images = images
        self.animation = animation
        self.battery_level = battery_level

        self.frame_count = len(images)

        self.start_frame = find_frame_for_level(animation, battery_level)
        self.frame_index = self.start_frame

        self.paused = False
        self.running = True

        self.tk_image = None

        self.label = tkinter.Label(root, background="black")
        self.label.pack(fill=tkinter.BOTH, expand=True)

        self.label.bind("<Configure>", self.on_resize)

        root.bind("<space>", self.toggle_pause)
        root.bind("<Left>", self.previous_frame)
        root.bind("<Right>", self.next_frame)
        root.bind("<r>", self.restart)
        root.bind("<Escape>", self.exit)

        root.title(
            f"AOSP Battery Animation "
            f"({battery_level}%, {self.frame_count} frames)"
        )

        self.last_animation_time = time.monotonic()

        self.show_frame()
        self.update()

    def on_resize(self, event):
        if event.width > 1 and event.height > 1:
            self.show_frame()

    def show_frame(self):
        image = self.images[self.frame_index]

        window_width = self.label.winfo_width()
        window_height = self.label.winfo_height()

        if window_width <= 1 or window_height <= 1:
            return

        image_width, image_height = image.size

        scale = min(window_width / image_width, window_height / image_height)

        new_size = (
            max(1, int(image_width * scale)),
            max(1, int(image_height * scale)),
        )

        resized = image.resize(new_size, Image.Resampling.NEAREST)

        self.tk_image = ImageTk.PhotoImage(resized)
        self.label.configure(image=self.tk_image)

    def update(self):
        if not self.running:
            return

        if not self.paused:
            now = time.monotonic()
            duration = self.animation[self.frame_index].duration_ms / 1000.0

            if now - self.last_animation_time >= duration:
                next_index = self.frame_index + 1

                if next_index >= self.frame_count:
                    next_index = self.start_frame

                if self.animation[next_index].matches(self.battery_level):
                    self.frame_index = next_index
                    self.last_animation_time = now
                    self.show_frame()
                else:
                    # The next frame's own range no longer covers the
                    # requested level - hold here. Manual Left/Right
                    # still work; only the automatic timer stops.
                    self.last_animation_time = now

        # Don't run every 1 ms. 20-60 Hz is more than enough.
        self.root.after(10, self.update)

    def toggle_pause(self, event=None):
        self.paused = not self.paused
        self.last_animation_time = time.monotonic()

    def previous_frame(self, event=None):
        self.frame_index = max(0, self.frame_index - 1)
        self.last_animation_time = time.monotonic()
        self.show_frame()

    def next_frame(self, event=None):
        self.frame_index = min(self.frame_count - 1, self.frame_index + 1)
        self.last_animation_time = time.monotonic()
        self.show_frame()

    def restart(self, event=None):
        self.frame_index = self.start_frame
        self.last_animation_time = time.monotonic()
        self.show_frame()

    def exit(self, event=None):
        self.running = False
        self.root.destroy()


def parse_args():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    ap.add_argument("sprite",
                    help="Path to the battery_scale.png sprite sheet")
    ap.add_argument("animation",
                    help="Path to the matching animation.txt")
    ap.add_argument("battery_level",
                    type=int, nargs="?", default=50,
                    help="Battery level to preview, 0-100 (default: 50)",
    )
    ap.add_argument("--save-gif",
                    metavar="PATH",
                    help="Export the matched playback sequence as an "
                         "animated GIF")
    ap.add_argument("--save-video",
                    metavar="PATH",
                    help="Export the matched playback sequence as an MP4 "
                         "(requires ffmpeg on PATH)")
    ap.add_argument("--no-window",
                    action="store_true",
                    help="Don't open the interactive viewer - only useful "
                         "together with --save-gif/--save-video")

    args = ap.parse_args()

    if args.no_window and not (args.save_gif or args.save_video):
        ap.error("--no-window requires --save-gif and/or --save-video")

    # Clamp rather than reject - a slightly out-of-range preview level
    # isn't worth aborting the whole viewer over.
    args.battery_level = max(0, min(100, args.battery_level))

    return args


def main():
    """Loads the sprite sheet + animation.txt named on the command line,
    decodes the sprite sheet into individual frames, and hands them to
    a BatteryViewer window.

    All frame *decoding* happens in extract_frames() below - this
    function only loads/validates the two input files and starts the
    Tk event loop with whatever extract_frames() produced.
    """
    args = parse_args()

    # animation.txt
    animation = parse_animation(args.animation)

    if animation is None:
        return 1

    # PNG
    try:
        image = Image.open(args.sprite)

        # Force metadata parsing before closing the file.
        frame_count = get_png_metadata(image)

        if frame_count is None:
            image.close()
            return 1

        print(f"PNG: {args.sprite}")
        print(f"  size:   {image.width}x{image.height}")
        print(f"  mode:   {image.mode}")
        print(f"  Frames: {frame_count}")

        print("animation.txt:")
        print(f"  frames: {len(animation)}")

        if len(animation) != frame_count:
            print(
                "\nERROR: PNG Frames does not match animation.txt "
                "frame count."
            )
            print(f"  PNG Frames = {frame_count}")
            print(f"  animation.txt frames = {len(animation)}")
            image.close()
            return 1

        # Convert after reading metadata.
        image = image.convert("RGBA")

    except Exception as e:
        print(f"Failed to load PNG: {e}")
        return 1

    # Decode AOSP row-interleaved sprite
    images = extract_frames(image, frame_count)
    image.close()

    if images is None:
        return 1

    width, height = images[0].size
    print(f"  frame size: {width}x{height}")

    # Export
    sequence = build_playback_sequence(images, animation, args.battery_level)

    print(
        f"  playback: {len(sequence)} frame(s) for {args.battery_level}% "
        f"({'loops' if len(sequence) > 1 else 'static'})"
    )

    export_failed = False

    if args.save_gif:
        if save_gif(sequence, args.save_gif):
            print(f"Saved GIF: {args.save_gif}")
        else:
            export_failed = True

    if args.save_video:
        if save_video(sequence, args.save_video):
            print(f"Saved video: {args.save_video}")
        else:
            export_failed = True

    if args.no_window:
        return 1 if export_failed else 0

    # Imported here rather than at module load, so --no-window (export
    # only) keeps working on systems with no Tk installation - the
    # interactive viewer below is the only thing in this file that
    # needs either module. `global` puts both names in the module's
    # namespace, where BatteryViewer's methods look them up.
    global tkinter, ImageTk
    import tkinter
    from PIL import ImageTk

    # Viewer
    root = tkinter.Tk()
    root.geometry(f"{width}x{height}")

    BatteryViewer(root, images, animation, args.battery_level)

    root.mainloop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
