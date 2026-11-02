"""Image -> ASCII conversion engine.

This module has NO GUI dependencies (only Pillow and NumPy), so it can be
imported, tested and scripted on its own.

Pipeline
--------
    load_image()  ->  convert()  ->  render_image() / to_text()  ->  export()

* ``load_image``   reads a file, fixes EXIF rotation, flattens transparency.
* ``convert``      turns a picture into a grid of characters (``AsciiResult``).
* ``render_image`` draws that grid onto a pixel canvas (PNG/JPEG output).
* ``export``       writes PNG, JPEG or TXT to disk.
"""

from __future__ import annotations

import functools
import os
import subprocess
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

# --------------------------------------------------------------------------
# Limits and defaults
# --------------------------------------------------------------------------
MIN_COLS = 4
MAX_COLS = 800
MAX_CELLS = 600_000             # protects against absurdly large grids
MAX_SOURCE_SIDE = 4096          # larger sources are downsized on load
MAX_OUTPUT_PIXELS = 50_000_000  # largest PNG/JPEG canvas we agree to draw
MAX_CHARSET_LEN = 256
DEFAULT_FONT_SIZE = 12
_NOISE_SEED = 7                 # fixed seed -> gradient thinning is reproducible

# Character presets, ordered DARKEST -> LIGHTEST (last symbol is usually a space).
PRESETS = {
    "Standard ramp (long)": "$@B%8&WM#*oahkbdpqwmZO0QLCJUYXzcvunxrjft/\\|()1{}[]?-_+~<>i!lI;:,\"^`'. ",
    "Classic short (10)": "@%#*+=-:. ",
    "Blocks (short)": "█▇▅▃▁ ",
    "Unicode shades": "█▓▒░ ",
    "Minimal two-tone": "# ",
}
DEFAULT_PRESET = "Standard ramp (long)"


# --------------------------------------------------------------------------
# Errors (every message is written for the end user)
# --------------------------------------------------------------------------
class AsciiError(Exception):
    """Base class for all user-facing errors."""


class ImageLoadError(AsciiError):
    pass


class CharsetError(AsciiError):
    pass


class ConversionError(AsciiError):
    pass


class ExportError(AsciiError):
    pass


# --------------------------------------------------------------------------
# Settings / result types
# --------------------------------------------------------------------------
class SizeMode(str, Enum):
    ORIGINAL = "original"
    UPSCALE = "upscale"
    DOWNSCALE = "downscale"
    NEW = "new"


class EdgeMode(str, Enum):
    NONE = "none"
    SHARP = "sharp"
    GRADIENT = "gradient"


class OutputFormat(str, Enum):
    PNG = "png"
    JPEG = "jpeg"
    TXT = "txt"

    @property
    def extensions(self) -> Tuple[str, ...]:
        return {"png": (".png",), "jpeg": (".jpg", ".jpeg"), "txt": (".txt",)}[self.value]

    @property
    def default_extension(self) -> str:
        return self.extensions[0]

    @property
    def file_filter(self) -> str:
        return {
            "png": "PNG image (*.png)",
            "jpeg": "JPEG image (*.jpg *.jpeg)",
            "txt": "Text file (*.txt)",
        }[self.value]


