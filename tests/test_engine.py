"""Engine tests. Run from the project root:  python -m unittest discover -v"""

import os
import tempfile
import unittest

import numpy as np
from PIL import Image

from ascii_art_studio import engine
from ascii_art_studio.engine import (
    ConversionSettings, EdgeMode, OutputFormat, SizeMode, convert, parse_charset,
)


def gradient_image(w=200, h=100):
    row = np.linspace(0, 255, w, dtype=np.uint8)
    return Image.fromarray(np.tile(row, (h, 1))).convert("RGB")


class CharsetTests(unittest.TestCase):
    def test_valid_sets(self):
        self.assertEqual(parse_charset("@#. "), "@#. ")
        self.assertEqual(parse_charset("X"), "X")
        self.assertEqual(parse_charset("█▓▒░ "), "█▓▒░ ")

    def test_newlines_are_dropped(self):
        self.assertEqual(parse_charset("@#\n. "), "@#. ")

    def test_invalid_sets(self):
        for bad in ("", "\n", "   ", "a\x00b", "漢字", "e\u0301"):
            with self.assertRaises(engine.CharsetError, msg=repr(bad)):
                parse_charset(bad)
        with self.assertRaises(engine.CharsetError):
            parse_charset("x" * (engine.MAX_CHARSET_LEN + 1))

    def test_all_presets_are_valid(self):
        for name, chars in engine.PRESETS.items():
            self.assertEqual(parse_charset(chars), chars, name)


