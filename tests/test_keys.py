"""Local key files are read into the environment without overriding what's already set."""
import tempfile
import unittest
from pathlib import Path

from papertrade.keys import load_keys, parse


class KeysTest(unittest.TestCase):
    def test_parse(self):
        text = "# my keys\n\nEIA_API_KEY = abc123\nexport X=\"quoted\"\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-z  # a year\njunk line\nEMPTY=\n"
        self.assertEqual(parse(text), {"EIA_API_KEY": "abc123", "X": "quoted",
                                       "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-z"})

    def test_load_from_api_keys_file(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "api_keys").write_text("EIA_API_KEY=abc\nSET_ALREADY=new\n")
            env = {"SET_ALREADY": "old"}
            self.assertEqual(load_keys(Path(d), env), ["EIA_API_KEY"])
            self.assertEqual(env, {"EIA_API_KEY": "abc", "SET_ALREADY": "old"})

    def test_no_file_is_fine(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(load_keys(Path(d), {}), [])

    def test_key_files_are_ignored_by_git(self):
        ignored = (Path(__file__).resolve().parent.parent / ".gitignore").read_text().split()
        for name in ("keys.env", "api_keys", "api_keys.txt"):
            self.assertIn(name, ignored)


if __name__ == "__main__":
    unittest.main()
