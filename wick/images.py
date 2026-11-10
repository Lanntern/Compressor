"""Image conversion for every route in plan.py, plus verification.

All work happens on temp files next to the original (ASCII names, tools run
with ``cwd`` there), so non-ASCII folder/user names never reach the encoders.
"""

from __future__ import annotations

import filecmp
import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path

from . import metadata as md
from . import steps, tools
from .fileops import link_or_copy, remove_quietly, temp_name
from .formats import BMP, GIF, HEIC, JPEG, PNG, TIFF, WEBP, register_heif
from .plan import Plan
from .settings import Settings


class VerificationFailed(RuntimeError):
    pass


class NotWorthIt(RuntimeError):
    """The encoder couldn't reach the requested quality/size; keep the original."""


_SUBSAMPLING_PIL = {"444": "4:4:4", "422": "4:2:2", "420": "4:2:0"}
_PIL_SAMPLING_TO_KEY = {0: "444", 1: "422", 2: "420"}


@dataclass
class Ctx:
    src: Path
    kind: str
    info: object
    plan: Plan
    s: Settings
    out: Path
    staged: Path = None  # type: ignore[assignment]
    temps: list[Path] = field(default_factory=list)

    @property
    def dir(self) -> Path:
        return self.src.parent

    def tmp(self, ext: str, tag: str = "") -> Path:
        p = temp_name(self.dir, ext, tag)
        self.temps.append(p)
        return p

    @property
    def deep(self) -> bool:
        return getattr(self.info, "bit_depth", 8) > 8

    @property
    def animated(self) -> bool:
        return getattr(self.info, "frames", 1) > 1


# ======================================================================= entry

SMART_ROUTES = {"jxl", "webp", "avif", "heic", "jpeg", "png_quant"}
SMART_QUALITIES = [96, 93, 90, 88, 85, 82, 79, 76, 72, 68, 64, 60, 55, 50]  # best quality first


def convert_image(src: Path, kind: str, info, plan: Plan, s: Settings) -> Path:
    """Encode ``src`` per ``plan`` into a temp file next to it, verify, return its path.

    With Smart quality (lossy static images), searches for the most compressed quality
    that still meets the perceptual target; ``plan.outcome`` records the choice.
    """
    animated = info is not None and getattr(info, "frames", 1) > 1
    limit = s.fit_under_kb * 1024
    if limit and plan.route in SMART_ROUTES and not plan.lossless and not animated and src.stat().st_size > limit:
        return _convert_to_fit(src, kind, info, plan, s, limit)
    if not (s.smart_quality and plan.route in SMART_ROUTES and not plan.lossless and not animated):
        return _convert_once(src, kind, info, plan, s)
    return _convert_smart(src, kind, info, plan, s)


FIT_QUALITIES = [95, 92, 90, 87, 85, 82, 80, 77, 75, 72, 70, 65, 60, 55, 50, 45, 40]   # best first


def _convert_to_fit(src: Path, kind: str, info, plan: Plan, s: Settings, limit: int) -> Path:
    """The best quality that fits under ``limit`` bytes; if even low quality doesn't, smaller dimensions too."""
    import dataclasses
    import math

    from .formats import human

    outputs: list[Path] = []
    try:
        def attempt(q: int, resize=None) -> Path:
            p = dataclasses.replace(plan, resize=resize) if resize is not None else plan
            out = _convert_once(src, kind, info, p, dataclasses.replace(s, image_quality=q),
                                label=f"Trying quality {q} to fit under {human(limit)}")
            outputs.append(out)
            return out

        # Binary search for the first (best) quality that fits: sizes shrink as quality drops.
        lo, hi, best = 0, len(FIT_QUALITIES) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            out = attempt(FIT_QUALITIES[mid], plan.resize)
            if out.stat().st_size <= limit:
                best, hi = (FIT_QUALITIES[mid], out), mid - 1
            else:
                lo = mid + 1
        dims = plan.resize or (getattr(info, "width", 0), getattr(info, "height", 0))
        q = 75
        tries = 0
        while best is None and tries < 6 and dims[0] > 16 and dims[1] > 16:
            # Even the lowest quality is too big: fewer pixels, at a sensible quality.
            last = outputs[-1].stat().st_size if outputs else limit * 2
            scale = max(0.3, min(0.92, math.sqrt(limit / max(1, last)) * 0.95))
            dims = (max(16, int(dims[0] * scale)), max(16, int(dims[1] * scale)))
            out = attempt(q, dims)
            tries += 1
            if out.stat().st_size <= limit:
                best = (q, out)
                plan.resize = dims
        if best is None:
            raise VerificationFailed(f"Couldn't make it smaller than {human(limit)}")
        q, out = best
        plan.outcome = f"quality {q}" + (f", {plan.resize[0]}×{plan.resize[1]}" if plan.resize else "") + \
            f" to fit under {human(limit)}"
        for o in outputs:
            if o != out:
                remove_quietly(o)
        return out
    except BaseException:
        remove_quietly(*outputs)
        raise


