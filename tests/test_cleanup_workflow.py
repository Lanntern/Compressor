"""Streaming scans, shared inventories and app/game controls; never compress a real installed app."""

import json
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wick import activity, journal, tools
from wick.cleanup import actions, appcompress, broken, common, extensions, game_results


class StreamingTests(unittest.TestCase):
    def test_result_append_and_worker_pool_publish_before_return(self):
        batches = []
        with common.stream_results(lambda key, items: batches.append((key, items))):
            result = common.ScanResult("test")
            item = common.Candidate(Path("one.bin"), 1)
            result.items.append(item)
            self.assertIs(batches[0][1][0], item)
            sink = common.publisher()
            with ThreadPoolExecutor(1) as pool:
                pool.submit(sink, "test", [item]).result()
        self.assertEqual(len(batches), 2)
        result.items.append(common.Candidate(Path("two.bin"), 1))
        self.assertEqual(len(batches), 2)     # no callback leaks outside its context

    def test_shared_inventory_reuses_listings_and_does_not_outlive_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            roots = [root / "drive-a", root / "drive-b"]
            for folder in roots:
                folder.mkdir()
                (folder / "file.bin").write_bytes(b"a")
            original = os.scandir
            with patch.object(os, "scandir", wraps=original) as walk, common.shared_inventory():
                first = list(common.walk_files(roots))
                calls = walk.call_count
                (roots[0] / "new.bin").write_bytes(b"new")
                self.assertEqual(list(common.walk_files(roots)), first)
                self.assertEqual(walk.call_count, calls)
            self.assertEqual(len(list(common.walk_files(roots))), 3)

    def test_cancelled_walk_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for i in range(3):
                (root / f"{i}.bin").write_bytes(b"x")
            cancel = threading.Event()
            with common.shared_inventory():
                iterator = common.walk_files([root], cancel=cancel)
                next(iterator)
                cancel.set()
                self.assertEqual(list(iterator), [])
                cancel.clear()
                self.assertEqual(len(list(common.walk_files([root], cancel=cancel))), 3)

    def test_extensions_publish_while_more_folders_remain(self):
        batches = []

        def listing():
            for i in range(128):
                yield Path(f"{i}.png"), SimpleNamespace(st_size=10)
            self.assertTrue(batches, "first results must arrive before folder enumeration finishes")
            yield Path("last.png"), SimpleNamespace(st_size=10)

        with common.stream_results(lambda key, items: batches.extend(items)), \
                patch.object(extensions, "walk_files", return_value=listing()), \
                patch.object(extensions, "wrong_extension", return_value=".jpg"):
            result = extensions.find_wrong_extensions([Path("root")])
        self.assertEqual(len(result.items), 129)

    def test_broken_pool_delivers_validated_results(self):
        batches = []
        files = [(Path("bad.png"), SimpleNamespace(st_size=10))]
        with common.stream_results(lambda key, items: batches.extend(items)), \
                patch.object(broken, "walk_files", return_value=files), \
                patch.object(broken, "check_file", return_value=(broken.UNREADABLE, "bad test file")):
            result = broken.find_broken([Path("root")])
        self.assertTrue(batches)
        self.assertEqual(len(result.items), 1)