@dataclass
class ConversionSettings:
    charset: str = PRESETS[DEFAULT_PRESET]
    size_mode: SizeMode = SizeMode.ORIGINAL
    columns: int = 100            # grid width for ORIGINAL (and base for UP/DOWN)
    scale: float = 1.0            # multiplier on `columns` for UPSCALE / DOWNSCALE
    detail_px: int = 8            # NEW mode: source pixels covered by one character
    cell_aspect: Optional[float] = None   # char width / char height; None = from font
    edge_mode: EdgeMode = EdgeMode.NONE
    sharp_width: int = 1          # SHARP: border thickness, in character rows
    fade_percent: float = 15.0    # GRADIENT: fade width, % of the shorter image side
    invert: bool = False
    auto_contrast: bool = True
    line_art: bool = False        # hard black/white threshold (outline drawings)
    line_threshold: int = 128

    def validate(self) -> None:
        def bad(msg: str) -> None:
            raise ConversionError(msg)

        if not 1 <= self.columns <= 100_000:
            bad("Columns must be a positive number.")
        if not self.scale > 0:
            bad("The scale factor must be greater than zero.")
        if self.detail_px < 1:
            bad("'Source pixels per character' must be at least 1.")
        if self.cell_aspect is not None and not 0.1 <= self.cell_aspect <= 2.0:
            bad("Character aspect ratio must be between 0.1 and 2.0.")
        if not 1 <= self.sharp_width <= 50:
            bad("Sharp-edge thickness must be between 1 and 50.")
        if not 0 <= self.fade_percent <= 100:
            bad("Gradient width must be between 0 and 100 %.")
        if not 0 <= self.line_threshold <= 255:
            bad("The line-art threshold must be between 0 and 255.")


@dataclass(frozen=True)
class AsciiResult:
    lines: Tuple[str, ...]
    cols: int
    rows: int
    cell_aspect: float   # width / height of one character cell used for the grid

    def to_text(self, strip: bool = True) -> str:
        """Plain text, one grid row per line (optionally without trailing blanks)."""
        rows = (ln.rstrip() for ln in self.lines) if strip else self.lines
        return "\n".join(rows) + "\n"


# --------------------------------------------------------------------------
# Character set handling
# --------------------------------------------------------------------------
def parse_charset(text: str) -> str:
    """Validate a symbol set typed by the user (darkest -> lightest).

    Any length >= 1 is accepted, including a single symbol. Spaces are allowed and
    meaningful (a trailing space makes the lightest tone empty). Line breaks are
    dropped so a pasted multi-line value still works.
    """
    if text is None:
        raise CharsetError("The symbol set is empty. Enter at least one symbol.")
    cleaned = text.replace("\r", "").replace("\n", "").replace("\t", " ")
    if not cleaned:
        raise CharsetError("The symbol set is empty. Enter at least one symbol.")
    if len(cleaned) > MAX_CHARSET_LEN:
        raise CharsetError(f"The symbol set is too long ({len(cleaned)}); the maximum is {MAX_CHARSET_LEN}.")
    for ch in cleaned:
        category = unicodedata.category(ch)
        if category[0] == "C":
            raise CharsetError(f"U+{ord(ch):04X} is a control or invisible character and can't be used.")
        if category in ("Mn", "Me"):
            raise CharsetError(f"U+{ord(ch):04X} is a combining mark; use standalone symbols only.")
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            raise CharsetError(
                f"'{ch}' is a double-width character and would break the grid alignment. "
                "Please use single-width symbols."
            )
    if all(ch.isspace() for ch in cleaned):
        raise CharsetError("The symbol set contains only spaces, so nothing would be visible.")
    return cleaned


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def load_image(path: str) -> Image.Image:
    """Open any Pillow-supported file and return an RGB image ready for conversion."""
    try:
        with Image.open(path) as im:
            im.load()
            img = ImageOps.exif_transpose(im)   # honour camera rotation
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                # Transparent areas become white so they turn into empty space
                # (important for line-art PNGs with a transparent background).
                rgba = img.convert("RGBA")
                backdrop = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                img = Image.alpha_composite(backdrop, rgba)
            img = img.convert("RGB")
    except FileNotFoundError:
        raise ImageLoadError(f"File not found:\n{path}") from None
    except PermissionError:
        raise ImageLoadError(f"No permission to read:\n{path}") from None
    except Image.DecompressionBombError:
        raise ImageLoadError("The image is far too large to open safely.") from None
    except (OSError, ValueError, SyntaxError, EOFError) as exc:
        raise ImageLoadError(
            "This file couldn't be read as an image. It may be corrupted or in an "
            f"unsupported format.\n\nDetails: {exc}"
        ) from exc
    if img.width < 1 or img.height < 1:
        raise ImageLoadError("The image has no pixels.")
    if max(img.size) > MAX_SOURCE_SIDE:
        img.thumbnail((MAX_SOURCE_SIDE, MAX_SOURCE_SIDE), Image.Resampling.LANCZOS)
    return img


