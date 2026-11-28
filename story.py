import bisect
import gc
import itertools
import logging
import os
import re
import shutil
import subprocess
import threading

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("story")

W, H = 720, 1280
FPS = 24
MIN_SECONDS = 3
MAX_CLIP_SECONDS = 8
LAYOUT_VERSION = "4"

FLAME_WIDTH = 300
GROUP_CENTER_Y = H * 0.5
CENTER_X = W / 2

NUMBER_PER_DIGIT = 128
NUMBER_MAX_W = 400
NUMBER_MAX_H = 190
LABEL_RATIO = 0.85
LABEL_MIN_W = 260
LABEL_MAX_W = 380
LABEL_MAX_H = 60
GAP_STICKER_NUMBER = 60
GAP_NUMBER_LABEL = 40
TEXT_RENDER_SIZE = 360
EDGE_MARGIN = 60

FLAME_NAMES = ("flame.apng", "flame.webp", "flame.webm", "flame.mov", "flame.gif", "flame.png")
VIDEO_EXT = {".webm", ".mov"}

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


def _find_flame():
    for name in FLAME_NAMES:
        for folder in (HERE, ASSETS):
            path = os.path.join(folder, name)
            if os.path.exists(path):
                return path
    return None


def _source_signature() -> str:
    paths = [_find_flame()]

    for path in _font_candidates():
        if os.path.exists(path):
            try:
                ImageFont.truetype(path, 20)
                paths.append(path)
                break
            except Exception:
                continue

    parts = [f"layout-{LAYOUT_VERSION}"]
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


