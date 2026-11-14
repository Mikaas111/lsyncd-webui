import importlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP_DIR)

TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = os.path.join(TMP.name, "data")
os.environ["DATA_RW_DIR"] = os.environ["DATA_DIR"]
os.environ["CONFIG_DIR"] = os.path.join(TMP.name, "config")
for folder in ("Documents", "Documents/2024", "Photos", "has \"quote\""):
    os.makedirs(os.path.join(os.environ["DATA_DIR"], folder))
# Unraid-style exclusive share: a relative shortcut from the user share to a pool
os.makedirs(os.path.join(TMP.name, "cache", "appdata", "plex"))
os.symlink("../cache/appdata/", os.path.join(os.environ["DATA_DIR"], "appdata"))
os.symlink("../nopool/gone/", os.path.join(os.environ["DATA_DIR"], "gone"))

server = importlib.import_module("server")
count = importlib.import_module("count")
speedtest = importlib.import_module("speedtest")
plain = importlib.import_module("plain")


def run_here(command, options=()):
    """Stands in for ssh: runs the remote command on this machine."""
    return ["sh", "-c", command]


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
        self.assertIn('_extra = { "--out-format=@lsw@0 %i %l %b %n", "--whole-file", "--bwlimit=500" }', conf)
        self.assertIn("maxProcesses = 1,", conf)  # one transfer at a time to the backup
        self.assertIn('"--out-format=@lsw@1 %i %l %b %n"', conf)
        self.assertIn("delete = true", conf)
        self.assertIn("init = false", conf)
        self.assertIn(f'ssh = {{ binary = "{server.SSH_WRAPPER}" }}', conf)
        self.assertEqual(conf.count("{"), conf.count("}"))


