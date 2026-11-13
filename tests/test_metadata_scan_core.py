"""Discovery streams without compression probing and stops within a large directory."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from wick import metaclean, scanner
from wick.settings import Settings


class MetadataDiscoveryTests(unittest.TestCase):
    def test_streams_unique_entries_without_compression_probes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for n in range(12):
                (root / f"{n}.png").write_bytes(b"not decoded by discovery")
            found = []
            with patch.object(scanner, "probe_entries", side_effect=AssertionError("unwanted probing")):
                result = scanner.scan([root, root], Settings(), probe=False, found=found.append)
            self.assertEqual(len(found), 12)
            self.assertEqual({id(e) for e in found}, {id(e) for e in result.entries})

    def test_cancels_inside_one_large_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for n in range(100):
                (root / f"{n}.png").touch()
            cancel = threading.Event()
            found = []

            def receive(entry):
                found.append(entry)
                if len(found) == 3:
                    cancel.set()

            result = scanner.scan(root, Settings(), cancel=cancel, probe=False, found=receive)
            self.assertTrue(result.cancelled)
            self.assertEqual(len(found), 3)

    def test_video_summary_reads_tags_without_decoding_hdr_frames(self):
        with patch("wick.probe.probe_raw", return_value={"format": {"tags": {"LOCATION": "59.3+18.0/"}}}):
            with patch("wick.probe.probe_video", side_effect=AssertionError("unwanted media analysis")):
                summary = metaclean.summarize(Path("hdr.mp4"))
        self.assertEqual(summary.gps, "59.3+18.0")
        self.assertFalse(summary.error)

    def test_unreadable_is_not_reported_as_no_metadata(self):
        summary = metaclean.summarize(Path("missing-photo.png"))
        self.assertTrue(summary.error)
        self.assertFalse(summary.anything)


if __name__ == "__main__":
    unittest.main()
