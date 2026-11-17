"""End to end: real server (dev mode) + real restic + the job runner, in a temp directory.

Skipped when restic is not installed. Nothing outside the temp directory is touched.
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESTIC = os.environ.get("RC_RESTIC") or shutil.which("restic")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(RESTIC, "restic is not installed")
class IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rc-it-")
        t = lambda *p: os.path.join(cls.tmp, *p)  # noqa: E731
        cls.t = staticmethod(t)
        os.makedirs(t("src", "docs"))
        with open(t("src", "docs", "hello.txt"), "w") as f:
            f.write("hello restic\n")
        with open(t("src", "top.txt"), "w") as f:
            f.write("top\n")
        os.makedirs(t("restore"))
        cls.port = free_port()
        cls.env = dict(os.environ, RC_DEV="1", RC_PORT=str(cls.port), RC_ETC=t("etc"), RC_STATE=t("state"),
                       RC_UNIT_DIR=t("units"), RC_RESTIC=RESTIC, RESTIC_CACHE_DIR=t("cache"))
        subprocess.run([sys.executable, os.path.join(ROOT, "server.py"), "init", "--restore-root", t("restore"),
                        "--restic", RESTIC], env=cls.env, check=True)
        with open(t("etc", "token")) as f:
            cls.token = f.read().strip()
        cls.srv = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "server.py")], env=cls.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.2)
        cls.cookie = ""

    @classmethod
    def tearDownClass(cls):
        cls.srv.terminate()
        cls.srv.wait(10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, body=None, headers=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        h = {"Host": f"127.0.0.1:{self.port}", "Cookie": self.cookie}
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        if "Set-Cookie" in r.headers:
            type(self).cookie = r.headers["Set-Cookie"].split(";")[0]
        return (r.status, data) if raw else (r.status, json.loads(data or b"null"))

    def ok(self, method, path, body=None):
        st, j = self.req(method, path, body)
        self.assertEqual(st, 200, j)
        return j

    def wait_task(self, tid):
        for _ in range(300):
            t = self.ok("GET", f"/api/task?id={tid}")
            if t["status"] not in ("running", "queued"):
                self.assertEqual(t["status"], "done", "\n".join(t["lines"]))
                return t
            time.sleep(0.2)
        self.fail("task timed out")

    def find(self, dest, sid, name, path="/", depth=0):
        """Walk the snapshot from / like the UI does, return the node called `name`."""
        for n in self.ok("GET", f"/api/ls?dest={dest}&snapshot={sid}&path={quote(path)}"):
            if n["name"] == name:
                return n
            if n["type"] == "dir" and depth < 20:
                hit = self.find(dest, sid, name, n["path"], depth + 1)
                if hit:
                    return hit
        return None

    def test_end_to_end(self):
        t = self.t
        # security checks
        self.assertEqual(self.req("GET", "/api/status")[0], 401)
        self.assertEqual(self.req("GET", "/", headers={"Host": "evil.example:80"}, raw=True)[0], 403)
        self.assertEqual(self.req("POST", "/api/login", {"token": "wrong"})[0], 401)
        self.assertEqual(self.req("POST", "/api/handoff", {}, headers={"X-Token": "wrong"})[0], 401)
        st, j = self.req("POST", "/api/handoff", {}, headers={"X-Token": self.token})
        self.assertEqual(st, 200)
        self.ok("POST", "/api/login", {"code": j["code"]})
        self.assertEqual(self.req("POST", "/api/login", {"code": j["code"]})[0], 401, "one-time code reused")
        hello = self.ok("GET", "/api/hello")["platform"]
        self.assertIn(hello["name"], ("linux", "windows"))

        # destination + job
        task = self.ok("POST", "/api/destination/add", {"id": "local", "name": "Local", "repo": t("repo"), "mode": "create"})
        self.wait_task(task["id"])
        self.assertIn("local", self.ok("GET", "/api/config")["destinations"])
        job = {"id": "docs", "new": True, "name": "Docs", "destination": "local", "sources": [t("src")],
               "retention": {"last": 3}, "schedule": {"calendars": ["*-*-* 02:00:00"]}, "excludes": "*.tmp\n"}
        self.ok("POST", "/api/job/save", job)
        self.assertEqual(self.req("POST", "/api/job/save", job)[0], 400, "duplicate job id accepted")
        bad = dict(job, id="bad", new=True, sources=["relative/path"])
        self.assertEqual(self.req("POST", "/api/job/save", bad)[0], 400)

        # a backup run, as the scheduler would start it
        r = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "runner.py"), "docs"], env=self.env,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        runs = self.ok("GET", "/api/runs?job=docs")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "success", runs[0])
        log = self.ok("GET", f"/api/runlog?job=docs&id={runs[0]['id']}")
        self.assertTrue(any("DONE" in line["m"] for line in log["lines"]))
        status = self.ok("GET", "/api/status")
        self.assertEqual(status["jobs"][0]["last"]["status"], "success")
        self.assertTrue(status["destinations"][0]["health"]["reachable"], status["destinations"][0])

        # browse, download, restore
        snaps = self.ok("GET", "/api/snapshots?dest=local&refresh=1")
        self.assertEqual(len(snaps), 1)
        sid = snaps[0]["id"]
        node = self.find("local", sid, "hello.txt")
        self.assertIsNotNone(node)
        st, data = self.req("GET", f"/api/dump?dest=local&snapshot={sid}&path={quote(node['path'])}", raw=True)
        self.assertEqual((st, data.replace(b"\r\n", b"\n")), (200, b"hello restic\n"))
        docs = self.find("local", sid, "docs")

        tgt = t("restore", "r1")
        task = self.ok("POST", "/api/tasks", {"kind": "restore", "dest": "local", "snapshot": sid, "path": docs["path"],
                                              "type": "dir", "target": tgt})
        self.wait_task(task["id"])
        found = [os.path.join(d, f) for d, _, fs in os.walk(tgt) for f in fs]
        self.assertEqual([os.path.basename(f) for f in found], ["hello.txt"], found)

        tgt = t("restore", "r2")
        task = self.ok("POST", "/api/tasks", {"kind": "restore", "dest": "local", "snapshot": sid, "path": node["path"],
                                              "type": "file", "target": tgt})
        self.wait_task(task["id"])
        found = [os.path.join(d, f) for d, _, fs in os.walk(tgt) for f in fs]
        self.assertEqual(len(found), 1, found)
        with open(found[0]) as f:
            self.assertEqual(f.read(), "hello restic\n")

        st, j = self.req("POST", "/api/tasks", {"kind": "restore", "dest": "local", "snapshot": sid, "path": "/",
                                                "type": "dir", "target": t("outside")})
        self.assertEqual(st, 400, "restore outside the restore roots was accepted")

        # maintenance task and logout
        self.wait_task(self.ok("POST", "/api/tasks", {"kind": "check", "dest": "local"})["id"])
        self.ok("POST", "/api/logout", {})
        self.assertEqual(self.req("GET", "/api/status")[0], 401)


if __name__ == "__main__":
    unittest.main()
