from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.remote_privacy_audit import scan_for_sentinel, write_audit_report


class PrivacyAuditTests(unittest.TestCase):
    def test_scan_reports_sentinel_locations_without_exposing_content(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            clean = root / "clean.log"
            leaked = root / "relay.log"
            clean.write_text("request_id=abc\n", encoding="utf-8")
            leaked.write_text("prompt=PRIVACY_SENTINEL\n", encoding="utf-8")

            result = scan_for_sentinel([root], "PRIVACY_SENTINEL")
            report = root / "report.json"
            write_audit_report(result, report)

            self.assertFalse(result.passed)
            self.assertEqual(result.matches, (str(leaked),))
            self.assertNotIn("PRIVACY_SENTINEL", report.read_text(encoding="utf-8"))

    def test_missing_paths_are_inspected_without_failing(self) -> None:
        with TemporaryDirectory() as directory:
            result = scan_for_sentinel([Path(directory) / "missing"], "sentinel")

            self.assertTrue(result.passed)
            self.assertEqual(len(result.inspected_paths), 1)


if __name__ == "__main__":
    unittest.main()