class GridTests(unittest.TestCase):
    def test_aspect_correction(self):
        # 2:1 picture, 100 columns, cell width/height = 0.5  ->  25 rows
        s = ConversionSettings(columns=100, cell_aspect=0.5)
        r = convert(gradient_image(200, 100), s)
        self.assertEqual((r.cols, r.rows), (100, 25))
        self.assertTrue(all(len(line) == 100 for line in r.lines))

    def test_size_modes(self):
        img = gradient_image()
        base = convert(img, ConversionSettings(columns=80, cell_aspect=0.5))
        up = convert(img, ConversionSettings(size_mode=SizeMode.UPSCALE, columns=80, scale=2.0, cell_aspect=0.5))
        down = convert(img, ConversionSettings(size_mode=SizeMode.DOWNSCALE, columns=80, scale=0.5, cell_aspect=0.5))
        new = convert(img, ConversionSettings(size_mode=SizeMode.NEW, detail_px=10, cell_aspect=0.5))
        self.assertEqual((base.cols, up.cols, down.cols, new.cols), (80, 160, 40, 20))

    def test_dark_to_light_mapping(self):
        r = convert(gradient_image(), ConversionSettings(charset="@. ", columns=60, cell_aspect=0.5))
        line = r.lines[len(r.lines) // 2]
        self.assertEqual(line[0], "@")      # black end -> darkest symbol
        self.assertEqual(line[-1], " ")     # white end -> lightest symbol

    def test_single_character_set(self):
        r = convert(gradient_image(), ConversionSettings(charset="#", columns=60, cell_aspect=0.5))
        used = set("".join(r.lines))
        self.assertEqual(used, {"#", " "})

    def test_too_many_cells(self):
        s = ConversionSettings(columns=engine.MAX_COLS, cell_aspect=2.0)
        with self.assertRaises(engine.ConversionError):
            convert(gradient_image(100, 1000), s)

    def test_invalid_settings(self):
        with self.assertRaises(engine.ConversionError):
            convert(gradient_image(), ConversionSettings(cell_aspect=5.0))


class EdgeTests(unittest.TestCase):
    def black(self):
        return Image.new("RGB", (400, 200), (0, 0, 0))

    def test_none_is_plain(self):
        r = convert(self.black(), ConversionSettings(charset="#. ", columns=60, cell_aspect=0.5))
        self.assertTrue(all(set(line) == {"#"} for line in r.lines))

    def test_sharp_edges_make_solid_frame(self):
        white = Image.new("RGB", (400, 200), (255, 255, 255))
        s = ConversionSettings(charset="@. ", columns=60, cell_aspect=0.5,
                               edge_mode=EdgeMode.SHARP, sharp_width=2)
        r = convert(white, s)
        self.assertEqual(r.lines[0], "@" * r.cols)
        self.assertEqual(r.lines[1], "@" * r.cols)
        self.assertEqual(r.lines[r.rows // 2][:4], "@@@@")      # 2 rows -> 4 columns wide
        self.assertEqual(r.lines[r.rows // 2][4:-4].strip(), "")  # interior untouched

    def test_gradient_fades_continuously(self):
        s = ConversionSettings(charset="#", columns=100, cell_aspect=0.5,
                               edge_mode=EdgeMode.GRADIENT, fade_percent=40)
        r = convert(self.black(), s)
        mid = r.rows // 2
        self.assertEqual(r.lines[0].strip(), "")                 # outermost row is empty
        self.assertEqual(r.lines[mid][0], " ")                   # outermost column is empty
        self.assertEqual(r.lines[mid][r.cols // 2], "#")         # centre is untouched
        # ink density rises monotonically (within noise) from the left border inward
        grid = np.array([[c != " " for c in line] for line in r.lines], dtype=float)
        profile = grid[r.rows // 4: 3 * r.rows // 4].mean(axis=0)[: r.cols // 2]
        bands = [profile[i:i + 10].mean() for i in range(0, 40, 10)]
        self.assertTrue(all(a <= b + 0.05 for a, b in zip(bands, bands[1:])), bands)
        self.assertLess(bands[0], 0.4)
        self.assertGreater(bands[-1], 0.9)

    def test_gradient_width_is_adjustable_and_deterministic(self):
        def ink(pct):
            s = ConversionSettings(charset="#", columns=100, cell_aspect=0.5,
                                   edge_mode=EdgeMode.GRADIENT, fade_percent=pct)
            return convert(self.black(), s)
        narrow, wide = ink(5), ink(45)
        count = lambda r: sum(line.count("#") for line in r.lines)
        self.assertGreater(count(narrow), count(wide))
        self.assertEqual(ink(20).lines, ink(20).lines)


class IoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.result = convert(gradient_image(), ConversionSettings(columns=40))

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_txt_export(self):
        p = self.path("a.txt")
        engine.export(self.result, p, OutputFormat.TXT)
        with open(p, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), self.result.rows)

    def test_png_and_jpeg_export(self):
        for fmt, name in ((OutputFormat.PNG, "a.png"), (OutputFormat.JPEG, "a.jpg")):
            p = self.path(name)
            engine.export(self.result, p, fmt, font_size=14, dark_canvas=True)
            with Image.open(p) as im:
                self.assertEqual(im.mode, "RGB")
                self.assertGreater(im.width, self.result.cols)
                # the picture is not stretched: pixel ratio ~ physical ratio of the source
                self.assertAlmostEqual(im.width / im.height, 2.0, delta=0.25)

    def test_export_accepts_plain_string_formats(self):
        p = self.path("s.txt")
        engine.export(self.result, p, "txt")
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().splitlines()), self.result.rows)

    def test_unicode_blocks_render(self):
        r = convert(gradient_image(), ConversionSettings(charset="█▓▒░ ", columns=40))
        img = engine.render_image(r, 12)
        self.assertGreater(np.asarray(img.convert("L")).min(), -1)
        self.assertLess(np.asarray(img.convert("L")).mean(), 250)   # something was drawn

    def test_write_errors(self):
        with self.assertRaises(engine.ExportError):
            engine.export(self.result, self.path("missing/dir/a.png"), OutputFormat.PNG)
        with self.assertRaises(engine.ExportError):
            engine.export(self.result, self.path("missing/dir/a.txt"), OutputFormat.TXT)

    def test_load_errors(self):
        with self.assertRaises(engine.ImageLoadError):
            engine.load_image(self.path("nope.png"))
        bad = self.path("bad.png")
        with open(bad, "wb") as fh:
            fh.write(b"this is not a picture")
        with self.assertRaises(engine.ImageLoadError):
            engine.load_image(bad)

    def test_transparent_png_becomes_white(self):
        p = self.path("t.png")
        Image.new("RGBA", (20, 20), (0, 0, 0, 0)).save(p)
        self.assertEqual(engine.load_image(p).getpixel((5, 5)), (255, 255, 255))


if __name__ == "__main__":
    unittest.main()