def _convert_smart(src: Path, kind: str, info, plan: Plan, s: Settings) -> Path:
    import dataclasses

    from . import quality

    ref_ctx = Ctx(src, kind, info, plan, s, temp_name(src.parent, ".png"))
    ref_ctx.staged = ref_ctx.tmp(src.suffix.lower(), "-in")
    outputs: list[Path] = []
    by_quality: dict[int, Path] = {}
    try:
        link_or_copy(src, ref_ctx.staged)
        reference = _intermediate_png(ref_ctx)

        def attempt(q: int):
            out = _convert_once(src, kind, info, plan, dataclasses.replace(s, image_quality=q),
                                label=f"Trying quality {q} (Smart quality looks for the smallest that looks the same)")
            outputs.append(out)
            by_quality[q] = out
            shown = ref_ctx.tmp(".png", "-q")
            shown.write_bytes(decode_for_display(out))
            score = quality.score_images(reference, shown)
            return score.meets(s.smart_target), (out, score)

        chosen = quality.search(SMART_QUALITIES, attempt)
        if chosen is None:  # even the best quality misses the target: keep the best-quality attempt
            q = SMART_QUALITIES[0]
            out = by_quality.get(q)          # the attempt at this quality (not just any attempt)
            if out is None or not out.exists():
                out = _convert_once(src, kind, info, plan, dataclasses.replace(s, image_quality=q))
                outputs.append(out)
            plan.outcome = f"quality {q} (target not reached)"
        else:
            q, (out, score) = chosen  # type: ignore[misc]  # (the search hands back what attempt() made)
            plan.outcome = f"quality {q}, {score}"
        for o in outputs:
            if o != out:
                remove_quietly(o)
        assert out is not None
        return out
    except BaseException:
        remove_quietly(*outputs)
        raise
    finally:
        remove_quietly(*ref_ctx.temps)


def measure_quality(src: Path, kind: str, info, plan: Plan, s: Settings, out: Path):
    """Perceptual score of ``out`` against the (resized) original, as Smart quality measures it."""
    from . import quality

    ctx = Ctx(src, kind, info, plan, s, temp_name(src.parent, ".png"))
    ctx.staged = ctx.tmp(src.suffix.lower(), "-in")
    try:
        link_or_copy(src, ctx.staged)
        reference = _intermediate_png(ctx)
        shown = ctx.tmp(".png", "-q")
        shown.write_bytes(decode_for_display(out))
        return quality.score_images(reference, shown)
    finally:
        remove_quietly(*ctx.temps)


