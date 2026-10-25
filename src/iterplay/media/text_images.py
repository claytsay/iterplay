"""Text drawn into PNG images, for the labels and title cards burned into clips (HLD §4 [4]).

ffmpeg's ``drawtext`` filter needs a build with freetype, which many lack, so media prep
draws its text here with a built-in 5x7 bitmap font and composites the images with
``overlay``, which every build has. Pure stdlib: the font, a small RGBA canvas and a PNG
encoder. Internal to :mod:`iterplay.media`.
"""

import struct
import zlib
from collections.abc import Mapping, Sequence
from typing import Final, TypeAlias

Rgba: TypeAlias = tuple[int, int, int, int]
"""A colour as 8-bit red, green, blue and alpha."""

GLYPH_WIDTH: Final = 5
GLYPH_HEIGHT: Final = 7
GLYPH_SPACING: Final = 1
"""Blank font pixels between neighbouring characters, and between lines."""

LABEL_TEXT: Final[Rgba] = (255, 255, 255, 255)
LABEL_BACKGROUND: Final[Rgba] = (0, 0, 0, 160)
"""Semi-opaque, so the game stays visible behind a label."""

LABEL_PADDING: Final = 4
"""Pixels of background around a label's text."""

CARD_TEXT: Final[Rgba] = (255, 255, 255, 255)
CARD_BACKGROUND: Final[Rgba] = (24, 26, 33, 255)

MAX_CARD_SCALE: Final = 8
"""Largest scale :func:`render_title_card` picks by itself, so short titles stay modest."""

CARD_FILL: Final = 0.8
"""Fraction of a title card's width and height that its text may cover."""

PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"

_RGBA_8BIT: Final = (8, 6)
"""PNG bit depth and colour type for 8-bit RGBA."""

_GLYPHS: Final[Mapping[str, tuple[str, ...]]] = {
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("###..", "#..#.", "#...#", "#...#", "#...#", "#..#.", "###.."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": (".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "#.#.#", ".#.#."),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("#####", "...#.", "..#..", "...#.", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    " ": (".....", ".....", ".....", ".....", ".....", ".....", "....."),
    ":": (".....", ".##..", ".##..", ".....", ".##..", ".##..", "....."),
    "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
    "_": (".....", ".....", ".....", ".....", ".....", ".....", "#####"),
    ".": (".....", ".....", ".....", ".....", ".....", ".##..", ".##.."),
    "/": (".....", "....#", "...#.", "..#..", ".#...", "#....", "....."),
    "(": ("...#.", "..#..", ".#...", ".#...", ".#...", "..#..", "...#."),
    ")": (".#...", "..#..", "...#.", "...#.", "...#.", "..#..", ".#..."),
    "#": (".#.#.", ".#.#.", "#####", ".#.#.", "#####", ".#.#.", ".#.#."),
}

_BOX: Final = ("#####", "#...#", "#...#", "#...#", "#...#", "#...#", "#####")
"""Drawn for any character the font lacks."""

SUPPORTED_CHARACTERS: Final = frozenset(_GLYPHS)
"""Characters with a glyph of their own; lowercase letters borrow the uppercase ones."""


def glyph(char: str) -> tuple[str, ...]:
    """The 5x7 bitmap of one character, as rows of ``#`` (ink) and ``.`` (blank).

    Lowercase letters share the uppercase glyphs. Any other character the font lacks is drawn
    as a box, so unexpected text stays visible and keeps its width.

    Raises:
        ValueError: ``char`` is not a single character.
    """
    if len(char) != 1:
        raise ValueError(f"expected one character, got {char!r}")
    return _GLYPHS.get(char.upper(), _BOX)


def text_size(text: str, scale: int) -> tuple[int, int]:
    """Width and height in pixels of one line of ``text`` drawn at ``scale``."""
    _check_scale(scale)
    columns = max(len(text) * (GLYPH_WIDTH + GLYPH_SPACING) - GLYPH_SPACING, 0)
    return columns * scale, GLYPH_HEIGHT * scale


class Canvas:
    """An RGBA image in memory, painted with solid rectangles and bitmap text."""

    def __init__(self, width: int, height: int, background: Rgba) -> None:
        if width < 1 or height < 1:
            raise ValueError(f"an image must be at least 1x1, not {width}x{height}")
        self.width = width
        self.height = height
        self._pixels = bytearray(bytes(background) * (width * height))

    def pixel(self, x: int, y: int) -> Rgba:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"({x}, {y}) is outside the {self.width}x{self.height} image")
        offset = (y * self.width + x) * 4
        red, green, blue, alpha = self._pixels[offset : offset + 4]
        return red, green, blue, alpha

    def fill_rect(self, x: int, y: int, width: int, height: int, color: Rgba) -> None:
        """Paint a rectangle with its top-left corner at (x, y), clipped to the image."""
        left, right = max(x, 0), min(x + width, self.width)
        top, bottom = max(y, 0), min(y + height, self.height)
        if left >= right or top >= bottom:
            return
        run = bytes(color) * (right - left)
        for row in range(top, bottom):
            start = (row * self.width + left) * 4
            self._pixels[start : start + len(run)] = run

    def draw_text(self, text: str, x: int, y: int, *, scale: int, color: Rgba) -> None:
        """Draw one line of text with its top-left corner at (x, y), clipped to the image.

        Each font pixel becomes a ``scale`` x ``scale`` square.
        """
        _check_scale(scale)
        advance = (GLYPH_WIDTH + GLYPH_SPACING) * scale
        for index, char in enumerate(text):
            self._draw_glyph(glyph(char), x + index * advance, y, scale, color)

    def to_png(self) -> bytes:
        return encode_png(self.width, self.height, bytes(self._pixels))

    def _draw_glyph(self, rows: Sequence[str], x: int, y: int, scale: int, color: Rgba) -> None:
        for row_index, row in enumerate(rows):
            for column_index, ink in enumerate(row):
                if ink == "#":
                    left, top = x + column_index * scale, y + row_index * scale
                    self.fill_rect(left, top, scale, scale, color)