class BackupLoadTest(unittest.TestCase):
    def test_whole_files_can_be_switched_off(self):
        on, _ = server.validate({"host": "10.0.0.2", "folders": ["Photos"], "twoway": ["Photos"]})
        off, _ = server.validate({"host": "10.0.0.2", "folders": ["Photos"], "twoway": ["Photos"], "whole_file": False})
        self.assertIn("--whole-file", server.rsync_base(on))
        self.assertNotIn("--whole-file", server.rsync_base(off))
        self.assertNotIn("--whole-file", server.build_lsyncd_conf(off, ["Photos"]))
        self.assertNotIn("-rsync", server.build_unison_cmd(off, ["Photos"]))

    def test_unison_pauses_and_continues(self):
        def state(pid):
            with open(f"/proc/{pid}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[0]

        daemon = server.Unison()
        daemon.proc = subprocess.Popen(["sleep", "30"])
        try:
            daemon.pause()
            time.sleep(0.2)
            self.assertEqual((daemon.paused, state(daemon.proc.pid)), (True, "T"))
            daemon.resume()
            time.sleep(0.2)
            self.assertEqual((daemon.paused, state(daemon.proc.pid)), (False, "S"))
            daemon.pause()
            proc = daemon.proc
            daemon.stop()  # stopping also works on a paused process
            self.assertIsNotNone(proc.poll())
        finally:
            if daemon.proc is not None:
                daemon.proc.kill()


class ScheduledTest(unittest.TestCase):
    def test_scheduled_folders_are_not_watched_live(self):
        s, errors = server.validate({
            "host": "10.0.0.2", "folders": ["Documents", "Photos", "appdata"],
            "scheduled": ["Photos", "appdata", "Elsewhere"], "twoway": ["appdata"], "schedule_hours": 12,
        })
        self.assertEqual(errors, [])
        self.assertEqual(s["scheduled"], ["Photos"])  # two-way wins, unknown folders are dropped
        self.assertEqual(server.mirror_folders(s), ["Documents"])
        self.assertEqual(server.scheduled_folders(s), ["Photos"])
        self.assertEqual(server.folder_mode(s, "Photos"), "scheduled")
        self.assertNotIn("Photos", server.build_lsyncd_conf(s, server.mirror_folders(s)))
        self.assertEqual(len(server.validate({"schedule_hours": 0})[1]), 1)

    def test_due_after_the_interval_or_soon_after_a_failure(self):
        s, _ = server.validate({"schedule_hours": 6})
        now = 1_000_000
        self.assertEqual(server.scheduled_due(s, {}, now), now)
        self.assertEqual(server.scheduled_due(s, {"last_scheduled": {"time": now, "ok": True}}, now), now + 6 * 3600)
        self.assertEqual(server.scheduled_due(s, {"last_scheduled": {"time": now, "ok": False}}, now), now + 1800)


class FullSyncProgressTest(unittest.TestCase):
    def test_scanning_then_comparing_moves_the_bar(self):
        jobs = server.Jobs()
        jobs.info = {"folder": "Photos", "state": "running", "kind": "full", "phase": ""}
        jobs.progress = server.new_progress()
        before = list(server.LOG)
        jobs._on_progress("building file list ... ")
        for n in range(0, 1000, 100):
            jobs._on_progress(f" {n} files...")
        info = jobs.snapshot()  # always the latest count, also when rsync then goes quiet
        self.assertEqual((info["phase"], info["scanned"], info["percent"], info["quiet"]), ("Scanning", 900, 0, 0))
        jobs._on_progress("400 files to consider")
        for i in range(100):
            jobs._on_progress(f"@lsw@ .f          1 0 d/f{i}")
        jobs._on_progress("@lsw@ *deleting   0 0 old")
        jobs._on_progress("     1,000,000  10%   40.00MB/s    0:00:01 (xfr#1, to-chk=10/400)")
        info = jobs.snapshot()
        self.assertEqual((info["phase"], info["items"], info["checked"], info["percent"]),
                         ("Comparing and sending", 400, 100, 25))
        self.assertEqual((info["bytes"], info["speed"]), (1_000_000, "40.00MB/s"))
        jobs._on_progress("   300,000,000  60%   40.00MB/s    0:00:01 (xfr#2, to-chk=9/400)")
        self.assertEqual(jobs.snapshot()["percent"], 60)  # bytes sent can be ahead of items compared
        self.assertEqual(list(server.LOG), before)  # none of this goes to the log
        jobs.progress = {}
        self.assertEqual(jobs.snapshot()["phase"], "")  # after rsync, the job's own phase shows again

    def test_what_rsync_waits_for(self):
        self.assertEqual(server.waiting_on("do_select"), "the backup server")
        self.assertEqual(server.waiting_on("do_sys_poll"), "the backup server")
        self.assertEqual(server.waiting_on("fuse_dev_do_read"), "the disk or file system")
        self.assertEqual(server.waiting_on("folio_wait_bit_common"), "the disk or file system")
        self.assertEqual(server.waiting_on("0"), "")
        self.assertEqual(server.waiting_on("hrtimer_nanosleep"), "")

    def test_where_rsync_is(self):
        folder = os.path.join(server.DATA_DIR, "Documents", "2024")
        proc = subprocess.Popen([sys.executable, "-c", "import os, sys, time; fd = os.open(sys.argv[1], os.O_RDONLY); time.sleep(5)", folder])
        try:
            time.sleep(0.3)
            self.assertEqual(server.rsync_activity(proc.pid)["now_at"], "Documents/2024")
        finally:
            proc.kill()
            proc.wait()

    def test_expected_items_come_from_the_last_count(self):
        os.makedirs(server.CONFIG_DIR, exist_ok=True)
        server.update_state(counts={"folders": {"A": {"main": {"dirs": 9, "files": 90}}}})
        try:
            self.assertEqual((server.Jobs._expected_items("A"), server.Jobs._expected_items("B")), (100, 0))
        finally:
            server.update_state(counts=None)


class TwoWayTest(unittest.TestCase):
    def test_twoway_must_be_a_selected_folder(self):
        s, errors = server.validate({"folders": ["Documents", "Photos"], "twoway": ["Photos", "Elsewhere"]})
        self.assertEqual((s["twoway"], errors), (["Photos"], []))

    def test_twoway_needs_write_access(self):
        old = server.DATA_RW_DIR
        server.DATA_RW_DIR = "/does/not/exist"
        try:
            _, errors = server.validate({"folders": ["Photos"], "twoway": ["Photos"]})
        finally:
            server.DATA_RW_DIR = old
        self.assertEqual(len(errors), 1)
        self.assertIn("/mnt-rw", errors[0])

    def test_twoway_folders_are_left_out_of_lsyncd(self):
        s, _ = server.validate({"host": "10.0.0.2", "folders": ["Documents", "Photos"], "twoway": ["Photos"]})
        self.assertEqual(server.mirror_folders(s), ["Documents"])
        self.assertEqual(server.twoway_folders(s), ["Photos"])
        conf = server.build_lsyncd_conf(s, server.mirror_folders(s))
        self.assertIn("Documents", conf)
        self.assertNotIn("Photos", conf)

    def test_unison_command(self):
        s, _ = server.validate({
            "host": "10.0.0.2", "folders": ["Photos"], "twoway": ["Photos"],
            "excludes": ["*.tmp", "cache/", "/Downloads/", "a/b"],
        })
        cmd = server.build_unison_cmd(s, ["Photos"])
        self.assertEqual(cmd[:3], ["unison", server.DATA_RW_DIR, "ssh://backup-target//backup"])
        self.assertEqual(cmd[cmd.index("-path") + 1], "Photos")
        for flag in ("-batch", "-copyonconflict", "-times", "-numericids"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("-repeat") + 1], "watch+86400")
        self.assertEqual(cmd[cmd.index("-maxthreads") + 1], "1")
        self.assertEqual(cmd[cmd.index("-rsync") + 1], "false")
        self.assertEqual(cmd[cmd.index("-prefer") + 1], "newer")
        self.assertEqual(cmd[cmd.index("-sshcmd") + 1], server.SSH_WRAPPER)
        ignores = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-ignore"]
        self.assertEqual(ignores, ["Name *.tmp", "Name cache", "Path Photos/Downloads", "Path Photos/a/b"])
        self.assertNotIn("-follow", cmd)