def _convert_once(src: Path, kind: str, info, plan: Plan, s: Settings, label: str = "") -> Path:
    if kind == HEIC or plan.route == "heic":
        register_heif()
    ctx = Ctx(src, kind, info, plan, s, temp_name(src.parent, plan.target_ext))
    try:
        ctx.staged = ctx.tmp(src.suffix.lower(), "-in")
        link_or_copy(src, ctx.staged)
        if s.metadata != "keep" and kind in (JPEG, PNG, WEBP):
            cleaned = ctx.tmp(src.suffix.lower(), "-meta")
            if md.rewrite_file_meta(ctx.staged, cleaned, kind, s.metadata):
                ctx.staged = cleaned
        steps.report(label or f"Encoding {steps.format_name(plan.target_ext)}")
        check = ROUTES[plan.route](ctx)
        if not ctx.out.exists() or ctx.out.stat().st_size == 0:
            raise RuntimeError("Encoder produced no output")
        steps.report("Checking the result")
        check()
        return ctx.out
    except BaseException:
        remove_quietly(ctx.out)
        raise
    finally:
        remove_quietly(*ctx.temps)


# ======================================================================= helpers

def _open(path: Path):
    from PIL import Image

    if path.suffix.lower() in _FAST_FORMATS:
        with open(path, "rb") as f:
            if f.read(2) == b"P7":            # PAM (has alpha): Pillow can't read it
                return read_pam(path)
    return Image.open(path)


def read_pam(path: Path):
    """A PAM file (what djxl writes for images with alpha) as a Pillow image (16-bit reduced to 8)."""
    from PIL import Image

    data = path.read_bytes()
    end = data.index(b"ENDHDR\n") + 7
    fields = dict(line.split(b" ", 1) for line in data[:end].split(b"\n")[1:] if b" " in line)
    w, h, depth = int(fields[b"WIDTH"]), int(fields[b"HEIGHT"]), int(fields[b"DEPTH"])
    maxval = int(fields[b"MAXVAL"])
    mode = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}[depth]
    raw = data[end:]
    if maxval > 255:
        import numpy as np

        raw = (np.frombuffer(raw, ">u2", w * h * depth) >> 8).astype(np.uint8).tobytes()
    return Image.frombytes(mode, (w, h), raw[:w * h * depth])


def _filtered_meta(ctx: Ctx, img) -> md.Meta:
    return md.filter_meta(md.read_meta(img), ctx.s.metadata)


def _ffmpeg_png(ctx: Ctx, src: Path, size: tuple[int, int] | None, eight_bit: bool = False) -> Path:
    """Decode with ffmpeg to PNG (keeps 16-bit unless ``eight_bit``), optionally resized."""
    out = ctx.tmp(".png", "-ff")
    args = [tools.require("ffmpeg"), "-v", "error", "-nostdin", "-y", "-i", src.name, "-frames:v", "1"]
    if size:
        args += ["-vf", f"scale={size[0]}:{size[1]}:flags=lanczos+accurate_rnd+full_chroma_int"]
    if eight_bit:
        args += ["-pix_fmt", "rgba"]
    # Temporary file: store it uncompressed (ffmpeg's default PNG compression is slow).
    tools.run([*args, "-compression_level", "0", out.name], cwd=ctx.dir, timeout=600)
    return out


def _load_static(ctx: Ctx, eight_bit_ok: bool = True):
    """(Pillow image, filtered metadata) of the first frame, resized if planned.

    16-bit sources go through ffmpeg (Pillow would truncate them silently).
    """
    from PIL import Image

    with _open(ctx.staged) as im:
        meta = _filtered_meta(ctx, im)
        if ctx.deep:
            path = _ffmpeg_png(ctx, ctx.staged, ctx.plan.resize, eight_bit=eight_bit_ok)
            img = Image.open(path)
            img.load()
            return img, meta
        if im.format != "PSD":      # (a Photoshop file opens on its flattened picture; its "frames" are layers)
            im.seek(0)
        img = im.copy() if im.mode not in ("P", "PA") else im.convert("RGBA" if _has_alpha(im) else "RGB")
        orientation = int(im.getexif().get(md.ORIENTATION, 1) or 1)
    # Never let encoders re-attach the source's metadata behind our back.
    img.info = {k: v for k, v in img.info.items() if k in ("transparency", "dpi")}
    resize = ctx.plan.resize
    if orientation != 1:
        # Rotate the pixels and reset the tag: JPEG XL/AVIF viewers ignore EXIF orientation.
        method = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE",
                  6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90"}.get(orientation)
        if method:
            from PIL import Image

            img = img.transpose(getattr(Image.Transpose, method))
        img.info = {k: v for k, v in img.info.items() if k in ("transparency", "dpi")}
        meta = md.Meta(md.set_orientation(meta.exif, 1), meta.xmp, meta.icc)
        if resize and orientation in (5, 6, 7, 8):
            resize = (resize[1], resize[0])
    if img.mode == "CMYK" and ctx.plan.route not in ("jpeg", "tiff"):
        img = img.convert("RGB")
    if img.mode not in ("1", "L", "LA", "RGB", "RGBA", "CMYK", "I;16", "I"):
        img = img.convert("RGBA" if _has_alpha(img) else "RGB")
    if resize:
        img = _resize(img, resize)
    return img, meta


