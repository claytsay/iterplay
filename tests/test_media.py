"""Media prep turns bot recordings into labelled 640x360 clips for the critic (HLD §4 [4]).

Tests marked ``ffmpeg`` run the real tools on synthetic recordings of ffmpeg's test pattern.
The others drive the wrapper with stand-in executables and the preparer with a scripted
``FFmpeg``, so they need no ffmpeg at all.
"""

import asyncio
import dataclasses
import itertools
import json
import logging
import math
import os
import shutil
import string
import struct
import subprocess
import sys
import tempfile
import zlib
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from iterplay.errors import MediaError
from iterplay.media import text_images
from iterplay.media.ffmpeg import FFmpeg
from iterplay.media.prep import LABEL_OFFSET, LABEL_SCALE, MediaPreparer, MediaPrepConfig
from iterplay.models.media import PreparedMedia
from iterplay.models.playtest import BotName
from iterplay.storage.layout import VersionPaths

FONT = string.ascii_uppercase + string.digits + " :-_./()#"
"""Characters the label font must draw."""

WIDTH, HEIGHT = 640, 360
RECORDING_S = {BotName.RANDOM: 3.0, BotName.SPEEDRUNNER: 2.5, BotName.NEWBIE: 2.0}
RECORDING_CODECS = {BotName.SPEEDRUNNER: "libvpx-vp9"}
"""VP8 unless listed here; recent Playwright releases record VP9."""
CARD_S = MediaPrepConfig().title_card_s
TEXT_ORIGIN = LABEL_OFFSET + text_images.LABEL_PADDING
"""Where the text of a burned-in label starts, on both axes."""

InkTest = Callable[[int, int], bool]


# Reading images back


@dataclasses.dataclass(frozen=True)
class Image:
    """A decoded 8-bit RGBA image."""

    width: int
    height: int
    rows: tuple[bytes, ...]

    def pixel(self, x: int, y: int) -> tuple[int, ...]:
        return tuple(self.rows[y][4 * x : 4 * x + 4])

    def is_ink(self, x: int, y: int) -> bool:
        return self.pixel(x, y) == text_images.LABEL_TEXT

    def rgb(self) -> bytes:
        return bytes(
            value for row in self.rows for index, value in enumerate(row) if index % 4 != 3
        )


@dataclasses.dataclass(frozen=True)
class Frame:
    """One video frame as packed 8-bit RGB."""

    width: int
    height: int
    rgb: bytes

    def is_ink(self, x: int, y: int) -> bool:
        """Bright enough to be label text on the label's dark box."""
        offset = 3 * (y * self.width + x)
        red, green, blue = self.rgb[offset : offset + 3]
        return 0.299 * red + 0.587 * green + 0.114 * blue > 160

    def crop(self, left: int, top: int, width: int, height: int) -> bytes:
        return b"".join(
            self.rgb[3 * (row * self.width + left) : 3 * (row * self.width + left + width)]
            for row in range(top, top + height)
        )


