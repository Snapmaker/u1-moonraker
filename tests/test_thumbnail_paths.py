"""Thumbnail path regressions; also runnable with unittest without Klipper."""
from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock

from PIL import Image

from moonraker.components.file_manager.metadata import BaseSlicer, Cura
from moonraker.components.file_manager.file_manager import (
    BaseFileSystemObserver, FileManager, MetadataStorage,
)


class ThumbnailPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "gcodes"
        self.root.mkdir()
        self.storage = MetadataStorage.__new__(MetadataStorage)
        self.storage.gc_path = str(self.root)
        self.storage.metadata = {}
        self.storage.mddb = MagicMock()
        loop = Mock()

        async def run_in_thread(func, *args):
            return func(*args)

        loop.run_in_thread = run_in_thread
        self.storage.server = Mock()
        self.storage.server.get_event_loop.return_value = loop

    def gcode(self, folder):
        path = self.root / folder / "part.gcode"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def embedded_thumbnails(self, path, size=(300, 300)):
        buf = io.BytesIO()
        Image.new("RGB", size, "red").save(buf, format="PNG")
        data = base64.b64encode(buf.getvalue()).decode()
        slicer = BaseSlicer(str(path))
        slicer.set_data(
            f"; thumbnail begin {size[0]}x{size[1]} {len(data)}\n"
            f"; {data}\n; thumbnail end\n", "", 0,
        )
        return slicer.parse_thumbnails()

    def assert_thumbnail_paths(self, path, thumbs):
        self.assertTrue(thumbs)
        for thumb in thumbs:
            actual = (path.parent / thumb["relative_path"]).resolve()
            expected = (self.root / ".thumbs" /
                        path.parent.relative_to(self.root) / actual.name)
            self.assertEqual(actual, expected)
            self.assertEqual(actual.stat().st_size, thumb["size"])
            with Image.open(actual) as image:
                self.assertEqual(image.size, (thumb["width"], thumb["height"]))

    def test_embedded_and_generated_medium_paths(self):
        for folder in ("", "PrintExpress3D", "jobs/TPU parts"):
            with self.subTest(folder=folder):
                path = self.gcode(folder)
                thumbs = self.embedded_thumbnails(path)
                self.assertEqual(len(thumbs), 2)
                self.assert_thumbnail_paths(path, thumbs)

    def test_existing_medium_paths(self):
        for folder in ("", "jobs/nested"):
            with self.subTest(folder=folder):
                path = self.gcode(folder)
                thumbs = self.embedded_thumbnails(path, (96, 96))
                self.assertEqual(len(thumbs), 1)
                self.assert_thumbnail_paths(path, thumbs)

    def test_thumbnail_fallback_without_gcodes_directory(self):
        self.root = Path(self.tmp.name).resolve() / "custom_root"
        self.root.mkdir()
        path = self.gcode("")
        self.assert_thumbnail_paths(path, self.embedded_thumbnails(path))

    def test_cura_paths(self):
        for folder in ("", "jobs/nested"):
            with self.subTest(folder=folder):
                path = self.gcode(folder)
                image = self.root / ".thumbs" / folder / "part.png"
                image.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (300, 300)).save(image)
                slicer = Cura(str(path))
                slicer.set_data("", "", 0)
                self.assert_thumbnail_paths(path, slicer.parse_thumbnails())

    def test_thumbnail_endpoint_keeps_root_relative_paths(self):
        fm = FileManager.__new__(FileManager)
        fm.gcode_metadata = self.storage
        for folder in ("", "PrintExpress3D", "jobs/nested"):
            with self.subTest(folder=folder):
                path = self.gcode(folder)
                fname = str(path.relative_to(self.root))
                thumbs = self.embedded_thumbnails(path)
                self.storage.metadata[fname] = {"thumbnails": thumbs}
                request = Mock()
                request.get_str.return_value = fname
                result = asyncio.run(fm._handle_list_thumbs(request))
                for thumb in result:
                    self.assertNotIn("relative_path", thumb)
                    self.assertTrue(thumb["thumbnail_path"].startswith(".thumbs/"))
                    self.assertTrue((self.root / thumb["thumbnail_path"]).is_file())
                self.assertEqual(self.storage.get(fname)["thumbnails"], thumbs)

    def test_remove_thumbnails(self):
        for folder in ("", "jobs/nested"):
            with self.subTest(folder=folder):
                path = self.gcode(folder)
                thumbs = self.embedded_thumbnails(path)
                self.storage._remove_thumbs({
                    str(path.relative_to(self.root)): {"thumbnails": thumbs}
                })
                for thumb in thumbs:
                    self.assertFalse((path.parent / thumb["relative_path"]).exists())

    def test_remove_directory_thumbnails_after_directory_is_gone(self):
        path = self.gcode("jobs/nested")
        thumbs = self.embedded_thumbnails(path)
        path.unlink()
        path.parent.rmdir()
        path.parent.parent.rmdir()
        self.storage._remove_thumbs({
            "jobs/nested/part.gcode": {"thumbnails": thumbs}
        })
        self.assertFalse(any((self.root / ".thumbs/jobs/nested").iterdir()))

    def test_legacy_cache_paths_are_converted_without_rescan(self):
        config = Mock()
        db = MagicMock()
        db.get_item.return_value.result.return_value = 3
        mddb = db.wrap_namespace.return_value
        mddb.as_dict.return_value = {
            "jobs/deep/part.gcode": {"uuid": "preserve-me", "thumbnails": [
                {"relative_path": ".thumbs/jobs/deep/part.png"}
            ]},
            "part.gcode": {"thumbnails": [{"relative_path": ".thumbs/part.png"}]},
            "jobs/custom.gcode": {"thumbnails": [
                {"relative_path": ".thumbs/custom.png"}
            ]},
        }
        storage = MetadataStorage(config, db)
        thumbs = storage.get("jobs/deep/part.gcode")["thumbnails"]
        self.assertEqual(thumbs[0]["relative_path"],
                         "../../.thumbs/jobs/deep/part.png")
        self.assertEqual(storage.get("jobs/deep/part.gcode")["uuid"], "preserve-me")
        self.assertEqual(storage.get("part.gcode")["thumbnails"][0]["relative_path"],
                         ".thumbs/part.png")
        self.assertEqual(storage.get("jobs/custom.gcode")["thumbnails"][0]
                         ["relative_path"], ".thumbs/custom.png")
        mddb.clear.assert_not_called()
        mddb.__setitem__.assert_called_once_with(
            "jobs/deep/part.gcode", storage.metadata["jobs/deep/part.gcode"]
        )
        # Loading the converted cache again must not rewrite it.
        mddb.__setitem__.reset_mock()
        MetadataStorage(config, db)
        mddb.__setitem__.assert_not_called()

    def test_move_file_thumbnails_and_persist_paths(self):
        for source, dest in (("part.gcode", "jobs/deep/renamed.gcode"),
                             ("jobs/part.gcode", "part.gcode"),
                             ("jobs/part.gcode", "other/part.gcode")):
            with self.subTest(source=source, dest=dest):
                path = self.gcode(str(Path(source).parent))
                thumbs = self.embedded_thumbnails(path)
                old_paths = [(path.parent / t["relative_path"]).resolve()
                             for t in thumbs]
                self.storage.metadata[source] = {"thumbnails": thumbs}
                target = self.root / dest
                target.parent.mkdir(parents=True, exist_ok=True)
                path.rename(target)
                asyncio.run(self.storage.move_file_metadata(source, dest))
                self.assert_thumbnail_paths(target, thumbs)
                self.assertTrue(all(not p.exists() for p in old_paths))
                self.assertEqual(self.storage.mddb.__setitem__.call_args.args,
                                 (dest, self.storage.metadata[dest]))

    def test_move_directory_thumbnails(self):
        path = self.gcode("jobs/deep")
        thumbs = self.embedded_thumbnails(path)
        self.storage.metadata["jobs/deep/part.gcode"] = {"thumbnails": thumbs}
        (self.root / "archive").mkdir()
        (self.root / "jobs").rename(self.root / "archive/jobs")
        asyncio.run(self.storage.move_directory_metadata("jobs", "archive/jobs"))
        target = self.root / "archive/jobs/deep/part.gcode"
        self.assert_thumbnail_paths(target, thumbs)

    def test_directory_observer_returns_thumbnail_move(self):
        observer = BaseFileSystemObserver.__new__(BaseFileSystemObserver)
        observer.file_manager = Mock()
        observer.file_manager.get_relative_path.side_effect = ["jobs", "archive"]
        observer.gcode_metadata = Mock()
        result = observer.try_move_metadata(
            "gcodes", "gcodes", "old", "new", is_dir=True
        )
        self.assertIs(result, observer.gcode_metadata.move_directory_metadata())


if __name__ == "__main__":
    unittest.main()