def _has_alpha(im) -> bool:
    return im.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in im.info


def _resize(img, size):
    from PIL import Image

    if img.mode in ("1", "P"):
        img = img.convert("RGBA" if _has_alpha(img) else "RGB")
    return img.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)


def _intermediate_png(ctx: Ctx) -> Path:
    """A PNG of the (resized) first frame, metadata attached, for CLI encoders."""
    if ctx.deep:
        return _ffmpeg_png(ctx, ctx.staged, ctx.plan.resize)
    img, meta = _load_static(ctx)
    if img.mode == "CMYK":
        img = img.convert("RGB")
    path = ctx.tmp(".png", "-mid")
    kw = {"icc_profile": meta.icc} if meta.icc else {}
    img.save(path, "PNG", compress_level=0, **kw)   # temporary: uncompressed is faster to write and read
    if meta.exif or meta.xmp:
        path.write_bytes(md.png_write_meta(path.read_bytes(), md.Meta(meta.exif, meta.xmp, None), False))
    return path


def _load_frames(path: Path):
    """All frames as RGBA images, with durations and loop count."""
    from PIL import ImageSequence

    with _open(path) as im:
        loop = im.info.get("loop", 0)
        frames, durations = [], []
        for fr in ImageSequence.Iterator(im):
            f = fr.convert("RGBA")
            f.info = {}
            frames.append(f)
            durations.append(int(fr.info.get("duration", im.info.get("duration", 100)) or 100))
    return frames, durations, loop


# ---------------------------------------------------------------- digests

def digest_images(images) -> tuple[int, str]:
    """Count + SHA-256 of premultiplied RGBA pixels (invisible colour under alpha=0 ignored)."""
    h = hashlib.sha256()
    n = 0
    for im in images:
        rgba = im.convert("RGBA").convert("RGBa")
        h.update(f"{rgba.width}x{rgba.height};".encode())
        h.update(rgba.tobytes())
        n += 1
    return n, h.hexdigest()


_FAST_FORMATS = (".ppm", ".pgm", ".pam")   # RGB, grey, with alpha (libjxl 0.11 wants the matching one)


def _djxl_fast(path: Path, make_out, png_only: bool = False) -> Path | None:
    """Decode with djxl to an uncompressed file (PPM/PGM/PAM, whichever fits the image), else PNG."""
    for ext in (() if png_only else _FAST_FORMATS) + (".png",):
        out = make_out(ext)
        try:
            tools.run([tools.require("djxl"), path.name, out.name], cwd=path.parent, timeout=1800)
            if out.exists():
                return out
        except tools.ToolError:
            remove_quietly(out)
            if ext == ".png":
                raise
    return None


def _decoded_path(ctx: Ctx, path: Path) -> Path:
    """Something Pillow can read: JPEG XL is decoded with djxl first."""
    if path.suffix.lower() != ".jxl":
        return path
    # Uncompressed output (.ppm: djxl writes PPM/PGM/PAM as fits) is up to 16x faster than PNG. Animations
    # need PNG (APNG) to keep every frame.
    animated = ctx.info is not None and getattr(ctx.info, "animated", False)
    out = _djxl_fast(path, lambda ext: ctx.tmp(ext, "-dec"), png_only=animated)
    if out is None or not out.exists():
        raise VerificationFailed("JPEG XL output could not be decoded")
    return out


