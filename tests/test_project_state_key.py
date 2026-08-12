from __future__ import annotations

import unittest

from open_somnia.config.settings import _strip_windows_verbatim_prefix


class ProjectStateKeyTest(unittest.TestCase):
    """The Windows verbatim ``\\\\?\\`` prefix must not change the project key.

    Rust's ``std::fs::canonicalize`` (Tauri sidecar) yields a verbatim path on
    Windows; without stripping, the desktop hashed the same workspace to a
    different key than the CLI and read an empty sibling state dir.
    """

    def test_strip_windows_verbatim_local_prefix(self) -> None:
        self.assertEqual(
            _strip_windows_verbatim_prefix(r"\\?\D:\Project\Git\somnia"),
            r"D:\Project\Git\somnia",
        )

    def test_strip_windows_verbatim_unc_prefix(self) -> None:
        self.assertEqual(
            _strip_windows_verbatim_prefix(r"\\?\UNC\server\share\foo"),
            r"\\server\share\foo",
        )

    def test_strip_is_noop_without_prefix(self) -> None:
        self.assertEqual(_strip_windows_verbatim_prefix(r"D:\Project\Git\somnia"), r"D:\Project\Git\somnia")
        self.assertEqual(_strip_windows_verbatim_prefix("/home/user/somnia"), "/home/user/somnia")


if __name__ == "__main__":
    unittest.main()