class ShortcutTest(unittest.TestCase):
    def test_browse_follows_relative_shortcuts(self):
        entries = {d["name"]: d for d in server.browse("")["dirs"]}
        self.assertEqual(entries["appdata"]["link"], "../cache/appdata/")
        self.assertFalse(entries["appdata"]["broken"])
        self.assertTrue(entries["appdata"]["children"])
        self.assertTrue(entries["gone"]["broken"])
        self.assertNotIn("link", entries["Photos"])
        self.assertEqual([d["name"] for d in server.browse("appdata")["dirs"]], ["plex"])

    def test_shortcut_can_be_selected_and_counted(self):
        s, errors = server.validate({"folders": ["appdata"]})
        self.assertEqual((s["folders"], errors), (["appdata"], []))
        with open(os.path.join(TMP.name, "cache", "appdata", "plex", "library.db"), "w") as f:
            f.write("x")
        counted = count.count_tree(server.source_dir("appdata"))
        self.assertEqual((counted["dirs"], counted["files"]), (1, 1))

    def test_broken_shortcut_explains_the_fix(self):
        _, errors = server.validate({"folders": ["gone"]})
        self.assertIn("shortcut to ../nopool/gone/", errors[0])
        self.assertIn("/mnt", errors[0])

    def test_unison_follows_a_shortcut(self):
        s, _ = server.validate({"host": "10.0.0.2", "folders": ["appdata"], "twoway": ["appdata"]})
        cmd = server.build_unison_cmd(s, ["appdata"])
        self.assertEqual(cmd[cmd.index("-follow") + 1], "Path appdata")

    def test_first_dir_prefers_an_existing_candidate(self):
        os.environ.pop("LSYNCD_WEBUI_TEST_DIR", None)
        self.assertEqual(server.first_dir("LSYNCD_WEBUI_TEST_DIR", ["/does/not/exist", TMP.name]), TMP.name)
        self.assertEqual(server.first_dir("LSYNCD_WEBUI_TEST_DIR", ["/nope1", "/nope2"]), "/nope2")
        os.environ["LSYNCD_WEBUI_TEST_DIR"] = "/chosen"
        try:
            self.assertEqual(server.first_dir("LSYNCD_WEBUI_TEST_DIR", [TMP.name]), "/chosen")
        finally:
            del os.environ["LSYNCD_WEBUI_TEST_DIR"]


