from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


def load_release_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "release" / "build_desktop_sidecar.py"
    spec = importlib.util.spec_from_file_location("build_desktop_sidecar", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_module = load_release_module()


class DesktopReleaseTests(unittest.TestCase):
    def test_binary_filename_for_windows_target_adds_exe_suffix(self) -> None:
        self.assertEqual(
            release_module.binary_filename_for_target("x86_64-pc-windows-msvc"),
            "somnia-sidecar-x86_64-pc-windows-msvc.exe",
        )

    def test_binary_filename_for_macos_target_has_no_extension(self) -> None:
        self.assertEqual(
            release_module.binary_filename_for_target("aarch64-apple-darwin"),
            "somnia-sidecar-aarch64-apple-darwin",
        )

    def test_target_platform_rejects_unknown_target(self) -> None:
        with self.assertRaises(ValueError):
            release_module.target_platform("wasm32-unknown-unknown")


if __name__ == "__main__":
    unittest.main()