def png_chunks(data: bytes) -> Iterator[tuple[bytes, bytes]]:
    """The type and payload of every chunk after the signature, checking each CRC."""
    offset = len(text_images.PNG_SIGNATURE)
    while offset < len(data):
        (length,) = struct.unpack_from(">I", data, offset)
        kind, payload = data[offset + 4 : offset + 8], data[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack_from(">I", data, offset + 8 + length)
        assert crc == zlib.crc32(kind + payload), f"bad CRC in {kind!r}"
        yield kind, payload
        offset += 12 + length


def decode_png(data: bytes) -> Image:
    """Decode a PNG written by ``text_images``, checking its signature, header and rows."""
    assert data.startswith(text_images.PNG_SIGNATURE)
    chunks = list(png_chunks(data))
    kinds = [kind for kind, _ in chunks]
    assert (kinds[0], kinds[-1]) == (b"IHDR", b"IEND") and b"IDAT" in kinds
    width, height, *format_fields = struct.unpack(">IIBBBBB", chunks[0][1])
    assert format_fields == [8, 6, 0, 0, 0], "8-bit RGBA, deflate, no filter, no interlace"
    scanlines = zlib.decompress(b"".join(payload for kind, payload in chunks if kind == b"IDAT"))
    stride = 1 + 4 * width
    assert len(scanlines) == stride * height
    rows = [scanlines[row * stride : (row + 1) * stride] for row in range(height)]
    assert all(row[0] == 0 for row in rows), "every row uses filter type 0"
    return Image(width, height, tuple(row[1:] for row in rows))


def read_text(is_ink: InkTest, left: int, top: int, length: int, scale: int = LABEL_SCALE) -> str:
    """Read ``length`` characters of the label font drawn from (left, top) at ``scale``."""
    step = (text_images.GLYPH_WIDTH + text_images.GLYPH_SPACING) * scale
    return "".join(read_glyph(is_ink, left + index * step, top, scale) for index in range(length))


def read_glyph(is_ink: InkTest, left: int, top: int, scale: int) -> str:
    """The character whose glyph best matches the middles of the font pixels at (left, top).

    Taking the nearest glyph means a pixel blurred by video compression does not change the
    reading, while any two glyphs differ in enough pixels to tell them apart.
    """
    centre = scale // 2
    seen = [
        is_ink(left + column * scale + centre, top + row * scale + centre)
        for row in range(text_images.GLYPH_HEIGHT)
        for column in range(text_images.GLYPH_WIDTH)
    ]

    def mismatches(char: str) -> int:
        inked = [mark == "#" for row in text_images.glyph(char) for mark in row]
        return sum(expected != actual for expected, actual in zip(inked, seen, strict=True))

    return min(sorted(text_images.SUPPORTED_CHARACTERS), key=mismatches)


def label_text(label: Image) -> str:
    """The text of a label image from ``render_label`` with the default padding."""
    pad = text_images.LABEL_PADDING
    step = (text_images.GLYPH_WIDTH + text_images.GLYPH_SPACING) * LABEL_SCALE
    length = (label.width - 2 * pad + text_images.GLYPH_SPACING * LABEL_SCALE) // step
    return read_text(label.is_ink, pad, pad, length).rstrip()


def mean_difference(first: bytes, second: bytes) -> float:
    assert len(first) == len(second)
    return sum(abs(a - b) for a, b in zip(first, second, strict=True)) / len(first)


def composite(label: Image, under: bytes) -> bytes:
    """``label`` alpha-blended over ``under``, an RGB crop of the same size."""
    blended = bytearray()
    for y in range(label.height):
        for x in range(label.width):
            *colour, alpha = label.pixel(x, y)
            offset = 3 * (y * label.width + x)
            for top, bottom in zip(colour, under[offset : offset + 3], strict=True):
                blended.append(round((top * alpha + bottom * (255 - alpha)) / 255))
    return bytes(blended)


def title_card(bot: BotName) -> bytes:
    """The RGB pixels of the title card that introduces ``bot``."""
    card = text_images.render_title_card(bot.value.upper(), width=WIDTH, height=HEIGHT)
    return decode_png(card).rgb()


# Real tools, for the tests marked ffmpeg


@dataclasses.dataclass(frozen=True)
class VideoInfo:
    codec: str
    width: int
    height: int
    pixel_format: str
    fps: float
    frames: int
    duration_s: float


def tool_output(*command: str) -> bytes:
    return subprocess.run(command, capture_output=True, check=True).stdout


def record_test_pattern(
    path: Path, seconds: float, *, size: str = "1280x720", codec: str = "libvpx"
) -> Path:
    """A webm of ffmpeg's test pattern at 25 fps, shaped like a Playwright recording."""
    pattern = f"testsrc=size={size}:rate=25:duration={seconds}"
    encoder = ["-c:v", codec, "-deadline", "realtime", "-cpu-used", "8", "-b:v", "1M"]
    tool_output("ffmpeg", "-v", "error", "-f", "lavfi", "-i", pattern, *encoder, str(path))
    return path


def grab_frame(video: Path, at_s: float, size: tuple[int, int] = (WIDTH, HEIGHT)) -> Frame:
    """The frame showing at ``at_s``, scaled to ``size``."""
    width, height = size
    rgb = tool_output(
        *("ffmpeg", "-v", "error", "-ss", f"{at_s:.3f}", "-i", str(video)),
        *("-vf", f"scale={width}:{height}", "-frames:v", "1"),
        *("-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"),
    )
    assert len(rgb) == 3 * width * height
    return Frame(width, height, rgb)


def probe(video: Path) -> VideoInfo:
    entries = "stream=codec_name,width,height,pix_fmt,r_frame_rate,nb_read_frames:format=duration"
    report = json.loads(
        tool_output(
            *("ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0"),
            *("-show_entries", entries, "-of", "json", str(video)),
        )
    )
    (stream,) = report["streams"]
    numerator, denominator = stream["r_frame_rate"].split("/")
    return VideoInfo(
        codec=stream["codec_name"],
        width=stream["width"],
        height=stream["height"],
        pixel_format=stream["pix_fmt"],
        fps=int(numerator) / int(denominator),
        frames=int(stream["nb_read_frames"]),
        duration_s=float(report["format"]["duration"]),
    )


def label_at(video: Path, at_s: float, length: int) -> str:
    """The burned-in label showing at ``at_s``."""
    return read_text(grab_frame(video, at_s).is_ink, TEXT_ORIGIN, TEXT_ORIGIN, length)


def nearest_title_card(frame: Frame) -> BotName:
    return min(BotName, key=lambda bot: mean_difference(frame.rgb, title_card(bot)))


# Version directories and stand-ins


def install(paths: VersionPaths, recordings: Mapping[BotName, Path]) -> VersionPaths:
    """Copy recordings to where the playtest runner leaves them."""
    paths.playtest_dir.mkdir(parents=True, exist_ok=True)
    for bot, recording in recordings.items():
        shutil.copyfile(recording, paths.raw_video(bot))
    return paths


def placeholder_version(root: Path, *bots: BotName) -> VersionPaths:
    """A version with a recording for each of ``bots`` that is not really a video."""
    paths = VersionPaths(root / "v1")
    paths.playtest_dir.mkdir(parents=True)
    for bot in bots:
        paths.raw_video(bot).write_bytes(b"not really a webm")
    return paths


def file_names(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir()}


def inputs_of(command: list[str]) -> list[Path]:
    return [Path(command[index + 1]) for index, arg in enumerate(command) if arg == "-i"]


class ScriptedFFmpeg(FFmpeg):
    """Stands in for ffmpeg: records each command and writes a placeholder for its output.

    Commands for which ``fails`` is true write a partial output, then raise ``MediaError``.
    Input images are decoded while they still exist: each clock label sequence into
    ``clocks``, and each title card image into ``cards`` under the card's output path.
    """

    def __init__(
        self, *, duration_s: float = 2.5, fails: Callable[[list[str]], bool] | None = None
    ) -> None:
        super().__init__(ffmpeg="no-such-ffmpeg", ffprobe="no-such-ffprobe")
        self.commands: list[list[str]] = []
        self.clocks: list[list[Image]] = []
        self.cards: dict[Path, bytes] = {}
        self._duration_s = duration_s
        self._fails = fails or (lambda command: False)

    async def duration_s(self, path: Path) -> float:
        return self._duration_s

    async def run(self, *args: str | Path) -> None:
        command = [str(arg) for arg in args]
        self.commands.append(command)
        await asyncio.to_thread(self._execute, command)

    def _execute(self, command: list[str]) -> None:
        output = Path(command[-1])
        output.write_bytes(b"placeholder")
        if self._fails(command):
            raise MediaError("scripted ffmpeg failure")
        for source in inputs_of(command):
            if "%04d" in source.name:
                images = sorted(source.parent.glob("*.png"))
                self.clocks.append([decode_png(image.read_bytes()) for image in images])
            elif "-loop" in command:
                self.cards[output] = source.read_bytes()

    def encodes(self) -> list[list[str]]:
        """Commands that wrote a clip, as opposed to a title card."""
        return [command for command in self.commands if "-loop" not in command]


class HangingFFmpeg(ScriptedFFmpeg):
    """Starts writing its output, then never finishes."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def run(self, *args: str | Path) -> None:
        await asyncio.to_thread(Path(args[-1]).write_bytes, b"partial")
        self.started.set()
        await asyncio.Event().wait()


def fake_tool(directory: Path, body: str, *, executable: bool = True) -> str:
    """An executable stand-in for ffmpeg or ffprobe that runs ``body`` as Python."""
    script = directory / "fake-tool"
    script.write_text(f"#!{sys.executable}\nimport json, os, sys, time\n{body}\n")
    script.chmod(0o755 if executable else 0o644)
    return str(script)


def recorded_arguments(path: Path) -> list[str]:
    arguments = json.loads(path.read_text())
    assert isinstance(arguments, list)
    return [str(argument) for argument in arguments]


def read_if_present(path: Path) -> str:
    return path.read_text() if path.exists() else ""


async def wait_for_pid(path: Path) -> int:
    """Poll until a fake tool has written its process id to ``path``."""
    async with asyncio.timeout(10):
        while True:
            text = await asyncio.to_thread(read_if_present, path)
            if text.strip().isdigit():
                return int(text)
            await asyncio.sleep(0.02)


# The bitmap font and PNG writer


def test_png_has_the_signature_an_rgba_header_and_the_pixels() -> None:
    pixels = bytes(range(24))

    png = text_images.encode_png(3, 2, pixels)

    image = decode_png(png)
    assert (image.width, image.height) == (3, 2)
    assert b"".join(image.rows) == pixels


@pytest.mark.parametrize(("width", "height", "length"), [(2, 2, 15), (0, 1, 0), (1, 0, 0)])
def test_png_rejects_pixels_that_do_not_fill_the_image(
    width: int, height: int, length: int
) -> None:
    with pytest.raises(ValueError, match="RGBA image"):
        text_images.encode_png(width, height, bytes(length))


def test_font_covers_the_label_characters() -> None:
    assert set(FONT) <= text_images.SUPPORTED_CHARACTERS


@pytest.mark.parametrize("char", list(FONT))
def test_every_glyph_is_5_by_7_and_only_space_is_blank(char: str) -> None:
    rows = text_images.glyph(char)

    assert len(rows) == text_images.GLYPH_HEIGHT
    assert all(len(row) == text_images.GLYPH_WIDTH and set(row) <= {"#", "."} for row in rows)
    assert ("#" in "".join(rows)) is (char != " ")


def test_glyphs_are_all_different() -> None:
    bitmaps = {text_images.glyph(char) for char in FONT}

    assert len(bitmaps) == len(FONT)


def test_lowercase_letters_share_the_uppercase_glyphs() -> None:
    for upper in string.ascii_uppercase:
        assert text_images.glyph(upper.lower()) == text_images.glyph(upper)


@pytest.mark.parametrize("char", ["~", "%", "é", "ß", "\n"])
def test_characters_the_font_lacks_are_drawn_as_a_box(char: str) -> None:
    box = text_images.glyph(char)

    assert box[0] == box[-1] == "#####"
    assert all(row[0] == row[-1] == "#" for row in box)
    assert box not in {text_images.glyph(known) for known in FONT}


@pytest.mark.parametrize("text", ["", "AB"])
def test_glyph_takes_exactly_one_character(text: str) -> None:
    with pytest.raises(ValueError, match="one character"):
        text_images.glyph(text)


def test_text_size_counts_glyphs_and_the_gaps_between_them() -> None:
    assert text_images.text_size("AB", 3) == ((5 + 1 + 5) * 3, 7 * 3)
    assert text_images.text_size("", 2) == (0, 14)
    with pytest.raises(ValueError, match="scale"):
        text_images.text_size("A", 0)


def test_label_is_its_text_plus_padding_on_every_side() -> None:
    label = decode_png(text_images.render_label("RANDOM 00:42", scale=2, padding=4))

    assert (label.width, label.height) == ((12 * 6 - 1) * 2 + 8, 7 * 2 + 8)


def test_label_text_is_opaque_on_a_semi_opaque_dark_box() -> None:
    label = decode_png(text_images.render_label("I", scale=3, padding=2))

    background = text_images.LABEL_BACKGROUND
    assert background[:3] == (0, 0, 0) and 0 < background[3] < 255
    assert label.pixel(0, 0) == background
    assert label.pixel(2, 2) == background, "I's top-left font pixel is blank"
    for x, y in [(2 + 3, 2), (2 + 5, 2 + 2), (2 + 6, 2 + 3 * 6 + 2)]:
        assert label.pixel(x, y) == text_images.LABEL_TEXT


def test_label_reads_back_in_uppercase() -> None:
    label = decode_png(text_images.render_label("speedrunner 01:05 (v2) #3 a_b-c./"))

    assert label_text(label) == "SPEEDRUNNER 01:05 (V2) #3 A_B-C./"


def test_labels_of_the_same_length_have_the_same_size() -> None:
    sizes = {
        (image.width, image.height)
        for image in map(
            decode_png, map(text_images.render_label, ["NEWBIE 00:00", "NEWBIE 59:59"])
        )
    }

    assert len(sizes) == 1


def test_an_empty_label_is_just_its_padding() -> None:
    label = decode_png(text_images.render_label("", padding=3))

    assert (label.width, label.height) == (6, 14 + 6)


def test_title_card_centres_large_text_on_an_opaque_background() -> None:
    card = decode_png(text_images.render_title_card("NEWBIE", width=WIDTH, height=HEIGHT))

    assert (card.width, card.height) == (WIDTH, HEIGHT)
    pixels = {card.pixel(x, y) for y in range(HEIGHT) for x in range(WIDTH)}
    assert pixels == {text_images.CARD_BACKGROUND, text_images.CARD_TEXT}
    ink = [(x, y) for y in range(HEIGHT) for x in range(WIDTH) if card.pixel(x, y)[0] == 255]
    left, right = min(x for x, _ in ink), max(x for x, _ in ink)
    top, bottom = min(y for _, y in ink), max(y for _, y in ink)
    scale = text_images.MAX_CARD_SCALE
    assert (right - left + 1, bottom - top + 1) == ((6 * 6 - 1) * scale, 7 * scale)
    assert left == WIDTH - 1 - right and top == HEIGHT - 1 - bottom


def test_title_card_scales_long_text_down_to_fit() -> None:
    card = decode_png(text_images.render_title_card("SPEEDRUNNER", width=WIDTH, height=HEIGHT))

    columns = [x for x in range(WIDTH) if any(card.pixel(x, y)[0] == 255 for y in range(HEIGHT))]
    width = columns[-1] - columns[0] + 1
    assert width <= WIDTH * text_images.CARD_FILL
    assert width == (11 * 6 - 1) * 7, "the largest scale that fits"


def test_title_card_centres_each_line_and_honours_an_explicit_scale() -> None:
    card = decode_png(text_images.render_title_card("I\nIII", width=60, height=40, scale=2))

    def inked(y: int) -> list[int]:
        return [x for x in range(60) if card.pixel(x, y)[0] == 255]

    rows = [y for y in range(40) if inked(y)]
    assert (rows[0], rows[-1]) == (5, 34), "two lines of 14 pixels, 2 apart, centred in 40"
    for y in (rows[0], rows[-1]):
        assert inked(y)[0] == 59 - inked(y)[-1]


def test_text_too_long_for_a_title_card_is_clipped() -> None:
    card = decode_png(text_images.render_title_card("W" * 200, width=100, height=20))

    assert (card.width, card.height) == (100, 20)
    assert any(card.pixel(x, 10)[0] == 255 for x in range(100)), "the middle still shows"


def test_canvas_clips_drawing_to_its_edges() -> None:
    canvas = text_images.Canvas(4, 3, (0, 0, 0, 0))

    canvas.fill_rect(-2, 2, 10, 5, (9, 9, 9, 255))

    assert [canvas.pixel(x, 2) for x in range(4)] == [(9, 9, 9, 255)] * 4
    assert canvas.pixel(0, 1) == (0, 0, 0, 0)
    with pytest.raises(IndexError):
        canvas.pixel(4, 0)
    with pytest.raises(ValueError, match="at least 1x1"):
        text_images.Canvas(0, 3, (0, 0, 0, 0))


# The ffmpeg wrapper, with stand-in executables


async def test_run_hands_ffmpeg_its_arguments_verbatim(tmp_path: Path) -> None:
    argv = tmp_path / "argv.json"
    tool = fake_tool(tmp_path, f"open({str(argv)!r}, 'w').write(json.dumps(sys.argv[1:]))")
    tricky = "name with spaces; $HOME | *.mp4"

    await FFmpeg(ffmpeg=tool).run("-i", tmp_path / "in.webm", tricky)

    assert recorded_arguments(argv) == [
        *("-y", "-hide_banner", "-loglevel", "error"),
        *("-i", str(tmp_path / "in.webm"), tricky),
    ]


async def test_a_failed_run_quotes_the_end_of_stderr(tmp_path: Path) -> None:
    tool = fake_tool(
        tmp_path, "for i in range(30): print(f'line {i}', file=sys.stderr)\nsys.exit(3)"
    )

    with pytest.raises(MediaError) as failure:
        await FFmpeg(ffmpeg=tool).run("-version")

    header, *quoted = str(failure.value).splitlines()
    assert header == f"{tool} exited with status 3:"
    assert quoted == [f"line {i}" for i in range(10, 30)]


async def test_a_failed_run_without_stderr_still_explains_itself(tmp_path: Path) -> None:
    tool = fake_tool(tmp_path, "sys.exit(1)")

    with pytest.raises(MediaError, match=r"exited with status 1$"):
        await FFmpeg(ffmpeg=tool).run()


async def test_a_missing_tool_is_a_media_error(tmp_path: Path) -> None:
    missing = str(tmp_path / "no-such-tool")

    with pytest.raises(MediaError, match="was not found"):
        await FFmpeg(ffmpeg=missing).run("-version")
    with pytest.raises(MediaError, match="was not found"):
        await FFmpeg(ffprobe=missing).duration_s(tmp_path / "clip.webm")


async def test_a_tool_that_cannot_start_is_a_media_error(tmp_path: Path) -> None:
    tool = fake_tool(tmp_path, "sys.exit(0)", executable=False)

    with pytest.raises(MediaError, match="could not start"):
        await FFmpeg(ffmpeg=tool).run()


async def test_cancelling_a_run_kills_ffmpeg(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    tool = fake_tool(
        tmp_path, f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)"
    )
    task = asyncio.create_task(FFmpeg(ffmpeg=tool).run())
    pid = await wait_for_pid(pid_file)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_duration_is_the_format_duration_ffprobe_prints(tmp_path: Path) -> None:
    argv = tmp_path / "argv.json"
    write_argv = f"open({str(argv)!r}, 'w').write(json.dumps(sys.argv[1:]))"
    tool = fake_tool(tmp_path, f"{write_argv}\nprint('12.345000')")
    clip = tmp_path / "clip.webm"

    assert await FFmpeg(ffprobe=tool).duration_s(clip) == 12.345
    assert recorded_arguments(argv) == [
        *("-v", "error", "-show_entries", "format=duration"),
        *("-of", "csv=p=0", str(clip)),
    ]


@pytest.mark.parametrize("reported", ["N/A", "", "nan", "inf", "-1.5"])
async def test_a_missing_or_invalid_duration_is_a_media_error(
    tmp_path: Path, reported: str
) -> None:
    tool = fake_tool(tmp_path, f"print({reported!r})")

    with pytest.raises(MediaError, match="duration"):
        await FFmpeg(ffprobe=tool).duration_s(tmp_path / "clip.webm")


async def test_a_failed_probe_is_a_media_error(tmp_path: Path) -> None:
    tool = fake_tool(tmp_path, "print('clip.webm: Invalid data', file=sys.stderr)\nsys.exit(1)")

    with pytest.raises(MediaError, match="Invalid data"):
        await FFmpeg(ffprobe=tool).duration_s(tmp_path / "clip.webm")


# The preparer, with a scripted FFmpeg


async def test_each_second_of_a_recording_gets_its_own_clock_label(tmp_path: Path) -> None:
    ffmpeg = ScriptedFFmpeg(duration_s=2.5)
    paths = placeholder_version(tmp_path, BotName.SPEEDRUNNER)

    await MediaPreparer(ffmpeg).prepare(paths, [BotName.SPEEDRUNNER])

    (clock,) = ffmpeg.clocks
    assert [label_text(label) for label in clock] == [
        "SPEEDRUNNER 00:00",
        "SPEEDRUNNER 00:01",
        "SPEEDRUNNER 00:02",
    ]
    assert len({(label.width, label.height) for label in clock}) == 1


@pytest.mark.parametrize(("duration_s", "labels", "last"), [(0.0, 1, "00:00"), (61.2, 62, "01:01")])
async def test_the_clock_covers_the_whole_recording(
    tmp_path: Path, duration_s: float, labels: int, last: str
) -> None:
    ffmpeg = ScriptedFFmpeg(duration_s=duration_s)

    await MediaPreparer(ffmpeg).prepare(
        placeholder_version(tmp_path, BotName.RANDOM), [BotName.RANDOM]
    )

    (clock,) = ffmpeg.clocks
    assert len(clock) == labels
    assert label_text(clock[-1]) == f"RANDOM {last}"


async def test_combined_clip_puts_a_title_card_before_each_clip_in_bot_order(
    tmp_path: Path,
) -> None:
    bots = [BotName.NEWBIE, BotName.RANDOM]
    ffmpeg = ScriptedFFmpeg()
    paths = placeholder_version(tmp_path, *BotName)

    media = await MediaPreparer(ffmpeg).prepare(paths, bots)

    clips = {bot: paths.prepared_video(bot) for bot in bots}
    assert media == PreparedMedia(combined=paths.combined_video, per_bot=clips)
    assert list(media.per_bot) == bots
    card_for = {
        bot: card
        for card, image in ffmpeg.cards.items()
        for bot in bots
        if image == text_images.render_title_card(bot.value.upper(), width=WIDTH, height=HEIGHT)
    }
    assert inputs_of(ffmpeg.commands[-1]) == [
        card_for[BotName.NEWBIE],
        clips[BotName.NEWBIE],
        card_for[BotName.RANDOM],
        clips[BotName.RANDOM],
    ]


async def test_cards_clips_and_the_join_share_one_encoding(tmp_path: Path) -> None:
    ffmpeg = ScriptedFFmpeg()

    await MediaPreparer(ffmpeg).prepare(placeholder_version(tmp_path, *BotName), list(BotName))

    (encoding,) = {tuple(command[command.index("-c:v") : -1]) for command in ffmpeg.commands}
    assert len(ffmpeg.commands) == 2 * len(BotName) + 1
    expected_options = {("-c:v", "libx264"), ("-pix_fmt", "yuv420p"), ("-r", "10")}
    assert expected_options <= set(itertools.pairwise(encoding))


async def test_bots_without_a_recording_are_left_out_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    ffmpeg = ScriptedFFmpeg()
    paths = placeholder_version(tmp_path, BotName.SPEEDRUNNER)

    media = await MediaPreparer(ffmpeg).prepare(paths, list(BotName))

    assert media.per_bot == {BotName.SPEEDRUNNER: paths.prepared_video(BotName.SPEEDRUNNER)}
    assert len(inputs_of(ffmpeg.commands[-1])) == 2
    warnings = [r.getMessage() for r in caplog.records if r.name == "iterplay.media.prep"]
    assert len(warnings) == 2
    assert "random" in warnings[0] and "no recording" in warnings[0]
    assert "newbie" in warnings[1]


async def test_without_any_recording_prepare_raises_media_error(tmp_path: Path) -> None:
    ffmpeg = ScriptedFFmpeg()

    with pytest.raises(MediaError, match="no clip could be prepared"):
        await MediaPreparer(ffmpeg).prepare(VersionPaths(tmp_path / "v1"), list(BotName))
    with pytest.raises(MediaError, match="no bots were given"):
        await MediaPreparer(ffmpeg).prepare(VersionPaths(tmp_path / "v1"), [])
    assert ffmpeg.commands == []


async def test_a_clip_that_fails_does_not_cost_the_others(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    ffmpeg = ScriptedFFmpeg(fails=lambda command: any("speedrunner.webm" in arg for arg in command))
    paths = placeholder_version(tmp_path, *BotName)

    media = await MediaPreparer(ffmpeg).prepare(paths, list(BotName))

    assert list(media.per_bot) == [BotName.RANDOM, BotName.NEWBIE]
    assert "speedrunner" in caplog.text and "scripted ffmpeg failure" in caplog.text
    assert file_names(paths.playtest_dir) == {
        *("random.webm", "speedrunner.webm", "newbie.webm"),
        *("random_360p.mp4", "newbie_360p.mp4", "combined_360p.mp4"),
    }


async def test_a_failed_encode_leaves_nothing_under_the_final_name(tmp_path: Path) -> None:
    ffmpeg = ScriptedFFmpeg(fails=lambda command: True)
    paths = placeholder_version(tmp_path, BotName.RANDOM)

    with pytest.raises(MediaError, match="scripted ffmpeg failure"):
        await MediaPreparer(ffmpeg).prepare(paths, [BotName.RANDOM])

    assert file_names(paths.playtest_dir) == {"random.webm"}


async def test_a_failed_join_raises_and_leaves_no_combined_clip(tmp_path: Path) -> None:
    ffmpeg = ScriptedFFmpeg(fails=lambda command: any("concat=" in arg for arg in command))
    paths = placeholder_version(tmp_path, BotName.RANDOM)

    with pytest.raises(MediaError, match="scripted ffmpeg failure"):
        await MediaPreparer(ffmpeg).prepare(paths, [BotName.RANDOM])

    assert file_names(paths.playtest_dir) == {"random.webm", "random_360p.mp4"}


async def test_a_failed_title_card_raises_and_leaves_no_combined_clip(tmp_path: Path) -> None:
    ffmpeg = ScriptedFFmpeg(fails=lambda command: "-loop" in command)
    paths = placeholder_version(tmp_path, BotName.RANDOM, BotName.NEWBIE)

    with pytest.raises(MediaError, match="scripted ffmpeg failure"):
        await MediaPreparer(ffmpeg).prepare(paths, list(BotName))

    assert "combined_360p.mp4" not in file_names(paths.playtest_dir)
    assert len(ffmpeg.commands) == 2 + 1, "both clips, then the first card, and no join"


async def test_cancelling_prepare_leaves_no_partial_clip(tmp_path: Path) -> None:
    ffmpeg = HangingFFmpeg()
    paths = placeholder_version(tmp_path, BotName.RANDOM)
    task = asyncio.create_task(MediaPreparer(ffmpeg).prepare(paths, [BotName.RANDOM]))
    await ffmpeg.started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert file_names(paths.playtest_dir) == {"random.webm"}


async def test_title_cards_can_be_left_out(tmp_path: Path) -> None:
    ffmpeg = ScriptedFFmpeg()
    paths = placeholder_version(tmp_path, *BotName)

    await MediaPreparer(ffmpeg, MediaPrepConfig(title_card_s=0)).prepare(paths, list(BotName))

    assert ffmpeg.cards == {}
    assert inputs_of(ffmpeg.commands[-1]) == [paths.prepared_video(bot) for bot in BotName]


async def test_a_bot_named_twice_is_prepared_once(tmp_path: Path) -> None:
    ffmpeg = ScriptedFFmpeg()
    paths = placeholder_version(tmp_path, BotName.RANDOM)

    media = await MediaPreparer(ffmpeg).prepare(paths, [BotName.RANDOM, BotName.RANDOM])

    assert list(media.per_bot) == [BotName.RANDOM]
    assert len(ffmpeg.encodes()) == 2, "one clip and the combined clip"


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"width": 641}, "frame size"),
        ({"height": 0}, "frame size"),
        ({"fps": 0}, "fps"),
        ({"title_card_s": -1.0}, "title_card_s"),
        ({"title_card_s": math.nan}, "title_card_s"),
    ],
)
def test_config_rejects_formats_the_clips_cannot_use(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        MediaPrepConfig(**fields)


# The real tools


@pytest.fixture(scope="module")
def recordings(tmp_path_factory: pytest.TempPathFactory) -> dict[BotName, Path]:
    """One synthetic recording per bot, each of a different length."""
    directory = tmp_path_factory.mktemp("recordings")
    return {
        bot: record_test_pattern(
            directory / f"{bot}.webm", seconds, codec=RECORDING_CODECS.get(bot, "libvpx")
        )
        for bot, seconds in RECORDING_S.items()
    }


@pytest.fixture(scope="module")
def prepared(
    tmp_path_factory: pytest.TempPathFactory, recordings: dict[BotName, Path]
) -> PreparedMedia:
    """Every bot's recording prepared once, for the tests that only inspect the results."""
    paths = install(VersionPaths(tmp_path_factory.mktemp("run") / "v1"), recordings)
    return asyncio.run(MediaPreparer(FFmpeg()).prepare(paths, list(BotName)))


@pytest.mark.ffmpeg
def test_each_clip_is_640x360_h264_at_10_fps_and_as_long_as_its_recording(
    prepared: PreparedMedia,
) -> None:
    for bot, seconds in RECORDING_S.items():
        info = probe(prepared.per_bot[bot])

        assert (info.codec, info.pixel_format) == ("h264", "yuv420p")
        assert (info.width, info.height, info.fps) == (WIDTH, HEIGHT, 10)
        assert info.frames == round(10 * seconds)
        assert info.duration_s == pytest.approx(seconds, abs=0.11)


@pytest.mark.ffmpeg
def test_combined_clip_is_every_title_card_and_clip_back_to_back(prepared: PreparedMedia) -> None:
    info = probe(prepared.combined)

    expected_s = len(RECORDING_S) * CARD_S + sum(RECORDING_S.values())
    assert (info.codec, info.width, info.height, info.fps) == ("h264", WIDTH, HEIGHT, 10)
    assert info.duration_s == pytest.approx(expected_s, abs=0.15)
    assert info.frames == round(10 * expected_s)


@pytest.mark.ffmpeg
@pytest.mark.parametrize(("at_s", "label"), [(0.5, "00:00"), (1.5, "00:01"), (2.5, "00:02")])
def test_clock_shows_seconds_since_the_recording_started(
    prepared: PreparedMedia, at_s: float, label: str
) -> None:
    assert label_at(prepared.per_bot[BotName.RANDOM], at_s, 12) == f"RANDOM {label}"


@pytest.mark.ffmpeg
def test_label_is_burned_over_the_downscaled_recording(
    prepared: PreparedMedia, recordings: dict[BotName, Path]
) -> None:
    source = grab_frame(recordings[BotName.SPEEDRUNNER], 1.5)
    output = grab_frame(prepared.per_bot[BotName.SPEEDRUNNER], 1.5)
    label = decode_png(text_images.render_label("SPEEDRUNNER 00:01", scale=LABEL_SCALE))
    box = (LABEL_OFFSET, LABEL_OFFSET, label.width, label.height)

    burned, untouched = output.crop(*box), source.crop(*box)

    assert mean_difference(burned, untouched) > 40
    assert mean_difference(burned, composite(label, untouched)) < 8
    below = (0, 60, WIDTH, 200)
    assert mean_difference(output.crop(*below), source.crop(*below)) < 6


@pytest.mark.ffmpeg
def test_a_title_card_introduces_each_run_in_the_combined_clip(prepared: PreparedMedia) -> None:
    start_s = 0.0
    for bot, seconds in RECORDING_S.items():
        card = grab_frame(prepared.combined, start_s + CARD_S / 2)
        name = bot.value.upper()

        assert nearest_title_card(card) is bot
        assert mean_difference(card.rgb, title_card(bot)) < 4
        assert label_at(prepared.combined, start_s + CARD_S + 0.5, len(name) + 6) == f"{name} 00:00"
        start_s += CARD_S + seconds


@pytest.mark.ffmpeg
async def test_side_by_side_shows_both_clips_until_the_shorter_one_ends(
    recordings: dict[BotName, Path], tmp_path: Path
) -> None:
    output = tmp_path / "compare" / "v1_v3.mp4"

    await MediaPreparer(FFmpeg()).side_by_side(
        recordings[BotName.RANDOM], recordings[BotName.NEWBIE], output, labels=("v1", "v3")
    )

    info = probe(output)
    assert (info.codec, info.width, info.height, info.fps) == ("h264", 2 * WIDTH, HEIGHT, 10)
    assert info.duration_s == pytest.approx(RECORDING_S[BotName.NEWBIE], abs=0.11)
    frame = grab_frame(output, 1.0, size=(2 * WIDTH, HEIGHT))
    label_height = 7 * LABEL_SCALE + 2 * text_images.LABEL_PADDING
    top = HEIGHT - LABEL_OFFSET - label_height + text_images.LABEL_PADDING
    assert read_text(frame.is_ink, TEXT_ORIGIN, top, 2) == "V1"
    assert read_text(frame.is_ink, WIDTH + TEXT_ORIGIN, top, 2) == "V3"


@pytest.mark.ffmpeg
async def test_side_by_side_writes_mp4_whatever_the_output_is_called(
    recordings: dict[BotName, Path], tmp_path: Path
) -> None:
    output = tmp_path / "v1-against-v3"
    newbie = recordings[BotName.NEWBIE]

    await MediaPreparer(FFmpeg()).side_by_side(newbie, newbie, output, labels=("v1", "v3"))

    container = tool_output(
        *("ffprobe", "-v", "error", "-show_entries", "format=format_name"),
        *("-of", "csv=p=0", str(output)),
    )
    assert b"mp4" in container
    assert file_names(tmp_path) == {"v1-against-v3"}


@pytest.mark.ffmpeg
async def test_side_by_side_of_a_missing_clip_raises_media_error(
    recordings: dict[BotName, Path], tmp_path: Path
) -> None:
    output = tmp_path / "compare.mp4"

    with pytest.raises(MediaError, match="No such file or directory"):
        await MediaPreparer(FFmpeg()).side_by_side(
            tmp_path / "missing.mp4", recordings[BotName.RANDOM], output, labels=("a", "b")
        )

    assert file_names(tmp_path) == set()


@pytest.mark.ffmpeg
async def test_a_run_missing_recordings_still_gets_a_combined_clip(
    recordings: dict[BotName, Path], tmp_path: Path
) -> None:
    paths = install(VersionPaths(tmp_path / "v2"), {BotName.NEWBIE: recordings[BotName.NEWBIE]})

    media = await MediaPreparer(FFmpeg()).prepare(paths, list(BotName))

    assert list(media.per_bot) == [BotName.NEWBIE]
    expected_s = CARD_S + RECORDING_S[BotName.NEWBIE]
    assert probe(media.combined).duration_s == pytest.approx(expected_s, abs=0.15)


@pytest.mark.ffmpeg
async def test_a_recording_of_another_shape_is_letterboxed_not_stretched(tmp_path: Path) -> None:
    recording = record_test_pattern(tmp_path / "4x3.webm", 1.0, size="800x600")
    paths = install(VersionPaths(tmp_path / "v1"), {BotName.RANDOM: recording})

    media = await MediaPreparer(FFmpeg()).prepare(paths, [BotName.RANDOM])

    clip = media.per_bot[BotName.RANDOM]
    info = probe(clip)
    assert (info.width, info.height) == (WIDTH, HEIGHT)
    frame = grab_frame(clip, 0.5)
    bars = frame.crop(0, 40, 64, HEIGHT - 40) + frame.crop(WIDTH - 64, 40, 64, HEIGHT - 40)
    assert max(bars) < 40, "the 480x360 picture sits between black bars"
    assert mean_difference(frame.crop(80, 40, 480, HEIGHT - 40), bytes(3 * 480 * 320)) > 60
    assert label_at(clip, 0.5, 12) == "RANDOM 00:00"


@pytest.mark.ffmpeg
async def test_a_recording_that_is_not_a_video_raises_media_error(tmp_path: Path) -> None:
    paths = placeholder_version(tmp_path, BotName.RANDOM)

    with pytest.raises(MediaError, match=r"(?s)no clip could be prepared.*random: ffprobe"):
        await MediaPreparer(FFmpeg()).prepare(paths, [BotName.RANDOM])

    assert file_names(paths.playtest_dir) == {"random.webm"}


@pytest.mark.ffmpeg
async def test_clock_survives_a_percent_sign_in_the_temporary_directory(
    recordings: dict[BotName, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "100% scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    paths = install(VersionPaths(tmp_path / "v1"), {BotName.RANDOM: recordings[BotName.RANDOM]})

    media = await MediaPreparer(FFmpeg()).prepare(paths, [BotName.RANDOM])

    assert label_at(media.per_bot[BotName.RANDOM], 2.5, 12) == "RANDOM 00:02"


@pytest.mark.ffmpeg
async def test_duration_comes_from_ffprobe(recordings: dict[BotName, Path]) -> None:
    duration_s = await FFmpeg().duration_s(recordings[BotName.SPEEDRUNNER])

    assert duration_s == pytest.approx(RECORDING_S[BotName.SPEEDRUNNER], abs=0.05)


@pytest.mark.ffmpeg
async def test_ffmpeg_failures_carry_its_error_output(tmp_path: Path) -> None:
    with pytest.raises(MediaError, match="No such file or directory"):
        await FFmpeg().run("-i", tmp_path / "missing.webm", tmp_path / "out.mp4")


@pytest.mark.ffmpeg
async def test_a_file_that_is_not_media_has_no_duration(tmp_path: Path) -> None:
    paths = placeholder_version(tmp_path, BotName.RANDOM)

    with pytest.raises(MediaError, match="ffprobe exited with status"):
        await FFmpeg().duration_s(paths.raw_video(BotName.RANDOM))