def _orientation(path: Path) -> int:
    try:
        with _open(path) as im:
            return int(im.getexif().get(0x0112, 1) or 1)
    except Exception:  # noqa: BLE001
        return 1


def digest_file(ctx: Ctx, path: Path, apply_orientation: bool = False) -> tuple[int, str]:
    """Digest of all frames. ``apply_orientation`` rotates like a viewer would
    (djxl applies the EXIF orientation when decoding JPEG XL)."""
    from PIL import ImageOps, ImageSequence

    path = _decoded_path(ctx, path)
    with _open(path) as im:
        if apply_orientation:
            o = _orientation(path)
            frames = (ImageOps.exif_transpose(fr.copy()) if o != 1 else fr.copy()
                      for fr in ImageSequence.Iterator(im))
            return digest_images(frames)
        return digest_images(fr.copy() for fr in ImageSequence.Iterator(im))


def pixel_hash(path: Path, deep: bool = False) -> str:
    """SHA-256 of the first frame decoded by ffmpeg as RGBA (16-bit when ``deep``)."""
    ffmpeg = tools.require("ffmpeg")
    proc = tools.run(
        [ffmpeg, "-v", "error", "-nostdin", "-i", path.name, "-map", "0:v:0", "-frames:v", "1",
         "-c:v", "rawvideo", "-pix_fmt", "rgba64le" if deep else "rgba", "-f", "hash", "-hash", "sha256", "-"],
        cwd=path.parent, timeout=600,
    )
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if "=" not in line:
        raise VerificationFailed("Couldn't decode image for comparison")
    return line.split("=", 1)[1]


def _check_same_pixels(ctx: Ctx, reference: Path):
    def check():
        if ctx.deep:
            out = _decoded_path(ctx, ctx.out)
            if pixel_hash(reference, True) != pixel_hash(out, True):
                raise VerificationFailed("Lossless output's pixels don't match the original")
            return
        to_jxl = ctx.out.suffix.lower() == ".jxl"
        if digest_file(ctx, reference, apply_orientation=to_jxl) != digest_file(ctx, ctx.out):
            raise VerificationFailed("Lossless output's pixels don't match the original")
    return check


def _check_same_digest(ctx: Ctx, digest: tuple[int, str]):
    def check():
        if digest_file(ctx, ctx.out) != digest:
            raise VerificationFailed("Lossless output's pixels don't match the original")
    return check


def _check_decodes(ctx: Ctx):
    """Lossy output: must decode, with the expected size and frame count."""
    want = ctx.plan.resize or (ctx.info.width, ctx.info.height)
    frames = ctx.info.frames if ctx.plan.route not in ("jpeg", "png_quant", "heic") else 1
    rotated = _orientation(ctx.staged) in (5, 6, 7, 8)

    def check():
        path = _decoded_path(ctx, ctx.out)
        try:
            with _open(path) as im:
                size = im.size
                n = getattr(im, "n_frames", 1)
                im.seek(n - 1)
                im.load()  # decode the last frame fully: catches truncation
        except VerificationFailed:
            raise
        except Exception as e:  # noqa: BLE001
            raise VerificationFailed(f"Output can't be decoded ({e})") from e
        if tuple(size) != tuple(want) and not (rotated and tuple(size) == (want[1], want[0])):
            raise VerificationFailed(f"Output is {size[0]}×{size[1]}, expected {want[0]}×{want[1]}")
        if frames > 1 and n != frames:
            raise VerificationFailed(f"Output has {n} frames, expected {frames}")
    return check


# ======================================================================= routes

def _route_jxl_transcode(ctx: Ctx):
    tools.run([tools.require("cjxl"), ctx.staged.name, ctx.out.name, "-e", str(ctx.s.image_effort),
               f"--num_threads={ctx.s.threads_per_job()}",
               "--quiet", "--lossless_jpeg=1"], cwd=ctx.dir, timeout=1800)

    def check():
        rebuilt = ctx.tmp(".jpg", "-dec")
        tools.run([tools.require("djxl"), ctx.out.name, rebuilt.name], cwd=ctx.dir, timeout=1800)
        if not rebuilt.exists() or not filecmp.cmp(ctx.staged, rebuilt, shallow=False):
            raise VerificationFailed("Reconstructed JPEG doesn't match the original")
    return check


