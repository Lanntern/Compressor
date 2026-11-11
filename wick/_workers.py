"""Work that runs in helper processes: encoders that hold Python's GIL (oxipng, libimagequant) can't run
side by side in threads, so parallel jobs would take turns. Keep imports here minimal: each helper
process imports only this module."""

from __future__ import annotations


def set_low_priority(low: bool) -> None:
    if not low:
        return
    import os

    try:
        if os.name == "nt":
            import ctypes

            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except Exception:  # noqa: BLE001
        pass


def oxipng_file(src: str, dst: str, deep: bool) -> None:
    import oxipng

    # For 16-bit sources keep the bit depth/color type: the 16-bit verification
    # compares in 16-bit space, where ffmpeg's 8->16 and gray->RGB paths aren't bit-exact.
    if deep:
        oxipng.optimize(src, dst, level=4, bit_depth_reduction=False, grayscale_reduction=False)
    else:
        oxipng.optimize(src, dst, level=4)


def quantize(rgba: bytes, width: int, height: int, min_quality: int, max_quality: int) -> tuple[bytes, list]:
    import imagequant

    return imagequant.quantize_raw_rgba_bytes(rgba, width, height, dithering_level=1.0, max_colors=256,
                                              min_quality=min_quality, max_quality=max_quality)
