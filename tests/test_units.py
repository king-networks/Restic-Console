"""Unit tests: schedules, paths, run records. Standard library only: python -m unittest discover tests"""
import datetime as dt
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_tmp = tempfile.mkdtemp(prefix="rc-unit-")
os.environ.update(RC_DEV="1", RC_ETC=os.path.join(_tmp, "etc"), RC_STATE=os.path.join(_tmp, "state"))

import common  # noqa: E402
import runs  # noqa: E402
import schedule  # noqa: E402


class ScheduleTest(unittest.TestCase):
    def test_parse_builder_output(self):
        cases = {
            "*-*-* *:00/15:00": {"kind": "minutes", "every": 15},
            "*-*-* 00/6:30:00": {"kind": "hours", "every": 6, "minute": 30},
            "*-*-* 00/1:00:00": {"kind": "hours", "every": 1, "minute": 0},
            "*-*-* 02:05:00": {"kind": "daily", "hour": 2, "minute": 5},
            "Sun *-*-* 02:00:00": {"kind": "weekly", "days": [6], "hour": 2, "minute": 0},
            "Mon..Wed,Fri *-*-* 23:59:00": {"kind": "weekly", "days": [0, 1, 2, 4], "hour": 23, "minute": 59},
            "Mon..Sun *-*-* 01:00:00": {"kind": "daily", "hour": 1, "minute": 0},
        }
        for expr, want in cases.items():
            with self.subTest(expr=expr):
                self.assertEqual(schedule.parse(expr), want)

    def test_parse_rejects_non_portable(self):
        for expr in ["*-*-* *:00/7:00", "*-*-* 00/5:00:00", "Mon *-*-01 02:00:00", "daily", "*-*-* 25:00:00", "Xyz *-*-* 01:00:00"]:
            with self.subTest(expr=expr):
                self.assertIsNone(schedule.parse(expr))

    def test_normalize_round_trip(self):
        for expr in ["*-*-* *:00/15:00", "*-*-* 00/6:30:00", "*-*-* 02:05:00", "Mon,Tue,Fri *-*-* 02:00:00"]:
            self.assertEqual(schedule.parse(schedule.normalize(schedule.parse(expr))), schedule.parse(expr))

    def test_next_times(self):
        start = dt.datetime(2026, 1, 5, 10, 7)           # a Monday
        s = schedule.parse("*-*-* *:00/15:00")
        self.assertEqual([t.strftime("%H:%M") for t in schedule.next_times(s, 3, start)], ["10:15", "10:30", "10:45"])
        s = schedule.parse("*-*-* 00/6:30:00")
        self.assertEqual([t.strftime("%d %H:%M") for t in schedule.next_times(s, 3, start)], ["05 12:30", "05 18:30", "06 00:30"])
        s = schedule.parse("Sun,Wed *-*-* 02:00:00")
        self.assertEqual([t.strftime("%a %d") for t in schedule.next_times(s, 3, start)], ["Wed 07", "Sun 11", "Wed 14"])

    def test_windows_task_xml_is_valid(self):
        trig = [schedule.win_trigger(schedule.parse(e), 5, dt.date(2026, 1, 1))
                for e in ["*-*-* *:00/15:00", "*-*-* 00/6:30:00", "*-*-* 02:00:00", "Mon,Fri *-*-* 03:00:00"]]
        xml = schedule.win_task_xml("job <x> & y", trig, r"C:\Python\pythonw.exe", r'-I "C:\Program Files\x\runner.py" job',
                                    r"C:\Program Files\x")
        root = ET.fromstring(xml.split("?>", 1)[1])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        trs = root.findall("t:Triggers/t:CalendarTrigger", ns)
        self.assertEqual(len(trs), 4)
        self.assertEqual(trs[0].find("t:Repetition/t:Interval", ns).text, "PT15M")
        self.assertEqual(trs[1].find("t:StartBoundary", ns).text, "2026-01-01T00:30:00")
        self.assertEqual(trs[1].find("t:Repetition/t:Interval", ns).text, "PT6H")
        self.assertEqual(trs[2].find("t:RandomDelay", ns).text, "PT5M")
        self.assertIsNotNone(trs[3].find("t:ScheduleByWeek/t:DaysOfWeek/t:Friday", ns))
        self.assertEqual(root.find("t:Settings/t:ExecutionTimeLimit", ns).text, "PT0S")
        self.assertEqual(root.find("t:Principals/t:Principal/t:UserId", ns).text, "S-1-5-18")


class PathTest(unittest.TestCase):
    def test_snapshot_paths(self):
        self.assertEqual(common.check_snapshot_path("/a//b/./c/../d/"), "/a/b/d")
        self.assertEqual(common.check_snapshot_path("/../../etc"), "/etc")
        self.assertEqual(common.check_snapshot_path("/C/Users/x"), "/C/Users/x")
        for bad in ["relative", "", "/a\0b", "/a\nb", None]:
            with self.assertRaises(ValueError):
                common.check_snapshot_path(bad)

    def test_local_paths(self):
        if common.IS_WIN:
            good, bad = [r"C:\Users\x", "D:/backup", r"\\nas\share\restic"], ["/home/x", "relative", r"\nodrive", "sftp:h:/x", "C:"]
        else:
            good, bad = ["/home/x", "/mnt/backup"], ["relative", "sftp:host:/x", "", r"C:\x"]
        for p in good:
            self.assertTrue(common.is_local_path(p), p)
            common.check_local(p)
        for p in bad:
            self.assertFalse(common.is_local_path(p), p)
            with self.assertRaises(ValueError):
                common.check_local(p)


class RunsTest(unittest.TestCase):
    def test_lifecycle(self):
        r = runs.RunLog("unit", echo=False)
        r.line("Files:           3 new,     0 changed,     0 unmodified")
        r.line("Added to the repository: 1.234 KiB (900 B stored)")
        r.line("snapshot 1a2b3c4d saved")
        self.assertEqual(runs.running("unit")["id"], r.id)
        r.finish("success")
        self.assertIsNone(runs.running("unit"))
        rec = runs.list_runs("unit")[0]
        self.assertEqual((rec["status"], rec["snapshot"], rec["files"][0]), ("success", "1a2b3c4d", "3"))
        log = runs.read_log("unit", r.id)
        self.assertEqual(len(log["lines"]), 3)
        self.assertEqual(runs.read_log("unit", r.id, log["cursor"])["lines"], [])

    def test_dead_run_is_reaped_once(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        r = runs.RunLog("dead", echo=False)
        r.rec["pid"] = p.pid
        runs.write(r.rec)
        r.f.close()
        self.assertEqual(runs.list_runs("dead", grace=3600)[0]["status"], "unknown")    # still in grace period
        reaped = []
        rec = runs.list_runs("dead", reap=lambda x: reaped.append(x) or runs.finish_external(x, "failed", "dead"), grace=0)[0]
        self.assertEqual((rec["status"], len(reaped)), ("failed", 1))
        runs.list_runs("dead", reap=lambda x: reaped.append(x), grace=0)
        self.assertEqual(len(reaped), 1)

    def test_bad_run_id(self):
        with self.assertRaises(ValueError):
            runs.read_log("x", "../../etc/passwd")


if __name__ == "__main__":
    unittest.main()
