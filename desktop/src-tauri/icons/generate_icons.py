"""Regenerate every application icon from one vector description.

Run with any Python 3.12+ interpreter — it imports nothing outside the standard library:

    python desktop/src-tauri/icons/generate_icons.py

The mark is drawn once in normalised coordinates and rasterised at 4x with box-filter
downsampling, so a 16px favicon and a 1024px macOS icon come from the same geometry and
cannot drift apart. Colours are the accent ramp from ``docs/UI.md`` §2.2 — ``--accent-hover``
(#6165DE) to ``--accent-active`` (#5155C4) — with the glyph in ``--fg-primary`` (#F2F4F8).

Three container formats are written because three platforms want different things:

``*.png``
    What ``tauri::generate_context!`` embeds as the default window icon, which the tray
    reuses at runtime.
``icon.ico``
    Embedded into the Windows executable's resource table by ``tauri-build``. Every entry is
    an uncompressed 32-bit BGRA DIB with a full AND mask rather than a PNG payload: PNG
    entries are legal only from Vista onward and some resource compilers reject them, while
    a DIB entry is understood by every version of the ICO parser.
``icon.icns``
    macOS bundle icon. Modern ``ic07``/``ic08``/``ic09``/``ic12`` element types take PNG
    payloads directly, so no RLE encoder is needed.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Final

#: Supersampling factor. 4x is the point where the diagonal legs of the mark stop showing
#: stair-steps at 16px; 8x costs 4x the time for no visible difference.
SUPERSAMPLE: Final[int] = 4

#: Corner radius as a fraction of the icon edge — the macOS "squircle" proportion, which
#: also reads correctly inside the Windows taskbar's square slot.
CORNER_RADIUS_RATIO: Final[float] = 0.2237

#: Gradient endpoints (``--accent-hover`` → ``--accent-active``) and the glyph colour.
GRADIENT_TOP: Final[tuple[int, int, int]] = (0x61, 0x65, 0xDE)
GRADIENT_BOTTOM: Final[tuple[int, int, int]] = (0x51, 0x55, 0xC4)
GLYPH: Final[tuple[int, int, int]] = (0xF2, 0xF4, 0xF8)

#: PNG sizes `bundle.icon` in `tauri.conf.json` lists. The first of these is also what
#: `tauri::generate_context!` embeds as the default window icon, which the tray then reuses.
PNG_SIZES: Final[dict[str, int]] = {
    "32x32.png": 32,
    "128x128.png": 128,
    "128x128@2x.png": 256,
    "icon.png": 512,
}

#: Entries written into ``icon.ico``. 256 is the largest an ICO directory can address.
ICO_SIZES: Final[tuple[int, ...]] = (16, 24, 32, 48, 64, 128, 256)

#: ICNS element types keyed by the pixel size their PNG payload must be.
ICNS_TYPES: Final[dict[int, bytes]] = {
    64: b"ic12",
    128: b"ic07",
    256: b"ic08",
    512: b"ic09",
}


# ======================================================================================
# Geometry — the mark, in a unit square
# ======================================================================================


def _stroke(x0: float, y0: float, x1: float, y1: float, width: float) -> list[tuple[float, float]]:
    """Return the quad covering a line segment of the given width.

    Args:
        x0: Start x, in unit coordinates.
        y0: Start y, in unit coordinates.
        x1: End x, in unit coordinates.
        y1: End y, in unit coordinates.
        width: Stroke width, in unit coordinates.

    Returns:
        The four corners of the stroke, counter-clockwise.
    """
    dx, dy = x1 - x0, y1 - y0
    length = (dx * dx + dy * dy) ** 0.5
    nx, ny = -dy / length * width / 2.0, dx / length * width / 2.0
    return [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]


#: The glyph: an "A" built from two legs, a flat apex cap, and a crossbar, drawn as four
#: overlapping quads. An apex at 0.5 and feet at 0.21/0.79 keep the optical centre at the
#: icon's centre once the crossbar's mass is accounted for. The cap is not decoration: two
#: butt-capped legs meeting at a point leave a V-shaped notch between their top edges, and a
#: true miter at this angle would grow a spike half the glyph's height. A flat apex is what
#: a typeface would cut here anyway.
GLYPH_POLYGONS: Final[tuple[list[tuple[float, float]], ...]] = (
    _stroke(0.500, 0.215, 0.215, 0.800, 0.132),
    _stroke(0.500, 0.215, 0.785, 0.800, 0.132),
    _stroke(0.441, 0.215, 0.559, 0.215, 0.058),
    _stroke(0.300, 0.630, 0.700, 0.630, 0.104),
)


def _inside_polygon(polygon: Sequence[tuple[float, float]], x: float, y: float) -> bool:
    """Test a point against a polygon with the even-odd rule.

    Args:
        polygon: The polygon's vertices.
        x: Sample x, in unit coordinates.
        y: Sample y, in unit coordinates.

    Returns:
        Whether the point lies inside.
    """
    inside = False
    count = len(polygon)
    for index in range(count):
        ax, ay = polygon[index]
        bx, by = polygon[(index + 1) % count]
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return inside


def _inside_rounded_square(x: float, y: float, radius: float) -> bool:
    """Test a point against the rounded background plate.

    Args:
        x: Sample x, in unit coordinates.
        y: Sample y, in unit coordinates.
        radius: Corner radius, in unit coordinates.

    Returns:
        Whether the point lies inside the plate.
    """
    cx = min(max(x, radius), 1.0 - radius)
    cy = min(max(y, radius), 1.0 - radius)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= radius * radius


# ======================================================================================
# Rasteriser
# ======================================================================================


def render_rgba(size: int) -> bytearray:
    """Rasterise the icon at the given edge length.

    Args:
        size: Output edge length in pixels.

    Returns:
        Straight (non-premultiplied) RGBA bytes, row-major, top row first.
    """
    scale = SUPERSAMPLE
    high = size * scale
    radius = CORNER_RADIUS_RATIO

    # Accumulate coverage and colour per output pixel from `scale * scale` samples.
    pixels = bytearray(size * size * 4)
    for py in range(size):
        row_base = py * size * 4
        for px in range(size):
            plate_hits = 0
            glyph_hits = 0
            gradient_sum = 0.0
            for sy in range(scale):
                y = (py * scale + sy + 0.5) / high
                for sx in range(scale):
                    x = (px * scale + sx + 0.5) / high
                    if not _inside_rounded_square(x, y, radius):
                        continue
                    plate_hits += 1
                    gradient_sum += y
                    if any(_inside_polygon(poly, x, y) for poly in GLYPH_POLYGONS):
                        glyph_hits += 1

            offset = row_base + px * 4
            if plate_hits == 0:
                continue

            samples = float(scale * scale)
            mean_y = gradient_sum / plate_hits
            plate = tuple(
                round(top + (bottom - top) * mean_y)
                for top, bottom in zip(GRADIENT_TOP, GRADIENT_BOTTOM, strict=True)
            )
            glyph_weight = glyph_hits / plate_hits
            colour = tuple(
                round(plate[channel] + (GLYPH[channel] - plate[channel]) * glyph_weight)
                for channel in range(3)
            )
            pixels[offset] = colour[0]
            pixels[offset + 1] = colour[1]
            pixels[offset + 2] = colour[2]
            pixels[offset + 3] = round(255 * plate_hits / samples)
    return pixels


# ======================================================================================
# Containers
# ======================================================================================


def encode_png(size: int, rgba: bytes) -> bytes:
    """Encode RGBA pixels as a PNG.

    Args:
        size: Edge length in pixels.
        rgba: Straight RGBA bytes, row-major.

    Returns:
        The PNG file bytes.
    """

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    stride = size * 4
    raw = bytearray()
    for row in range(size):
        raw.append(0)  # filter type 0 (None) — the images are tiny and flat.
        raw += rgba[row * stride : (row + 1) * stride]

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def encode_ico(images: dict[int, bytes]) -> bytes:
    """Pack rasterised images into a Windows ICO with 32-bit DIB entries.

    Args:
        images: Edge length → straight RGBA bytes.

    Returns:
        The ICO file bytes.
    """
    entries: list[tuple[int, bytes]] = []
    for size in sorted(images):
        rgba = images[size]
        stride = size * 4
        colour = bytearray()
        for row in range(size - 1, -1, -1):  # DIBs are stored bottom-up.
            line = rgba[row * stride : (row + 1) * stride]
            for index in range(0, stride, 4):
                colour += bytes(
                    (line[index + 2], line[index + 1], line[index], line[index + 3])
                )
        # The AND mask is unused for 32-bit entries but must be present and 4-byte aligned.
        mask_stride = ((size + 31) // 32) * 4
        mask = bytes(mask_stride * size)
        # biHeight is doubled: it covers the colour plane plus the mask plane.
        info = struct.pack(
            "<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(colour) + len(mask), 0, 0, 0, 0
        )
        entries.append((size, info + bytes(colour) + mask))

    header = struct.pack("<HHH", 0, 1, len(entries))
    directory = bytearray()
    offset = len(header) + 16 * len(entries)
    for size, payload in entries:
        directory += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(payload), offset
        )
        offset += len(payload)
    return header + bytes(directory) + b"".join(payload for _, payload in entries)


def encode_icns(pngs: dict[int, bytes]) -> bytes:
    """Pack PNG payloads into a macOS ICNS.

    Args:
        pngs: Edge length → PNG file bytes. Sizes absent from :data:`ICNS_TYPES` are skipped.

    Returns:
        The ICNS file bytes.
    """
    elements = bytearray()
    for size, kind in sorted(ICNS_TYPES.items()):
        payload = pngs.get(size)
        if payload is None:
            continue
        elements += kind + struct.pack(">I", len(payload) + 8) + payload
    return b"icns" + struct.pack(">I", len(elements) + 8) + bytes(elements)


def main() -> None:
    """Rasterise every size once and write all three container formats."""
    here = Path(__file__).resolve().parent
    needed = sorted({*PNG_SIZES.values(), *ICO_SIZES, *ICNS_TYPES})
    rendered = {size: bytes(render_rgba(size)) for size in needed}

    for name, size in PNG_SIZES.items():
        (here / name).write_bytes(encode_png(size, rendered[size]))
    (here / "icon.ico").write_bytes(encode_ico({size: rendered[size] for size in ICO_SIZES}))
    (here / "icon.icns").write_bytes(
        encode_icns({size: encode_png(size, rendered[size]) for size in ICNS_TYPES})
    )
    print(f"wrote {len(PNG_SIZES) + 2} icon files to {here}")


if __name__ == "__main__":
    main()