class TransferTest(unittest.TestCase):
    def test_full_sync_reports_sent_files_without_logging_them(self):
        jobs = server.Jobs()
        jobs.info = {"folder": "Photos"}
        jobs.progress = server.new_progress()
        before = list(server.LOG)
        jobs._on_progress("@lsw@ <f+++++++++ 1234 1234 2024/img 1.jpg")
        jobs._on_progress("@lsw@ cd+++++++++ 0 0 2024/")
        jobs._on_progress("@lsw@ *deleting   0 0 old.jpg")
        self.assertEqual(list(server.LOG), before)
        last = server.transfer.last_file
        self.assertEqual((last["path"], last["size"], last["direction"]), ("Photos/2024/img 1.jpg", 1234, "to backup"))

    def test_lsyncd_lines_name_their_folder(self):
        daemon = server.Lsyncd()
        daemon.synced = ["Documents", "Photos"]
        self.assertTrue(daemon.on_line("@lsw@1 <f.st...... 10 3 a.txt"))
        self.assertEqual(server.transfer.last_file["path"], "Photos/a.txt")
        self.assertFalse(daemon.on_line("Normal: Calling rsync with filter-list of new/modified files/dirs"))

    def test_unison_reports_direction(self):
        daemon = server.Unison()
        daemon.on_line("[BGN] Copying Photos/x y.jpg from //backup-target//backup to /data-rw")
        daemon.on_line("[END] Copying Photos/x y.jpg")
        self.assertEqual(server.transfer.last_file["path"], "Photos/x y.jpg")
        self.assertEqual(server.transfer.last_file["direction"], "to main")
        daemon.on_line("[BGN] Updating file Photos/z from /data-rw to //backup-target//backup")
        daemon.on_line("[END] Updating file Photos/z")
        self.assertEqual(server.transfer.last_file["direction"], "to backup")

    def test_speed_counts_what_ssh_writes(self):
        # A process named ssh that writes what it reads, like ssh writes to the network.
        # A script, not a link to cat: multi-call coreutils (uutils) refuse to run under another name.
        fake = os.path.join(TMP.name, "bin", "ssh")
        os.makedirs(os.path.dirname(fake), exist_ok=True)
        with open(fake, "w") as f:
            f.write(f"#!{sys.executable}\nimport os\nout = os.open(os.devnull, os.O_WRONLY)\n"
                    "while data := os.read(0, 1 << 16):\n    os.write(out, data)\n")
        os.chmod(fake, 0o755)
        monitor = server.Transfer()
        monitor.sample()
        proc = subprocess.Popen([fake], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        try:
            monitor.sample()
            proc.stdin.write(bytes(5 << 20))
            proc.stdin.flush()
            time.sleep(0.3)
            monitor.sample()
        finally:
            proc.stdin.close()
            proc.wait()
        self.assertGreaterEqual(monitor.total, 5 << 20)
        self.assertGreater(monitor.speed(), 0)


class SpeedTestTest(unittest.TestCase):
    def test_each_link_is_measured(self):
        self.assertGreater(speedtest.network(run_here, "127.0.0.1", seconds=0.3)["bps"], 0)
        self.assertGreater(speedtest.ssh_pipe(run_here, "aes128-gcm@openssh.com", seconds=0.3)["bps"], 0)
        with tempfile.TemporaryDirectory() as target:
            disk = speedtest.remote_disk(run_here, target, size_mb=8)
            self.assertEqual(disk["bytes"], 8 << 20)
            self.assertGreater(disk["bps"], 0)
            self.assertEqual(os.listdir(target), [])
        self.assertIn("error", speedtest.remote_disk(run_here, "/does/not/exist", size_mb=1))

    def test_failures_are_reported(self):
        failing = lambda command, options=(): ["sh", "-c", "echo 'no matching cipher found' >&2; exit 255"]
        self.assertEqual(speedtest.ssh_pipe(failing, "x", seconds=0.3)["error"], "no matching cipher found")
        self.assertIn("error", speedtest.network(failing, "127.0.0.1", seconds=0.3))

    def test_verdict(self):
        self.assertEqual(speedtest.verdict(110e6, 100e6, 30e6), "disk")
        self.assertEqual(speedtest.verdict(110e6, 40e6, 200e6), "encryption")
        self.assertEqual(speedtest.verdict(25e6, 24e6, 200e6), "network")
        self.assertEqual(speedtest.verdict(None, 24e6, None), "network")
        self.assertEqual(speedtest.verdict(110e6, 40e6, 200e6, encrypted=False), "transport")

    def test_fastest_cipher_goes_into_the_ssh_config(self):
        os.makedirs(server.SSH_DIR, exist_ok=True)
        s, _ = server.validate({"host": "10.0.0.2"})
        try:
            server.update_state(speedtest={"cipher": "aes128-gcm@openssh.com"})
            server.write_ssh_config(s)
            with open(server.SSH_CONFIG) as f:
                self.assertIn("    Ciphers ^aes128-gcm@openssh.com\n", f.read())
            server.update_state(speedtest={"cipher": "x\n    ProxyCommand evil"})
            server.write_ssh_config(s)
            with open(server.SSH_CONFIG) as f:
                text = f.read()
            self.assertNotIn("Ciphers", text)
            self.assertNotIn("ProxyCommand", text)
        finally:
            server.update_state(speedtest=None)


class PlainTransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(dir=TMP.name)
        for name in ("good", "bad"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", os.path.join(cls.dir, name)], check=True)
        with open(os.path.join(cls.dir, "good.pub")) as f:
            good = f.read().strip()
        signers = os.path.join(cls.dir, "signers")
        with open(signers, "w") as f:
            f.write(plain.allowed_signers(['from="10.0.0.1" ' + good]))
        cls.server = plain.Server(0, signers, log=lambda message: None)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.key = os.path.join(cls.dir, "good")
        cls.conf = os.path.join(cls.dir, "plain.json")
        with open(cls.conf, "w") as f:
            f.write('{"host": "127.0.0.1", "port": %d, "key": "%s"}' % (cls.server.port, cls.key))

    def client(self, *args, data=b""):
        return subprocess.run([sys.executable, os.path.join(APP_DIR, "plain.py"), "client", self.conf, *args],
                              input=data, capture_output=True, timeout=30)

    def test_command_is_found_like_ssh_does(self):
        cases = [
            (["-F", "cfg", "backup-target", "rsync", "--server", "."], "rsync --server ."),
            (["-l", "root", "-p2222", "backup-target", "-e", "none", "unison", "-server"], "unison -server"),
            (["-c", "aes128-gcm@openssh.com", "-o", "Compression=no", "-q", "backup-target", "cat > /dev/null"], "cat > /dev/null"),
            (["backup-target", "--", "ls"], "ls"),
            (["-v", "backup-target"], ""),
        ]
        for args, command in cases:
            self.assertEqual(plain.command_from_ssh_args(args), command, args)

    def test_output_errors_and_exit_code_come_back(self):
        code, out, err = plain.call("127.0.0.1", self.server.port, self.key, "echo connected; echo oops >&2; exit 3")
        self.assertEqual((code, out, err), (3, "connected\n", "oops\n"))

    def test_only_the_allowed_key_gets_in(self):
        with self.assertRaises(plain.PlainError) as caught:
            plain.call("127.0.0.1", self.server.port, os.path.join(self.dir, "bad"), "echo hi")
        self.assertIn("AUTHORIZED_KEY", str(caught.exception))

    def test_closed_port_is_explained(self):
        with self.assertRaises(plain.PlainError) as caught:
            plain.call("127.0.0.1", 1, self.key, "echo hi")
        self.assertIn("up to date", str(caught.exception))

    def test_client_passes_input_through_like_ssh(self):
        result = self.client("-F", "x", "backup-target", "wc", "-c", data=b"x" * 3_000_000)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, b"3000000"))
        self.assertEqual(self.client("backup-target", "exit 24").returncode, 24)
        self.assertEqual(self.client("backup-target", "head -c 5000000 /dev/zero").stdout, bytes(5_000_000))

    def test_non_blocking_pipes_like_rsync_hands_out(self):
        # A slow reader on a non-blocking stdout, and input that arrives in pieces on a non-blocking stdin
        out_r, out_w = os.pipe()
        in_r, in_w = os.pipe()
        os.set_blocking(out_w, False)
        os.set_blocking(in_r, False)
        proc = subprocess.Popen([sys.executable, os.path.join(APP_DIR, "plain.py"), "client", self.conf,
                                 "backup-target", "wc -c; head -c 3000000 /dev/zero"], stdin=in_r, stdout=out_w)
        os.close(out_w)
        os.close(in_r)
        for _ in range(3):
            time.sleep(0.2)
            os.write(in_w, b"x" * 1000)
        os.close(in_w)
        time.sleep(0.5)  # let the output pile up first
        received = b""
        while chunk := os.read(out_r, 1 << 16):
            received += chunk
        os.close(out_r)
        self.assertEqual(proc.wait(timeout=30), 0)
        self.assertEqual(received, b"3000\n" + bytes(3_000_000))

    def test_wrapper_uses_plain_only_when_switched_on(self):
        s, _ = server.validate({"host": "127.0.0.1", "encryption": "none", "plain_port": self.server.port})
        server.ensure_key()
        server.write_ssh_config(s)
        try:
            with open(server.PLAIN_CONFIG) as f:
                self.assertEqual(f.read().count(str(self.server.port)), 1)
            result = subprocess.run(["ssh", "backup-target", "echo", "via", "plain"], capture_output=True, text=True, timeout=30,
                                    env={**os.environ, "LSYNCD_WEBUI_REAL_SSH": "/bin/false"})
            # The wrapper signs with the container's own key, which this test server does not know
            self.assertIn("AUTHORIZED_KEY", result.stderr)
            self.assertEqual(server.check_plain(s)[:31], "Encryption was not turned off. ")
        finally:
            server.write_ssh_config(server.validate({"host": "127.0.0.1"})[0])
        self.assertFalse(os.path.exists(server.PLAIN_CONFIG))

    def test_settings(self):
        s, errors = server.validate({"encryption": "none", "plain_port": 70000})
        self.assertEqual((s["encryption"], s["plain_port"], len(errors)), ("none", 2223, 1))
        self.assertEqual(server.validate({"encryption": "rot13"})[0]["encryption"], "ssh")

    def test_transfer_monitor_sees_the_plain_client(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)", "plain.py", "client"])
        try:
            time.sleep(0.2)
            self.assertIn(str(proc.pid), [pid for pid, _ in server.transport_counters()])
        finally:
            proc.kill()
            proc.wait()


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
