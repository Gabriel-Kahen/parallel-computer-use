import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from parallel_computer_use import ocr


def worker_png_header(width=800, height=600):
    return ocr._PNG_IHDR + struct.pack(">II", width, height) + bytes(9)


class OcrObserverTest(unittest.TestCase):
    def test_preserves_visible_lines_and_filters_control_characters(self):
        tsv = (b"1\t1\t0\t0\t0\t0\t0\t0\t1\t1\t-1\tignored\n"
               b"5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t90\tLocal\n"
               b"5\t1\t1\t1\t1\t2\t0\t0\t1\t1\t95\tcat\x00alog\n"
               b"5\t1\t1\t1\t2\t1\t0\t0\t1\t1\t85\tDesk lamp $24\n"
               b"malformed")
        self.assertEqual(ocr._visible_text(tsv), "Local catalog\nDesk lamp $24")
        with patch.object(ocr, "_MAX_TEXT", 20):
            self.assertEqual(ocr._visible_text(tsv), "Local catalog")

    def test_missing_executable_and_invalid_or_cancelled_requests_skip_ocr(self):
        with patch.object(ocr.shutil, "which", return_value=None):
            observer = ocr.OcrObserver()
        self.assertFalse(observer.available)
        cancel = threading.Event()
        cancel.set()
        with patch.object(ocr, "_read_tsv") as read:
            self.assertIsNone(observer.observe(worker_png_header()))
            with patch.object(ocr.shutil, "which", return_value="/usr/bin/tesseract"):
                observer = ocr.OcrObserver()
            self.assertIsNone(observer.observe(worker_png_header(), cancel))
            self.assertIsNone(observer.observe(b"outside PNG"))
            self.assertIsNone(observer.observe(worker_png_header(width=1921)))
            read.assert_not_called()

    def test_child_timeout_and_output_overflow_return_no_text(self):
        row = "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t90\tLocal catalog"
        cases = (
            (f"import time\ntime.sleep(0.3)\nprint({row!r})\n", {"_TIMEOUT": 0.12}),
            (f"print({row!r})\nprint('x' * 1024)\n", {"_MAX_TSV": 128}),
        )
        with tempfile.TemporaryDirectory() as parent:
            executable = Path(parent) / "tesseract"
            for script, limits in cases:
                with self.subTest(limits=limits):
                    executable.write_text(f"#!{sys.executable}\n{script}", encoding="utf-8")
                    executable.chmod(0o700)
                    with patch.object(ocr.shutil, "which", return_value=str(executable)):
                        observer = ocr.OcrObserver()
                    self.assertEqual(observer.observe(worker_png_header()), "Local catalog")
                    with patch.multiple(ocr, **limits):
                        self.assertIsNone(observer.observe(worker_png_header()))