# --------------------------------------------------------------------------
# Fonts and character-cell geometry
# --------------------------------------------------------------------------
_FONT_CANDIDATES = (
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",               # Arch Linux (ttf-dejavu)
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",   # Debian/Ubuntu
    "/usr/share/fonts/noto/NotoSansMono-Regular.ttf",
    "/usr/share/fonts/liberation/LiberationMono-Regular.ttf",
)


@functools.lru_cache(maxsize=1)
def find_font_path() -> Optional[str]:
    """DejaVu Sans Mono if present (it covers block/shade glyphs), else fontconfig's monospace."""
    for candidate in _FONT_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    try:
        out = subprocess.run(
            ["fc-match", "-f", "%{file}", "monospace"],
            capture_output=True, text=True, timeout=3, check=False,
        ).stdout.strip()
        if out and os.path.isfile(out):
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _open_font(size: int):
    # A fresh font object per call: FreeType faces are not thread-safe to share.
    path = find_font_path()
    try:
        font = ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
        font.getmetrics()  # raises AttributeError for Pillow's bitmap fallback font
        return font
    except (OSError, TypeError, AttributeError) as exc:
        raise ExportError(
            "No usable monospace font was found. On Arch Linux install one with:\n"
            "    sudo pacman -S ttf-dejavu"
        ) from exc


def _cell_metrics(font) -> Tuple[int, int, float]:
    ascent, descent = font.getmetrics()
    return ascent, ascent + descent, font.getlength("M")


@functools.lru_cache(maxsize=64)
def detect_cell_aspect(font_size: int = DEFAULT_FONT_SIZE) -> float:
    """Width/height of one character cell of the output font (about 0.5 for DejaVu Mono)."""
    _, height, advance = _cell_metrics(_open_font(font_size))
    return advance / height


# --------------------------------------------------------------------------
# Grid geometry and edge masks
# --------------------------------------------------------------------------
def compute_grid(src_size: Tuple[int, int], s: ConversionSettings, aspect: float) -> Tuple[int, int]:
    """Return (cols, rows) for the character grid.

    ASPECT-RATIO CORRECTION
    A character cell is taller than it is wide (width/height = `aspect`, ~0.5).
    If we used one character per pixel-shaped square, the output would look
    stretched vertically by 1/aspect. The picture's physical ratio is
        height/width = (rows * cell_h) / (cols * cell_w) = rows / (cols * aspect)
    so   rows = cols * aspect * (img_h / img_w).
    """
    w, h = src_size
    if s.size_mode is SizeMode.NEW:
        # Recomputed from the SOURCE pixels: one character per `detail_px` pixels.
        cols = round(w / s.detail_px)
    elif s.size_mode in (SizeMode.UPSCALE, SizeMode.DOWNSCALE):
        cols = round(s.columns * s.scale)
    else:
        cols = s.columns
    cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
    rows = max(1, round(cols * aspect * h / w))
    if cols * rows > MAX_CELLS:
        raise ConversionError(
            f"The grid would be {cols} × {rows} characters, which is too large. "
            "Reduce the number of columns."
        )
    return cols, rows


def _edge_index_maps(rows: int, cols: int):
    """Distance (in whole cells) of every cell from the nearest vertical / horizontal border."""
    y = np.arange(rows)[:, None]
    x = np.arange(cols)[None, :]
    return np.minimum(x, cols - 1 - x), np.minimum(y, rows - 1 - y)  # (dx, dy)


