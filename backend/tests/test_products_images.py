import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ingest import products


class FakeImage:
    def __init__(self, size):
        self.size = size
        self.width, self.height = size

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def convert(self, mode):
        return self

    def copy(self):
        return FakeImage(self.size)

    def thumbnail(self, bounds, resample):
        factor = min(1, bounds[0] / self.width, bounds[1] / self.height)
        self.size = (round(self.width * factor), round(self.height * factor))

    def save(self, path, **options):
        Path(path).write_text(json.dumps({"size": self.size, "options": options}), encoding="utf-8")


class ProductsImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.src, self.images = self.folder / "src", self.folder / "images"
        self.rows = products.read_products()[:2]
        self.opened = []
        self.exif_calls = []
        self.driver = SimpleNamespace(open=self.open_image, new=lambda mode, size, bg: FakeImage(size),
            alpha_composite=lambda background, rgba: rgba, Resampling=SimpleNamespace(LANCZOS=1))
        self.ops = SimpleNamespace(exif_transpose=self.transpose)

    def open_image(self, path):
        self.opened.append(Path(path).name)
        return FakeImage((1600, 800))

    def transpose(self, original):
        self.exif_calls.append(original.size)
        return original

    def import_module(self, name):
        return {"PIL.Image": self.driver, "PIL.ImageOps": self.ops}[name]

    def test_build_resizes_both_outputs_reports_missing_and_removes_success_marker(self):
        products.placeholders(self.rows, self.images)
        src = self.src / self.rows[0]["sku_id"]
        src.mkdir(parents=True)
        (src / "other.png").write_bytes(b"fake")
        (src / "main.JPG").write_bytes(b"fake")
        with patch.object(products.importlib, "import_module", side_effect=self.import_module):
            missing = products.images_build(self.rows, self.src, self.images)
        self.assertEqual(missing, [self.rows[1]["sku_id"]])
        self.assertEqual(self.opened, ["main.JPG"])
        self.assertEqual(self.exif_calls, [(1600, 800)])
        success = self.images / self.rows[0]["sku_id"]
        self.assertFalse((success / "main.placeholder").exists())
        self.assertEqual(json.loads((success / "main.webp").read_text())["size"], [800, 400])
        self.assertEqual(json.loads((success / "thumb.webp").read_text())["size"], [320, 160])
        self.assertEqual(list(success.glob("*.tmp")), [])
        self.assertTrue((self.images / self.rows[1]["sku_id"] / "main.placeholder").exists())

    def test_no_source_creates_placeholder_without_altering_existing_real_image(self):
        folder = self.images / self.rows[0]["sku_id"]
        folder.mkdir(parents=True)
        (folder / "main.webp").write_bytes(b"original real image")
        with patch.object(products.importlib, "import_module", side_effect=self.import_module):
            missing = products.images_build(self.rows, self.src, self.images)
        self.assertEqual(len(missing), 2)
        self.assertEqual((folder / "main.webp").read_bytes(), b"original real image")
        self.assertFalse((folder / "main.placeholder").exists())
        self.assertTrue((self.images / self.rows[1]["sku_id"] / "main.svg").exists())

    def test_broken_source_preserves_placeholder_and_reports_sku(self):
        products.placeholders(self.rows, self.images)
        src = self.src / self.rows[0]["sku_id"]
        src.mkdir(parents=True)
        (src / "main.webp").write_bytes(b"broken")
        self.driver.open = lambda path: (_ for _ in ()).throw(OSError("corrupt"))
        with patch.object(products.importlib, "import_module", side_effect=self.import_module):
            with self.assertRaisesRegex(ValueError, "tinglan-s1 原图无法构建"):
                products.images_build(self.rows, self.src, self.images)
        self.assertTrue((self.images / self.rows[0]["sku_id"] / "main.placeholder").exists())

    def test_cli_missing_pillow_has_nonzero_status_and_actionable_message(self):
        with patch.object(products.importlib, "import_module", side_effect=ImportError()), redirect_stderr(io.StringIO()) as output:
            self.assertEqual(products.main(["images-build"]), 1)
        self.assertIn("python -m pip install Pillow", output.getvalue())

    def test_real_pillow_conversion_when_available(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("环境未安装 Pillow；转换流程已使用离线替身验证")
        src = self.src / self.rows[0]["sku_id"]
        src.mkdir(parents=True)
        Image.new("RGBA", (1200, 1200), (224, 122, 63, 120)).save(src / "main.png")
        products.placeholders(self.rows, self.images)
        products.images_build(self.rows, self.src, self.images)
        folder = self.images / self.rows[0]["sku_id"]
        for filename, expected in (("main.webp", (800, 800)), ("thumb.webp", (320, 320))):
            with Image.open(folder / filename) as image:
                self.assertEqual(image.size, expected)
                self.assertEqual(image.format, "WEBP")
        self.assertFalse((folder / "main.placeholder").exists())


if __name__ == "__main__":
    unittest.main()