class AppCompressionTests(unittest.TestCase):
    def test_all_algorithms_and_rejected_unknown_algorithm(self):
        for mode in appcompress.ALGORITHMS:
            args = appcompress._compact_args(Path("game"), False, mode)
            self.assertNotIn("/i", args)  # /i hides access-denied failures behind exit code 0
            self.assertIn("/exe:" + mode, args)
        with self.assertRaises(ValueError):
            appcompress._compact_args(Path("game"), False, "malicious;command")
        self.assertEqual(appcompress.exclusions("*.MP4; png,.zip"), {".mp4", ".png", ".zip"})

    def test_compression_applies_exclusions_and_uses_explicit_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "game.bin").write_bytes(b"x")
            (root / "video.mp4").write_bytes(b"media")
            with patch.object(appcompress, "volume_type", return_value="NTFS"), \
                    patch.object(tools, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as run:
                code = appcompress.run_compact(root, algorithm_name="xpress4k", skip_extensions="mp4")
            self.assertEqual(code, 0)
            args = run.call_args.args[0]
            self.assertIn(str(root / "game.bin"), args)
            self.assertNotIn(str(root / "video.mp4"), args)
            self.assertFalse(any(a.startswith("/s:") for a in args))

    def test_native_errors_are_reported_and_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "game.bin").write_bytes(b"x")
            lines = []
            with patch.object(appcompress, "volume_type", return_value="NTFS"), \
                    patch.object(tools, "run", return_value=subprocess.CompletedProcess([], 1, "[ERR]", "Access denied")):
                with self.assertLogs("wick", level="WARNING"):
                    code = appcompress.run_compact(root, on_line=lines.append)
            self.assertEqual(code, 1)
            self.assertTrue(any("Access denied" in line for line in lines))

    def test_elevated_helper_uses_data_only_and_one_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "Game's files"
            root.mkdir()
            (root / "file.bin").write_bytes(b"x")
            created = []
            real_mkstemp = tempfile.mkstemp

            def capture(**kwargs):
                fd, name = real_mkstemp(**kwargs)
                created.append(Path(name))
                return fd, name

            def invoke(args, **kwargs):
                data = json.loads(created[-1].read_text(encoding="utf-8-sig"))
                self.assertEqual(len(data["batches"]), 1)
                self.assertIn(str(root / "file.bin"), data["batches"][0])
                self.assertIn("-Verb RunAs -WindowStyle Hidden", args[-1])
                self.assertNotIn("ExecutionPolicy", args[-1])
                self.assertEqual(created[-1].suffix, ".json")
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch.object(appcompress, "volume_type", return_value="NTFS"), \
                    patch.object(tempfile, "mkstemp", side_effect=capture), patch.object(tools, "run", side_effect=invoke) as run:
                self.assertEqual(appcompress.run_compact(root, elevate=True), 0)
                self.assertEqual(run.call_count, 1)
            self.assertFalse(created[-1].exists())

    def test_partial_compression_is_reported_and_undo_recorded(self):
        writer = SimpleNamespace(add=Mock())
        item = common.Candidate(Path("game"), 0, extra={"algorithm": "xpress4k"})
        with patch("wick.cleanup.inuse.busy_projects", return_value=set()), \
                patch.object(appcompress, "measure", side_effect=[(100, 100, 1), (100, 60, 1)]), \
                patch.object(appcompress, "run_compact", return_value=1223), \
                patch.object(appcompress, "uncompressed_snapshot", return_value=(0, "snapshot")):
            outcome = actions.compact_item(item, writer)
        self.assertFalse(outcome.ok)
        self.assertIn("Partially", outcome.message)
        record = writer.add.call_args.args[0]
        self.assertEqual(record.original_size, 100)
        self.assertEqual(record.output_size, 60)
        self.assertEqual(record.compression["algorithm"], "xpress4k")

    def test_history_keeps_first_before_size_and_latest_algorithm(self):
        first = journal.FileRecord("game", "game", 100, 70, "cleanup", "LZX", True, 1, mode="compact")
        latest = journal.FileRecord("game", "game", 90, 60, "cleanup", "XPRESS4K", True, 2, mode="compact",
                                    compression={"algorithm": "xpress4k"})
        runs = [journal.Run("new", 2, "game", {}, [latest]), journal.Run("old", 1, "game", {}, [first])]
        with patch.object(journal, "list_runs", return_value=runs):
            record = next(iter(appcompress.compression_records().values()))
            self.assertEqual(record["before"], 100)
            self.assertEqual(record["after"], 60)
            self.assertEqual(record["settings"]["algorithm"], "xpress4k")
            latest.undone = True
            self.assertEqual(appcompress.compression_records(), {})
            self.assertEqual(len(appcompress.compression_records(True)), 1)

    def test_preserved_old_timestamps_still_detect_new_uncompressed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            old = root / "old.bin"
            old.write_bytes(b"x" * 65536)
            os.utime(old, (1, 1))
            with patch.object(appcompress, "size_on_disk", side_effect=lambda path: path.stat().st_size):
                before = appcompress.uncompressed_snapshot(root)
                new = root / "new.bin"
                new.write_bytes(b"y" * 65536)
                os.utime(new, (1, 1))
                after = appcompress.uncompressed_snapshot(root)
                eligible = list(appcompress._compact_files(root, set(), None, changed_since=1000))
            self.assertNotEqual(before[1], after[1])
            self.assertEqual(after[0], 131072)
            self.assertIn(new, eligible)

    def test_sample_uses_temporary_copies_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            original = root / "game.bin"
            content = b"sample" * 12000
            original.write_bytes(content)
            sampled = []

            def compress(folder, **kwargs):
                sampled.append(folder)
                self.assertEqual(kwargs["algorithm_name"], "xpress4k")
                return 0

            with patch.object(appcompress, "volume_type", return_value="NTFS"), \
                    patch.object(appcompress, "run_compact", side_effect=compress), \
                    patch.object(appcompress, "size_on_disk", side_effect=lambda path: path.stat().st_size // 2):
                ratio, count = appcompress.sample_ratio(root, "xpress4k")
            self.assertEqual(ratio, 0.5)
            self.assertEqual(count, 1)
            self.assertFalse(sampled[0].exists())
            self.assertEqual(original.read_bytes(), content)


class CommunityAndActivityTests(unittest.TestCase):
    def test_community_estimates_validate_rows_and_separate_algorithms(self):
        result = game_results.parse([{"SteamID": 123, "GameName": "Test", "CompressionResults": [
            {"CompType": 0, "BeforeBytes": 100, "AfterBytes": 80, "TotalResults": 4},
            {"CompType": 3, "BeforeBytes": 100, "AfterBytes": 50, "TotalResults": 2},
            {"CompType": 1, "BeforeBytes": 0, "AfterBytes": 0}]}])
        self.assertEqual(result[123]["modes"]["xpress4k"]["ratio"], 0.8)
        self.assertEqual(result[123]["modes"]["lzx"]["ratio"], 0.5)
        self.assertNotIn("xpress8k", result[123]["modes"])
        with self.assertRaises(ValueError):
            game_results.parse({})

    def test_steam_matching_is_local_and_download_is_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            apps = Path(directory) / "steamapps"
            folder = apps / "common" / "Test Game"
            folder.mkdir(parents=True)
            (apps / "appmanifest_123.acf").write_text('"appid" "123" "installdir" "Test Game"')
            self.assertEqual(game_results.steam_id(folder), 123)
        with patch.dict(os.environ, {"WICK_NO_NETWORK": "1"}), patch.object(game_results.urllib.request, "urlopen") as network:
            with self.assertRaises(RuntimeError):
                game_results.download()
            network.assert_not_called()

    def test_invalid_database_does_not_replace_previous_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "database.json"
            cache.write_text("[]")
            with patch.object(game_results, "cache_path", return_value=cache):
                with self.assertRaises(ValueError):
                    game_results.save(b'{"bad":true}')
            self.assertEqual(cache.read_text(), "[]")

    def test_activity_never_invents_progress_for_unknown_tasks(self):
        a = activity.Activity("a", "Compress", "Encoding", 0.5)
        b = activity.Activity("b", "Scan", "Searching")
        self.assertEqual(activity.overall([a, b]), (None, False))
        b.fraction = 0.25
        self.assertEqual(activity.overall([a, b]), (0.375, False))
        a.paused = b.paused = True
        self.assertTrue(activity.overall([a, b])[1])


if __name__ == "__main__":
    unittest.main()
