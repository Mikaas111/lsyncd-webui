import importlib
import os
import sys
import tempfile
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP_DIR)

TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = os.path.join(TMP.name, "data")
os.environ["CONFIG_DIR"] = os.path.join(TMP.name, "config")
for folder in ("Documents", "Documents/2024", "Photos", "has \"quote\""):
    os.makedirs(os.path.join(os.environ["DATA_DIR"], folder))

server = importlib.import_module("server")
count = importlib.import_module("count")


class ProgressTest(unittest.TestCase):
    def test_progress_line_with_counts(self):
        m = server.PROGRESS_RE.match("    1,234,567  45%   12.34MB/s    0:01:23 (xfr#12, to-chk=345/1000)")
        self.assertEqual(m.group(2), "45")
        self.assertEqual(m.group(3), "12.34MB/s")
        self.assertEqual((m.group(5), m.group(6)), ("345", "1000"))

    def test_progress_line_without_counts(self):
        m = server.PROGRESS_RE.match("         32,768   3%    0.00kB/s    0:00:00")
        self.assertEqual(m.group(2), "3")
        self.assertIsNone(m.group(5))

    def test_other_output_is_not_progress(self):
        self.assertIsNone(server.PROGRESS_RE.match("rsync: connection unexpectedly closed"))


class SettingsTest(unittest.TestCase):
    def test_folders_are_normalised_and_nested_ones_dropped(self):
        s, errors = server.validate({"folders": ["Documents", "Documents/2024", "/Photos/", "../etc"]})
        self.assertEqual(s["folders"], ["Documents", "Photos"])
        self.assertEqual(len(errors), 1)  # ../etc becomes "etc", which does not exist

    def test_bad_values_are_reported(self):
        _, errors = server.validate({"host": "bad host!", "port": 70000, "nightly": "25:00", "target": "backup"})
        self.assertEqual(len(errors), 4)

    def test_empty_nightly_turns_it_off(self):
        s, errors = server.validate({"nightly": ""})
        self.assertEqual((s["nightly"], errors), ("", []))


class LsyncdConfigTest(unittest.TestCase):
    def test_config_has_one_sync_per_folder_and_escapes_strings(self):
        s, _ = server.validate({
            "host": "10.0.0.2", "folders": ["Documents", 'has "quote"'],
            "excludes": ["*.tmp", 'odd"name'], "bwlimit": 500,
        })
        conf = server.build_lsyncd_conf(s, s["folders"])
        self.assertEqual(conf.count("sync {"), 2)
        self.assertIn('targetdir = "/backup/has \\"quote\\"/"', conf)
        self.assertIn('"odd\\"name"', conf)
        self.assertIn('_extra = { "--bwlimit=500" }', conf)
        self.assertIn("delete = true", conf)
        self.assertIn("init = false", conf)
        self.assertIn(f'ssh = {{ binary = "{server.SSH_WRAPPER}" }}', conf)
        self.assertEqual(conf.count("{"), conf.count("}"))


class CountTest(unittest.TestCase):
    def test_counts_respect_excludes(self):
        with tempfile.TemporaryDirectory() as root:
            for d in ("a", "a/cache", "b"):
                os.makedirs(os.path.join(root, d))
            for f, size in (("top.txt", 3), ("a/keep.txt", 5), ("a/skip.tmp", 7), ("a/cache/x", 1), ("b/y", 2)):
                with open(os.path.join(root, f), "w") as fh:
                    fh.write("x" * size)
            everything = count.count_tree(root)
            self.assertEqual((everything["dirs"], everything["files"], everything["bytes"]), (3, 5, 18))
            filtered = count.count_tree(root, ["*.tmp", "cache/", "/top.txt"])
            self.assertEqual((filtered["dirs"], filtered["files"], filtered["bytes"]), (2, 2, 7))

    def test_missing_folder(self):
        self.assertEqual(count.count_tree("/does/not/exist"), {"missing": True})


if __name__ == "__main__":
    unittest.main()