def _route_jxl(ctx: Ctx):
    p = ctx.plan
    direct = not p.resize and ctx.kind in (PNG, GIF)
    src = ctx.staged if direct else _intermediate_png(ctx)
    quality = ["-d", "0"] if p.lossless else ["-q", str(ctx.s.image_quality)]
    try:
        tools.run([tools.require("cjxl"), src.name, ctx.out.name, "-e", str(ctx.s.image_effort),
               f"--num_threads={ctx.s.threads_per_job()}", "--quiet",
                   *quality], cwd=ctx.dir, timeout=3600)
    except tools.ToolError:
        if ctx.kind != GIF:
            raise
        # cjxl rejects GIFs whose pixels reference colours past the palette (common in
        # the wild); hand it the exact same frames as an APNG instead.
        remove_quietly(ctx.out)
        frames, durations, loop = _load_frames(ctx.staged)
        src = ctx.tmp(".png", "-apng")
        frames[0].save(src, "PNG", save_all=True, append_images=frames[1:], duration=durations,
                       loop=loop, disposal=1, blend=0, compress_level=0)
        tools.run([tools.require("cjxl"), src.name, ctx.out.name, "-e", str(ctx.s.image_effort),
               f"--num_threads={ctx.s.threads_per_job()}", "--quiet",
                   *quality], cwd=ctx.dir, timeout=3600)
    # cjxl doesn't reliably carry EXIF/XMP over from PNG input: write the boxes ourselves.
    with _open(src) as im:
        meta = md.read_meta(im)
    if direct:
        meta = md.filter_meta(meta, "keep")  # staged copy was already filtered
    if meta.exif or meta.xmp:
        ctx.out.write_bytes(md.jxl_write_meta(ctx.out.read_bytes(), meta))
    return _check_same_pixels(ctx, src) if p.lossless else _check_decodes(ctx)


def _pil_save_frames(ctx: Ctx, fmt: str, **opts):
    """Shared by WebP/AVIF/HEIC: encode static or animated with Pillow."""
    p = ctx.plan
    if ctx.animated:
        frames, durations, loop = _load_frames(ctx.staged)
        with _open(ctx.staged) as im:
            meta = _filtered_meta(ctx, im)
        digest = digest_images(frames) if p.lossless else None
        frames[0].save(ctx.out, fmt, save_all=True, append_images=frames[1:], duration=durations,
                       loop=loop, **opts, **meta.pil_kwargs())
    else:
        img, meta = _load_static(ctx)
        if img.mode in ("I;16", "I", "CMYK", "1"):
            img = img.convert("RGB")
        digest = digest_images([img]) if p.lossless else None
        img.save(ctx.out, fmt, **opts, **meta.pil_kwargs())
    return _check_same_digest(ctx, digest) if digest is not None else _check_decodes(ctx)


def webp_method(lossless: bool, pixels: int) -> int:
    """libwebp effort. Measured: method 6 vs 4 gains nothing for lossless (10-16x slower) and 0.2% on
    lossy photos (4x slower); it only pays off on small lossy images (screenshots: 1.6% smaller), where
    encoding is quick anyway."""
    return 6 if not lossless and pixels < 1_500_000 else 4


def _route_webp(ctx: Ctx):
    w, h = ctx.plan.resize or (ctx.info.width, ctx.info.height)
    method = webp_method(ctx.plan.lossless, w * h)
    if ctx.plan.lossless:
        opts = {"lossless": True, "quality": 100, "method": method, "exact": True}
    else:
        opts = {"quality": ctx.s.image_quality, "method": method}
    return _pil_save_frames(ctx, "WEBP", **opts)