def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    """Encode 8-bit RGBA pixels, row by row from the top-left corner, as a PNG file.

    Raises:
        ValueError: The size is not positive, or ``rgba`` does not hold exactly that many
            pixels.
    """
    stride = width * 4
    if width < 1 or height < 1 or len(rgba) != stride * height:
        raise ValueError(f"{len(rgba)} bytes are not a {width}x{height} RGBA image")
    scanlines = b"".join(b"\x00" + rgba[row * stride : (row + 1) * stride] for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, *_RGBA_8BIT, 0, 0, 0)
    return b"".join(
        (
            PNG_SIGNATURE,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(scanlines)),
            _chunk(b"IEND", b""),
        )
    )


def render_label(
    text: str,
    *,
    scale: int = 2,
    padding: int = LABEL_PADDING,
    color: Rgba = LABEL_TEXT,
    background: Rgba = LABEL_BACKGROUND,
) -> bytes:
    """One line of ``text`` on a semi-opaque dark box, as a PNG to overlay on video.

    The image is the text's size plus ``padding`` pixels on every side, so labels with the
    same number of characters always have the same dimensions.
    """
    if padding < 0:
        raise ValueError(f"padding must not be negative, got {padding}")
    text_width, text_height = text_size(text, scale)
    canvas = Canvas(max(text_width + 2 * padding, 1), text_height + 2 * padding, background)
    canvas.draw_text(text, padding, padding, scale=scale, color=color)
    return canvas.to_png()


def render_title_card(
    text: str,
    *,
    width: int,
    height: int,
    scale: int | None = None,
    color: Rgba = CARD_TEXT,
    background: Rgba = CARD_BACKGROUND,
) -> bytes:
    """A ``width`` x ``height`` image with ``text`` centred in large letters, as a PNG.

    Each line of ``text`` (split on ``\\n``) is centred on its own. Without ``scale``, the text
    gets the largest scale, up to ``MAX_CARD_SCALE``, at which it covers at most ``CARD_FILL``
    of the card; text too long even at scale 1 is clipped at the edges.
    """
    lines = text.split("\n")
    if scale is None:
        scale = _fitting_scale(lines, int(width * CARD_FILL), int(height * CARD_FILL))
    _check_scale(scale)
    line_pitch = (GLYPH_HEIGHT + GLYPH_SPACING) * scale
    block_height = len(lines) * line_pitch - GLYPH_SPACING * scale
    canvas = Canvas(width, height, background)
    top = (height - block_height) // 2
    for index, line in enumerate(lines):
        line_width, _ = text_size(line, scale)
        canvas.draw_text(
            line, (width - line_width) // 2, top + index * line_pitch, scale=scale, color=color
        )
    return canvas.to_png()


def _fitting_scale(lines: Sequence[str], max_width: int, max_height: int) -> int:
    widest = max(text_size(line, 1)[0] for line in lines)
    block_height = len(lines) * (GLYPH_HEIGHT + GLYPH_SPACING) - GLYPH_SPACING
    fits = min(max_width // max(widest, 1), max_height // block_height)
    return max(1, min(fits, MAX_CARD_SCALE))


def _check_scale(scale: int) -> None:
    if scale < 1:
        raise ValueError(f"scale must be at least 1, got {scale}")


def _chunk(kind: bytes, data: bytes) -> bytes:
    """One PNG chunk: length, type, data and the CRC of type and data."""
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
