"""
Renders the "N days in a row" story picture (PNG) or video (MP4).

Put your flame file next to this file (or into an assets/ folder):
  - animated: flame.apng, flame.webp or flame.gif -> MP4 video
  - static:   flame.png                            -> PNG picture
  - nothing:  a simple flame is drawn automatically
Optional: font.ttf (a bold font) for nicer text.

The cache is automatically refreshed when the flame or font file changes.
"""
import logging
import os
import shutil
import subprocess
import threading

from PIL import Image, ImageDraw, ImageFont, ImageSequence

log = logging.getLogger("story")

W, H = 720, 1280
FPS = 24
MIN_SECONDS = 3
FLAME_WIDTH = 560
FLAME_CENTER_Y = 360

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
OUT_DIR = os.environ.get("STORY_DIR", "/tmp/stories")

_lock = threading.Lock()


def _font_candidates():
    return (
        os.path.join(ASSETS, "font.ttf"),
        os.path.join(HERE, "font.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    )


def _font(size: int):
    for path in _font_candidates():
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _source_signature() -> str:
    """Signature of the flame and font assets used for rendering."""
    paths = [_find_flame()]

    for path in _font_candidates():
        if os.path.exists(path):
            try:
                ImageFont.truetype(path, 20)
                paths.append(path)
                break
            except Exception:
                continue

    parts = []
    for path in paths:
        if path and os.path.exists(path):
            stat = os.stat(path)
            parts.append(
                f"{os.path.abspath(path)}:{stat.st_mtime_ns}:{stat.st_size}"
            )
        else:
            parts.append("procedural-flame" if not path else str(path))

    return "|".join(parts)


def _write_cache_signature(out: str, signature: str) -> None:
    meta = out + ".meta"
    temp = meta + ".tmp"
    with open(temp, "w", encoding="utf-8") as file:
        file.write(signature)
    os.replace(temp, meta)


def _gradient() -> Image.Image:
    stops = [
        (0.0, (238, 152, 100)),
        (0.5, (224, 88, 54)),
        (1.0, (138, 22, 52)),
    ]
    col = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / (H - 1)
        for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
            if t0 <= t <= t1:
                k = (t - t0) / (t1 - t0)
                col.putpixel(
                    (0, y),
                    tuple(
                        int(c0[i] + (c1[i] - c0[i]) * k)
                        for i in range(3)
                    ),
                )
                break
    return col.resize((W, H))


def _procedural_flame() -> Image.Image:
    """A simple flame, used when no flame file is provided."""
    s = 3
    w, h = FLAME_WIDTH * s, int(FLAME_WIDTH * 1.25) * s
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    outer = [
        (0.50, 0.00), (0.60, 0.16), (0.74, 0.30), (0.90, 0.54),
        (0.90, 0.78), (0.74, 0.95), (0.50, 1.00), (0.26, 0.95),
        (0.10, 0.78), (0.10, 0.54), (0.24, 0.36), (0.32, 0.46),
        (0.38, 0.22),
    ]
    draw.polygon(
        [(x * w, y * h) for x, y in outer],
        fill=(206, 38, 44, 245),
    )

    inner = [
        (0.50, 0.38), (0.58, 0.52), (0.70, 0.66), (0.70, 0.84),
        (0.58, 0.95), (0.50, 0.97), (0.42, 0.95), (0.30, 0.84),
        (0.30, 0.66), (0.42, 0.52),
    ]
    draw.polygon(
        [(x * w, y * h) for x, y in inner],
        fill=(250, 190, 60, 250),
    )

    return img.resize(
        (FLAME_WIDTH, int(FLAME_WIDTH * 1.25)),
        Image.LANCZOS,
    )


def _find_flame():
    for name in ("flame.apng", "flame.webp", "flame.gif", "flame.png"):
        for folder in (HERE, ASSETS):
            path = os.path.join(folder, name)
            if os.path.exists(path):
                return path
    return None


def _load_flame():
    """Returns (frames, durations_ms). One frame means a static picture."""
    path = _find_flame()
    if not path:
        return [_procedural_flame()], [100]

    img = Image.open(path)
    frames, durations = [], []

    for frame in ImageSequence.Iterator(img):
        current = frame.convert("RGBA").copy()
        ratio = FLAME_WIDTH / current.width
        current = current.resize(
            (FLAME_WIDTH, max(1, int(current.height * ratio))),
            Image.LANCZOS,
        )
        frames.append(current)
        durations.append(int(frame.info.get("duration") or 40))

    if not frames:
        return [_procedural_flame()], [100]

    return frames, durations


def _text_layer(n: int) -> Image.Image:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    digits = len(str(n))
    size = {1: 420, 2: 380, 3: 300}.get(digits, 260)
    num_font = _font(size)

    cx, cy = W // 2, 860
    draw.text(
        (cx + 6, cy + 10), str(n), font=num_font,
        fill=(90, 10, 20, 90), anchor="mm",
    )
    draw.text(
        (cx, cy), str(n), font=num_font,
        fill=(255, 255, 255, 255), anchor="mm",
    )

    label = "day in a row" if n == 1 else "days in a row"
    label_font = _font(52)
    ly = 1090

    draw.text(
        (cx + 3, ly + 4), label, font=label_font,
        fill=(90, 10, 20, 90), anchor="mm",
    )
    draw.text(
        (cx, ly), label, font=label_font,
        fill=(255, 255, 255, 255), anchor="mm",
    )

    return layer


def _ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _render_png(n: int, flame: Image.Image, out: str) -> None:
    canvas = _gradient()
    pos = (
        (W - flame.width) // 2,
        FLAME_CENTER_Y - flame.height // 2,
    )
    canvas.paste(flame, pos, flame)
    text = _text_layer(n)
    canvas.paste(text, (0, 0), text)
    canvas.save(out, "PNG", optimize=True)


def _render_mp4(n: int, frames, durs, out: str) -> bool:
    exe = _ffmpeg_exe()
    if not exe:
        return False

    total = max(sum(durs), 1)
    loops = max(1, round(MIN_SECONDS * 1000 / total))
    duration = total * loops
    count = max(1, int(duration / 1000 * FPS))

    base = _gradient()
    text = _text_layer(n)

    cumulative, acc = [], 0
    for duration_ms in durs:
        acc += duration_ms
        cumulative.append(acc)

    cmd = [
        exe, "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-preset", "veryfast", "-crf", "24",
        "-movflags", "+faststart", "-an", out,
    ]

    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
    )

    try:
        for i in range(count):
            t = (i * 1000 / FPS) % total
            idx = next(
                (k for k, end in enumerate(cumulative) if t < end),
                len(frames) - 1,
            )
            flame = frames[idx]
            frame = base.copy()
            frame.paste(
                flame,
                (
                    (W - flame.width) // 2,
                    FLAME_CENTER_Y - flame.height // 2,
                ),
                flame,
            )
            frame.paste(text, (0, 0), text)
            proc.stdin.write(frame.tobytes())

        proc.stdin.close()
        proc.wait(timeout=300)

    except Exception:
        log.exception("ffmpeg failed")
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        return False

    return (
        proc.returncode == 0
        and os.path.exists(out)
        and os.path.getsize(out) > 0
    )


def story_path(n: int):
    """Returns the cached file for this number if it exists."""
    for ext in ("mp4", "png"):
        path = os.path.join(OUT_DIR, f"story_{n}.{ext}")
        if os.path.exists(path):
            return path
    return None


def ensure_story(n: int):
    """
    Renders (or finds in the cache) the story for streak n.
    Returns (path, 'mp4' | 'png').
    """
    n = max(0, int(n))
    os.makedirs(OUT_DIR, exist_ok=True)

    with _lock:
        signature = _source_signature()
        cached = story_path(n)

        if cached:
            meta = cached + ".meta"
            if os.path.exists(meta):
                try:
                    with open(meta, "r", encoding="utf-8") as file:
                        if file.read() == signature:
                            return cached, cached.rsplit(".", 1)[1]
                except OSError:
                    pass

        # Remove stale formats too, so an old MP4 cannot be selected
        # if rendering now falls back to PNG.
        for ext in ("mp4", "png"):
            old_path = os.path.join(OUT_DIR, f"story_{n}.{ext}")
            for path in (old_path, old_path + ".meta"):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError:
                    log.warning("Could not remove stale cache file: %s", path)

        frames, durations = _load_flame()

        if len(frames) > 1:
            out = os.path.join(OUT_DIR, f"story_{n}.mp4")
            if _render_mp4(n, frames, durations, out):
                _write_cache_signature(out, signature)
                return out, "mp4"
            log.warning("Video rendering failed, falling back to a picture")

        out = os.path.join(OUT_DIR, f"story_{n}.png")
        _render_png(n, frames[0], out)
        _write_cache_signature(out, signature)
        return out, "png"