def _route_avif(ctx: Ctx):
    q = ctx.s.image_quality
    return _pil_save_frames(ctx, "AVIF", quality=q, speed=4,
                            subsampling="4:4:4" if q >= 90 else "4:2:0")


def _route_heic(ctx: Ctx):
    return _pil_save_frames(ctx, "HEIF", quality=ctx.s.image_quality)


def _route_jpeg_opt(ctx: Ctx):
    import mozjpeg_lossless_optimization as mz

    data = mz.optimize(ctx.staged.read_bytes(), copy=mz.COPY_MARKERS.ALL)
    ctx.out.write_bytes(data)
    return _check_same_pixels(ctx, ctx.staged)


def _jpeg_subsampling(ctx: Ctx) -> str:
    sub = ctx.s.jpeg_subsampling
    if sub != "auto":
        return sub
    if ctx.kind == JPEG:
        try:
            from PIL import JpegImagePlugin

            with _open(ctx.staged) as im:
                key = _PIL_SAMPLING_TO_KEY.get(JpegImagePlugin.get_sampling(im))
            if key:
                return key
        except Exception:  # noqa: BLE001
            pass
    return "444" if ctx.s.image_quality >= 90 else "420"


def _route_jpeg(ctx: Ctx):
    s = ctx.s
    sub = _jpeg_subsampling(ctx)
    img, meta = _load_static(ctx)
    cjpegli = tools.find_tool("cjpegli")
    if cjpegli and img.mode in ("RGB", "L"):
        mid = ctx.tmp(".png", "-mid")
        img.save(mid, "PNG", compress_level=0)
        args = [cjpegli, mid.name, ctx.out.name, "-q", str(s.image_quality), f"--chroma_subsampling={sub}"]
        if not s.jpeg_progressive:
            args.append("--progressive_level=0")
        tools.run(args, cwd=ctx.dir, timeout=1800)
        data = ctx.out.read_bytes()
    else:
        if img.mode not in ("RGB", "L", "CMYK"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=s.image_quality, subsampling=_SUBSAMPLING_PIL[sub],
                 progressive=s.jpeg_progressive, optimize=True)
        data = buf.getvalue()
        if s.jpeg_progressive:
            import mozjpeg_lossless_optimization as mz

            data = mz.optimize(data, copy=mz.COPY_MARKERS.ALL)
    ctx.out.write_bytes(md.jpeg_write_meta(data, meta, drop_other=False))
    return _check_decodes(ctx)


def _oxipng(src: Path, dst: Path, deep: bool = False) -> None:
    from . import _workers, procpool

    procpool.call(_workers.oxipng_file, str(src), str(dst), deep)   # helper process: oxipng holds the GIL


def _route_png_opt(ctx: Ctx):
    if ctx.kind == PNG and not ctx.plan.resize:
        src = ctx.staged
    else:  # BMP, or resized
        src = _intermediate_png(ctx)
    _oxipng(src, ctx.out, deep=ctx.deep)
    return _check_same_pixels(ctx, src)


def _route_png_quant(ctx: Ctx):
    from . import _workers, procpool

    img, meta = _load_static(ctx)
    q = ctx.s.image_quality
    from PIL import Image

    rgba = img.convert("RGBA")
    try:
        # Raw-bytes API: the Pillow helper converts pixels in a slow Python loop.
        data, palette = procpool.call(_workers.quantize, rgba.tobytes(), rgba.width, rgba.height,
                                      max(0, q - 30), q)          # helper process: imagequant holds the GIL
    except RuntimeError as e:
        raise NotWorthIt(f"256 colours can't reach quality {q}; original kept") from e
    quant = Image.frombytes("P", rgba.size, data, "raw")
    quant.putpalette(palette, rawmode="RGBA")
    mid = ctx.tmp(".png", "-q")
    kw = {"icc_profile": meta.icc} if meta.icc else {}
    quant.save(mid, "PNG", **kw)
    _oxipng(mid, ctx.out)
    if meta.exif or meta.xmp:
        ctx.out.write_bytes(md.png_write_meta(ctx.out.read_bytes(), md.Meta(meta.exif, meta.xmp, None), False))
    return _check_decodes(ctx)