def edge_density(rows: int, cols: int, aspect: float, fade_percent: float) -> np.ndarray:
    """Per-cell density factor in [0, 1]: 0 on the outermost ring, 1 once inside the fade zone.

    GRADIENT EDGE LOGIC
    1. Measure each cell's distance to the nearest border in *physical* units
       (multiples of a cell height). A column is only `aspect` as wide as a row is
       tall, so horizontal distances are multiplied by `aspect`; otherwise the fade
       would look twice as wide on the sides as at the top and bottom.
    2. Divide by the fade width (a percentage of the shorter image side) and clamp
       to [0, 1]  ->  a linear ramp.
    3. Apply smoothstep (3t^2 - 2t^3) so the fade eases in and out: continuous,
       with no visible "kink" where the fade starts or ends.
    """
    dx, dy = _edge_index_maps(rows, cols)
    dist = np.minimum(dx * aspect, dy)               # broadcasts to (rows, cols)
    shorter_side = min(cols * aspect, float(rows))
    fade = max(fade_percent / 100.0 * shorter_side, 1e-6)
    t = np.clip(dist / fade, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------
def _sample_luminance(gray: Image.Image, cols: int, rows: int) -> np.ndarray:
    # BOX averages every source pixel under a cell (best when shrinking);
    # BICUBIC interpolates smoothly when the grid is finer than the source.
    shrinking = cols <= gray.width and rows <= gray.height
    method = Image.Resampling.BOX if shrinking else Image.Resampling.BICUBIC
    small = gray.resize((cols, rows), method)
    return np.asarray(small, dtype=np.float32) / 255.0


def _apply_tone(lum: np.ndarray, s: ConversionSettings) -> np.ndarray:
    if s.line_art:
        lum = np.where(lum * 255.0 < s.line_threshold, 0.0, 1.0).astype(np.float32)
    elif s.auto_contrast:
        lo, hi = np.percentile(lum, (1, 99))
        if hi - lo > 1e-3:                            # skip flat images
            lum = np.clip((lum - lo) / (hi - lo), 0.0, 1.0)
    return 1.0 - lum if s.invert else lum


def _map_to_chars(lum: np.ndarray, charset: str) -> np.ndarray:
    n = len(charset)
    if n == 1:
        # One symbol can't express tone: draw it where the picture is dark
        # (a silhouette) and leave the rest blank.
        return np.where(lum < 0.5, charset, " ")
    index = np.minimum((lum * n).astype(np.int32), n - 1)   # 0 = darkest symbol
    return np.array(list(charset))[index]


def convert(image: Image.Image, settings: ConversionSettings,
            font_size: int = DEFAULT_FONT_SIZE) -> AsciiResult:
    """Convert an RGB image into an ASCII grid according to `settings`.

    `font_size` only matters when `settings.cell_aspect` is None (auto): the
    aspect is then measured from the font that PNG/JPEG output will use.
    """
    charset = parse_charset(settings.charset)
    settings.validate()
    aspect = settings.cell_aspect or detect_cell_aspect(font_size)

    gray = image.convert("L")
    cols, rows = compute_grid(gray.size, settings, aspect)

    if settings.size_mode is SizeMode.NEW:
        # "Generate new": the layout is derived afresh from the source pixels,
        # with histogram equalisation so the whole tonal range of the character
        # set is used. It never rescales an earlier ASCII result.
        gray = ImageOps.equalize(gray)

    lum = _apply_tone(_sample_luminance(gray, cols, rows), settings)

    keep = None
    if settings.edge_mode is EdgeMode.GRADIENT:
        density = edge_density(rows, cols, aspect, settings.fade_percent)
        # Two cooperating effects, both driven by the same density value:
        #  (a) tone fades toward the lightest symbol -> glyphs get thinner/lighter;
        #  (b) random thinning removes cells with probability (1 - density) -> glyphs
        #      appear less often. The RNG seed is fixed so previews match exports.
        lum = 1.0 - (1.0 - lum) * density
        keep = np.random.default_rng(_NOISE_SEED).random((rows, cols)) < density

    grid = _map_to_chars(lum, charset)
    if keep is not None:
        grid[~keep] = " "     # blank (not the lightest symbol) so cells really vanish

    if settings.edge_mode is EdgeMode.SHARP:
        # Solid frame of the densest visible symbol: a hard cut-off at the border.
        # Thickness is given in rows; horizontally it is widened by 1/aspect so the
        # frame looks equally thick on all four sides.
        dx, dy = _edge_index_maps(rows, cols)
        wy = settings.sharp_width
        wx = max(1, round(wy / aspect))
        solid = next(ch for ch in charset if not ch.isspace())
        grid[(dx < wx) | (dy < wy)] = solid

    lines = tuple("".join(row) for row in grid.tolist())
    return AsciiResult(lines=lines, cols=cols, rows=rows, cell_aspect=float(aspect))


# --------------------------------------------------------------------------
# Rendering and export
# --------------------------------------------------------------------------
def render_image(result: AsciiResult, font_size: int = DEFAULT_FONT_SIZE,
                 dark_canvas: bool = False) -> Image.Image:
    """Draw the character grid onto an RGB canvas (black on white, or white on black)."""
    font = _open_font(font_size)
    ascent, cell_h, _ = _cell_metrics(font)
    # Cell width follows the aspect used for the grid, so the picture keeps its
    # proportions even if the user overrides the aspect ratio manually.
    cell_w = max(1, round(cell_h * result.cell_aspect))
    width, height = cell_w * result.cols, cell_h * result.rows
    if width * height > MAX_OUTPUT_PIXELS:
        raise ExportError(
            f"The image would be {width} × {height} px, which is too large. "
            "Lower the font size or use fewer columns."
        )
    fg, bg = ((255, 255, 255), (0, 0, 0)) if dark_canvas else ((0, 0, 0), (255, 255, 255))

    coverage = Image.new("L", (width, height), 0)   # glyph coverage mask
    glyphs = {}                                     # each distinct symbol is drawn once
    for row, line in enumerate(result.lines):
        top = row * cell_h
        for col, ch in enumerate(line):
            if ch == " ":
                continue
            glyph = glyphs.get(ch)
            if glyph is None:
                glyph = Image.new("L", (cell_w, cell_h), 0)
                # anchor "ms": centred horizontally on the cell, sitting on the baseline
                ImageDraw.Draw(glyph).text((cell_w / 2, ascent), ch, font=font, fill=255, anchor="ms")
                glyphs[ch] = glyph
            coverage.paste(glyph, (col * cell_w, top))

    canvas = Image.new("RGB", (width, height), bg)
    canvas.paste(fg, mask=coverage)
    return canvas


def export(result: AsciiResult, path: str, fmt: OutputFormat,
           font_size: int = DEFAULT_FONT_SIZE, dark_canvas: bool = False,
           jpeg_quality: int = 92) -> None:
    """Write the result to `path` as PNG, JPEG or TXT."""
    fmt = OutputFormat(fmt)   # also accepts the plain strings "png" / "jpeg" / "txt"
    try:
        if fmt is OutputFormat.TXT:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(result.to_text())
        else:
            img = render_image(result, font_size, dark_canvas)
            if fmt is OutputFormat.PNG:
                img.save(path, format="PNG", optimize=True)
            else:
                img.save(path, format="JPEG", quality=int(jpeg_quality), optimize=True)
    except AsciiError:
        raise
    except PermissionError:
        raise ExportError(f"No permission to write to:\n{path}") from None
    except FileNotFoundError:
        raise ExportError(f"The folder doesn't exist:\n{os.path.dirname(path) or path}") from None
    except OSError as exc:
        raise ExportError(f"Couldn't write the file:\n{path}\n\n{exc.strerror or exc}") from exc
    except ValueError as exc:
        raise ExportError(f"Couldn't save as {fmt.value.upper()}: {exc}") from exc