def _fit(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    if img.width != FLAME_WIDTH:
        height = max(1, round(img.height * FLAME_WIDTH / img.width))
        img = img.resize((FLAME_WIDTH, height), Image.LANCZOS)
    return img


def _ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _once(img):
    yield img


def _single_frame(path):
    with Image.open(path) as img:
        yield _fit(img)


def _durations(path) -> list[int]:
    durs = []
    with Image.open(path) as img:
        for i in range(getattr(img, "n_frames", 1)):
            img.seek(i)
            durs.append(max(int(img.info.get("duration") or 40), 10))
    return durs


def _pil_frames(path, durs):
    total = sum(durs)
    count = max(1, round(total * FPS / 1000))
    ends = list(itertools.accumulate(durs))

    with Image.open(path) as img:
        last, current = -1, None
        for k in range(count):
            idx = min(bisect.bisect_right(ends, k * 1000 / FPS), len(durs) - 1)
            if idx != last:
                img.seek(idx)
                current = _fit(img)
                last = idx
            yield current


def _probe_size(exe, path):
    try:
        res = subprocess.run(
            [exe, "-hide_banner", "-i", path],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return None
    for line in res.stderr.splitlines():
        if "Video:" in line:
            m = re.search(r",\s*(\d{2,5})x(\d{2,5})", line)
            if m:
                return int(m.group(1)), int(m.group(2))
    return None


def _video_frames(path):
    exe = _ffmpeg_exe()
    if not exe:
        return
    size = _probe_size(exe, path)
    if not size:
        log.warning("Could not read the size of %s", path)
        return

    fw = FLAME_WIDTH
    fh = max(1, round(size[1] * fw / size[0]))
    need = fw * fh * 4
    decoders = [["-c:v", "libvpx-vp9"], []] if path.lower().endswith(".webm") else [[]]

    for decoder in decoders:
        cmd = [
            exe, "-v", "error", "-threads", "2", *decoder,
            "-i", path, "-t", str(MAX_CLIP_SECONDS),
            "-vf", f"fps={FPS},scale={fw}:{fh}:flags=lanczos,format=rgba",
            "-f", "rawvideo", "-pix_fmt", "rgba", "-",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        got = 0
        try:
            while True:
                buf = proc.stdout.read(need)
                if len(buf) < need:
                    break
                got += 1
                yield Image.frombytes("RGBA", (fw, fh), buf)
        finally:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        if got:
            return


def _open_flame():
    path = _find_flame()
    if not path:
        return False, lambda: _once(_procedural_flame())

    if os.path.splitext(path)[1].lower() in VIDEO_EXT:
        return True, lambda: _video_frames(path)

    try:
        durs = _durations(path)
    except Exception:
        log.exception("Could not read %s, using the drawn flame", path)
        return False, lambda: _once(_procedural_flame())

    if len(durs) > 1:
        return True, lambda: _pil_frames(path, durs)
    return False, lambda: _single_frame(path)


def _median(values: bytes) -> float:
    total = sum(values)
    if not total:
        return len(values) / 2
    acc, half = 0, total / 2
    for i, v in enumerate(values):
        if acc + v >= half:
            return i + (half - acc) / v if v else i
        acc += v
    return len(values) / 2


def _metrics(img: Image.Image):
    alpha = img.getchannel("A")
    box = alpha.point(lambda v: 255 if v > 40 else 0).getbbox()
    if not box:
        return img.width / 2, 0, img.height
    cols = alpha.resize((img.width, 1), Image.BOX).tobytes()
    return _median(cols), box[1], box[3]


def _sprite(text: str, target_w: int, max_h: int) -> Image.Image:
    font = _font(TEXT_RENDER_SIZE)
    box = font.getbbox(text, anchor="ls")
    pad = 6
    mask = Image.new("L", (box[2] - box[0] + pad * 2, box[3] - box[1] + pad * 2), 0)
    ImageDraw.Draw(mask).text((pad - box[0], pad - box[1]), text, font=font, fill=255, anchor="ls")
    ink = mask.getbbox()
    if ink:
        mask = mask.crop(ink)
    scale = target_w / mask.width
    if mask.height * scale > max_h:
        scale = max_h / mask.height
    size = (max(1, round(mask.width * scale)), max(1, round(mask.height * scale)))
    return mask.resize(size, Image.LANCZOS)


def _paste_text(layer: Image.Image, mask: Image.Image, top: int) -> None:
    x = round(CENTER_X - mask.width / 2)
    shift = max(2, round(mask.height * 0.05))
    layer.paste((90, 10, 20, 90), (x + shift // 2, top + shift), mask)
    layer.paste((255, 255, 255, 255), (x, top), mask)


def _background(n: int, first: Image.Image):
    cx, box_top, box_bottom = _metrics(first)
    sticker_h = box_bottom - box_top

    number = str(n)
    label = "day in a row" if n == 1 else "days in a row"
    number_w = min(NUMBER_MAX_W, NUMBER_PER_DIGIT * len(number) + 10)
    number_img = _sprite(number, number_w, NUMBER_MAX_H)
    label_w = round(min(LABEL_MAX_W, max(LABEL_MIN_W, number_img.width * LABEL_RATIO)))
    label_img = _sprite(label, label_w, LABEL_MAX_H)

    total = (sticker_h + GAP_STICKER_NUMBER + number_img.height
             + GAP_NUMBER_LABEL + label_img.height)
    top = max(EDGE_MARGIN, round(GROUP_CENTER_Y - total / 2))

    pos = (round(CENTER_X - cx), top - box_top)
    number_top = top + sticker_h + GAP_STICKER_NUMBER
    label_top = number_top + number_img.height + GAP_NUMBER_LABEL

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _paste_text(layer, number_img, number_top)
    _paste_text(layer, label_img, label_top)

    bg = _gradient()
    bg.paste(layer, (0, 0), layer)
    return bg, pos


def _render_png(n: int, flame: Image.Image, out: str) -> None:
    bg, pos = _background(n, flame)
    bg.paste(flame, pos, flame)
    bg.save(out, "PNG", optimize=True)


def _encode(exe, frames, bg, pos, out) -> int:
    cmd = [
        exe, "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-preset", "veryfast", "-crf", "24", "-threads", "2",
        "-movflags", "+faststart", "-an", out,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    canvas = bg.copy()
    count = 0

    try:
        for flame in frames:
            canvas.paste(bg)
            canvas.paste(flame, pos, flame)
            proc.stdin.write(canvas.tobytes())
            count += 1

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
        return 0

    ok = proc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0
    return count if ok else 0


def _remove(*paths) -> None:
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Could not remove %s", path)


def _render_mp4(n: int, make_frames, out: str) -> bool:
    exe = _ffmpeg_exe()
    if not exe:
        return False

    gen = make_frames()
    loop_path = out + ".loop.mp4"
    try:
        first = next(gen, None)
        if first is None:
            return False

        bg, pos = _background(n, first)
        count = _encode(exe, itertools.chain([first], gen), bg, pos, loop_path)
        if not count:
            return False

        loops = max(1, round(MIN_SECONDS / (count / FPS)))
        if loops == 1:
            os.replace(loop_path, out)
            return True

        res = subprocess.run(
            [exe, "-y", "-v", "error", "-stream_loop", str(loops - 1),
             "-i", loop_path, "-c", "copy", "-movflags", "+faststart", out],
            stderr=subprocess.DEVNULL, timeout=120,
        )
        return (
            res.returncode == 0
            and os.path.exists(out)
            and os.path.getsize(out) > 0
        )
    except Exception:
        log.exception("Video rendering failed")
        return False
    finally:
        gen.close()
        _remove(loop_path)


def story_path(n: int):
    for ext in ("mp4", "png"):
        path = os.path.join(OUT_DIR, f"story_{n}.{ext}")
        if os.path.exists(path):
            return path
    return None


KEEP_FILES = 3


def _prune(keep: int = KEEP_FILES) -> None:
    try:
        files = [os.path.join(OUT_DIR, f) for f in os.listdir(OUT_DIR) if re.fullmatch(r"story_\d+\.(mp4|png)", f)]
        files.sort(key=os.path.getmtime, reverse=True)
        for path in files[keep:]:
            _remove(path, path + ".meta")
    except OSError:
        log.warning("Could not prune the story cache")


def ensure_story(n: int):
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

        for ext in ("mp4", "png"):
            old_path = os.path.join(OUT_DIR, f"story_{n}.{ext}")
            _remove(old_path, old_path + ".meta")

        try:
            animated, make_frames = _open_flame()

            if animated:
                out = os.path.join(OUT_DIR, f"story_{n}.mp4")
                if _render_mp4(n, make_frames, out):
                    _write_cache_signature(out, signature)
                    _prune()
                    return out, "mp4"
                _remove(out)
                log.warning("Video rendering failed, falling back to a picture")

            gen = make_frames()
            try:
                flame = next(gen, None) or _procedural_flame()
            finally:
                gen.close()

            out = os.path.join(OUT_DIR, f"story_{n}.png")
            _render_png(n, flame, out)
            _write_cache_signature(out, signature)
            _prune()
            return out, "png"
        finally:
            gc.collect()