def _route_gif(ctx: Ctx):
    p, s = ctx.plan, ctx.s
    args = [tools.require("gifsicle"), "-O3", "--no-warnings"]
    if not p.lossless:
        args.append(f"--lossy={max(5, min(200, (100 - s.image_quality) * 2))}")
    if p.resize:
        args += ["--resize-fit", f"{p.resize[0]}x{p.resize[1]}"]
    if s.metadata not in ("keep", "strip_gps"):
        args.append("--no-comments")
    tools.run([*args, ctx.staged.name, "-o", ctx.out.name], cwd=ctx.dir, timeout=1800)
    if p.lossless and not p.resize:
        return _check_same_pixels(ctx, ctx.staged)
    return _check_decodes(ctx)


_TIFF_STRIP_ALL = (270, 315, 33432, 33723, 34853, 700, 37510, 34665)  # desc, artist, copyright, IPTC, GPS, XMP, comment, EXIF
_TIFF_STRIP_GPS = (34853,)


def _route_tiff(ctx: Ctx):
    from PIL import ImageSequence

    with _open(ctx.staged) as im:
        tags = dict(im.tag_v2) if hasattr(im, "tag_v2") else {}
        drop = _TIFF_STRIP_GPS if ctx.s.metadata == "strip_gps" else () if ctx.s.metadata == "keep" else \
            _TIFF_STRIP_ALL
        # Let Pillow write the structural tags itself.
        keep_tags = {k: v for k, v in tags.items() if k not in drop and k in (
            270, 271, 272, 305, 306, 315, 33432, 700, 33723, 34853, 34665, 282, 283, 296)}
        if ctx.s.metadata == "strip_gps":
            keep_tags.pop(34853, None)
        pages = [fr.copy() for fr in ImageSequence.Iterator(im)]
        icc = im.info.get("icc_profile")
    reference = digest_images(pages)
    kw = {"icc_profile": icc} if icc else {}
    pages[0].save(ctx.out, "TIFF", compression="tiff_adobe_deflate", save_all=True,
                  append_images=pages[1:], tiffinfo=keep_tags, **kw)
    return _check_same_digest(ctx, reference)


ROUTES = {
    "jxl_transcode": _route_jxl_transcode,
    "jxl": _route_jxl,
    "webp": _route_webp,
    "avif": _route_avif,
    "heic": _route_heic,
    "jpeg_opt": _route_jpeg_opt,
    "jpeg": _route_jpeg,
    "png_opt": _route_png_opt,
    "png_quant": _route_png_quant,
    "gif": _route_gif,
    "tiff": _route_tiff,
}


# ======================================================================= preview

def decode_for_display(path: Path) -> bytes:
    """PNG bytes of the first frame of any supported image (for the preview window)."""
    from PIL import Image

    if path.suffix.lower() in (".heic", ".heif", ".hif"):
        register_heif()
    if path.suffix.lower() == ".jxl":
        made: list[Path] = []

        def tmp(ext):
            made.append(temp_name(path.parent, ext, "-view"))
            return made[-1]
        try:
            out = _djxl_fast(path, tmp)   # uncompressed: far faster than djxl's PNG writer
            if out is None:
                out = tmp(".png")         # (no fast route: djxl writes a PNG)
                tools.run([tools.require("djxl"), str(path), str(out)], timeout=600)
            img = _open(out)
            img.load()
            buf = io.BytesIO()
            img.save(buf, "PNG", compress_level=0)
            return buf.getvalue()
        finally:
            remove_quietly(*made)
    from PIL import ImageOps

    with Image.open(path) as im:
        im.seek(0)
        img = (ImageOps.exif_transpose(im) or im).convert("RGBA")  # show it the way viewers do
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=0)   # in memory, read once: compression only costs time
    return buf.getvalue()


__all__ = ["convert_image", "VerificationFailed", "NotWorthIt", "decode_for_display", "pixel_hash",
           "BMP", "GIF", "TIFF"]
