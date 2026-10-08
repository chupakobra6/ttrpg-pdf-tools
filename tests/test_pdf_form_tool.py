from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]


class AutosizeCliTests(unittest.TestCase):
    def test_output_copy_exists_even_when_autosize_changes_no_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sheet.pdf"
            with fitz.open() as document:
                page = document.new_page()
                page.insert_text((30, 30), "TTRPG reference")
                document.save(source)
            original = source.read_bytes()
            output_dir = root / "copies"
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "pdf_form_tool.py"),
                 str(source), "--autosize", "none", "--out-dir", str(output_dir)],
                capture_output=True, text=True, check=True,
            )
            self.assertIn("0 fields updated", result.stdout)
            self.assertEqual(source.read_bytes(), original)
            with fitz.open(output_dir / source.name) as output:
                self.assertEqual(output.page_count, 1)
                self.assertIn("TTRPG reference", output[0].get_text())


if __name__ == "__main__":
    unittest.main()
