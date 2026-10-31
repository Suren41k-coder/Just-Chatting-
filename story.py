"""Renders the "N days in a row" story picture (PNG) or video (MP4).

Put your flame file next to this file (or into an assets/ folder):
  - animated: flame.apng, flame.webp or flame.gif  -> the story becomes an MP4 video
  - static:   flame.png                            -> the story becomes a PNG picture
  - nothing:  a simple flame is drawn automatically
Optional: font.ttf (a bold font) for nicer text.
"""
import logging
import math
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
FLAME_CENTER_Y = 500

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
OUT_DIR = os.environ.get("STORY_DIR", "/tmp/stories")

_lock = threading.Lock()


# ---------- helpers ----------
def _font(size: int):
    for path in (os.path.join(ASSETS, "font.ttf"), os.path.join(HERE, "font.ttf"),
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _gradient() -> Image.Image:
    stops = [(0.0, (238, 152, 100)), (0.5, (224, 88, 54)), (1.0, (138, 22, 52))]
    col = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / (H - 1)
        for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
            if t0 <= t <= t1:
                k = (t - t0) / (t1 - t0)
                col.putpixel((0, y), tuple(int(c0[i] + (c1[i] - c0[i]) * k) for i in range(3)))
                break
    return col.resize((W, H))


def _procedural_flame() -> Image.Image:
    """A simple flame, used when no flame file is provided."""
    s = 3  # draw large, then shrink for smooth edges
    w, h = FLAME_WIDTH * s, int(FLAME_WIDTH * 1.25) * s
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    outer = [(0.50, 0.00), (0.60, 0.16), (0.74, 0.30), (0.90, 0.54), (0.90, 0.78), (0.74, 0.95),
             (0.50, 1.00), (0.26, 0.95), (0.10, 0.78), (0.10, 0.54), (0.24, 0.36), (0.32, 0.46),
             (0.38, 0.22)]
    d.polygon([(x * w, y * h) for x, y in outer], fill=(206, 38, 44, 245))
    inner = [(0.50, 0.38), (0.58, 0.52), (0.70, 0.66), (0.70, 0.84), (0.58, 0.95), (0.50, 0.97),
             (0.42, 0.95), (0.30, 0.84), (0.30, 0.66), (0.42, 0.52)]
    d.polygon([(x * w, y * h) for x, y in inner], fill=(250, 190, 60, 250))
    return img.resize((FLAME_WIDTH, int(FLAME_WIDTH * 1.25)), Image.LANCZOS)


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
    frames, durs = [], []
    for fr in ImageSequence.Iterator(img):
        frame = fr.convert("RGBA").copy()
        ratio = FLAME_WIDTH / frame.width
        frame = frame.resize((FLAME_WIDTH, max(1, int(frame.height * ratio))), Image.LANCZOS)
        frames.append(frame)
        durs.append(int(fr.info.get("duration") or 40))
    return frames, durs


def _text_layer(n: int) -> Image.Image:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    digits = len(str(n))
    size = {1: 520, 2: 440, 3: 340}.get(digits, 260)
    num_font = _font(size)
    cx, cy = W // 2, FLAME_CENTER_Y + 40
    d.text((cx + 6, cy + 10), str(n), font=num_font, fill=(90, 10, 20, 90), anchor="mm")
    d.text((cx, cy), str(n), font=num_font, fill=(255, 255, 255, 255), anchor="mm")
    label = "day in a row" if n == 1 else "days in a row"
    label_font = _font(78)
    ly = int(H * 0.70)
    d.text((cx + 3, ly + 4), label, font=label_font, fill=(90, 10, 20, 90), anchor="mm")
    d.text((cx, ly), label, font=label_font, fill=(255, 255, 255, 255), anchor="mm")
    return layer


def _ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


# ---------- rendering ----------
def _render_png(n: int, flame: Image.Image, out: str) -> None:
    canvas = _gradient()
    pos = ((W - flame.width) // 2, FLAME_CENTER_Y - flame.height // 2)
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
    cum, acc = [], 0
    for d in durs:
        acc += d
        cum.append(acc)

    cmd = [exe, "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
           "-crf", "24", "-movflags", "+faststart", "-an", out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        for i in range(count):
            t = (i * 1000 / FPS) % total
            idx = next((k for k, c in enumerate(cum) if t < c), len(frames) - 1)
            fl = frames[idx]
            frame = base.copy()
            frame.paste(fl, ((W - fl.width) // 2, FLAME_CENTER_Y - fl.height // 2), fl)
            frame.paste(text, (0, 0), text)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        proc.wait(timeout=300)
    except Exception:
        log.exception("ffmpeg failed")
        try:
            proc.kill()
        except Exception:
            pass
        return False
    return proc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0


def story_path(n: int):
    """Returns the cached file for this number if it exists."""
    for ext in ("mp4", "png"):
        p = os.path.join(OUT_DIR, f"story_{n}.{ext}")
        if os.path.exists(p):
            return p
    return None


def ensure_story(n: int):
    """Renders (or finds in the cache) the story for streak n. Returns (path, 'mp4' | 'png')."""
    n = max(0, int(n))
    os.makedirs(OUT_DIR, exist_ok=True)
    with _lock:
        cached = story_path(n)
        if cached:
            return cached, cached.rsplit(".", 1)[1]
        frames, durs = _load_flame()
        if len(frames) > 1:
            out = os.path.join(OUT_DIR, f"story_{n}.mp4")
            if _render_mp4(n, frames, durs, out):
                return out, "mp4"
            log.warning("video rendering failed, falling back to a picture")
        out = os.path.join(OUT_DIR, f"story_{n}.png")
        _render_png(n, frames[0], out)
        return out, "png"
