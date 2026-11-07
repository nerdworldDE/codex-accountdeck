"""Thread-name colors verified against Codex CLI 0.166.0, theme 1337.

The slot calculation uses the low five bits of FNV-1a over the canonical UUID.
Both the 32-bit and 64-bit FNV-1a variants give the same slots for this palette.
The palette and background-dependent contrast adjustment were checked against
native CLI terminal captures rather than inferred from session titles.
"""

from functools import lru_cache
import uuid


PALETTE_1337 = (
    (251, 227, 191), (253, 176, 130), (255, 137, 66), (233, 253, 172),
    (255, 94, 94), (251, 223, 181), (140, 218, 255), (252, 147, 84),
    (151, 216, 234), (102, 153, 204), (236, 253, 185), (255, 178, 249),
    (102, 217, 239), (214, 105, 144), (249, 38, 114), (205, 90, 197),
    (224, 93, 140), (229, 165, 224), (255, 225, 252), (255, 210, 166),
    (255, 208, 251), (144, 231, 247), (250, 184, 90), (255, 255, 255),
    (208, 208, 208), (102, 169, 236), (214, 153, 255), (166, 226, 46),
    (204, 0, 255), (153, 153, 153), (86, 86, 86), (117, 113, 94),
)


def luminance(rgb):
    linear = []
    for channel in rgb:
        value = channel / 255
        linear.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def contrast(left, right):
    low, high = sorted((luminance(left), luminance(right)))
    return (high + 0.05) / (low + 0.05)


def readable_color(foreground, background):
    if contrast(foreground, background) >= 4.5:
        return foreground
    target = max(((0, 0, 0), (255, 255, 255)), key=lambda rgb: contrast(rgb, background))
    for alpha in range(1, 256):
        candidate = tuple((value * (255 - alpha) + end * alpha) // 255 for value, end in zip(foreground, target))
        if contrast(candidate, background) >= 4.5:
            return candidate
    return target


@lru_cache(maxsize=4096)
def thread_color(thread_id, background=(0, 0, 0)):
    try:
        seed = str(uuid.UUID(thread_id))
    except ValueError:
        seed = thread_id
    slot = 5
    for byte in seed.encode("utf-8"):
        slot = ((slot ^ byte) * 19) & 31
    return readable_color(PALETTE_1337[slot], background)
