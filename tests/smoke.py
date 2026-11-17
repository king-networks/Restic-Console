#!/usr/bin/env python3
"""Smoke test against an INSTALLED Restic Console (used by CI right after the installer).

    sudo python3 tests/smoke.py --workdir DIR        (Linux)
    python tests\\smoke.py --workdir DIR             (Windows, elevated)

Creates a repository and a job under DIR, starts the job through systemd / Task Scheduler
("Run now"), waits for it, and checks run history, snapshots, restore and the schedule.
Needs admin rights to read the token. Leaves the job and destination behind for inspection.
"""
import argparse
import http.client
import json
import os
import sys
import time
import uuid
from urllib.parse import quote

IS_WIN = os.name == "nt"
TOKEN = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "ResticConsole", "token") if IS_WIN \
    else "/etc/restic-console/token"
PORT = 8790
cookie = ""


def req(method, path, body=None, headers=None):
    global cookie
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=120)
    h = {"Cookie": cookie}
    if body is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse()
    data = r.read()
    if r.getheader("Set-Cookie"):
        cookie = r.getheader("Set-Cookie").split(";")[0]
    try:
        return r.status, json.loads(data or b"null")
    except ValueError:
        return r.status, data


def ok(method, path, body=None):
    st, j = req(method, path, body)
    if st != 200:
        sys.exit(f"FAIL {method} {path}: {st} {j}")
    return j


def wait(cond, what, timeout=300):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(2)
    sys.exit(f"FAIL timed out waiting for {what}")


def wait_task(tid):
    t = wait(lambda: (lambda t: t if t["status"] not in ("running", "queued") else None)(ok("GET", f"/api/task?id={tid}")), "task")
    if t["status"] != "done":
        sys.exit("FAIL task:\n" + "\n".join(t["lines"]))
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    a = ap.parse_args()
    work = os.path.abspath(a.workdir)
    src = os.path.join(work, "src")
    os.makedirs(os.path.join(src, "sub"), exist_ok=True)
    with open(os.path.join(src, "sub", "hello.txt"), "w") as f:
        f.write("smoke\n")
    tag = uuid.uuid4().hex[:6]

    with open(TOKEN) as f:
        token = f.read().strip()
    st, j = req("POST", "/api/handoff", {}, {"X-Token": token})
    assert st == 200, (st, j)
    ok("POST", "/api/login", {"code": j["code"]})
    plat = ok("GET", "/api/config")["platform"]
    print(f"logged in: Restic Console {plat['version']} on {plat['name']}")

    did, jid = f"smoke-{tag}", f"smoke-{tag}"
    wait_task(ok("POST", "/api/destination/add", {"id": did, "name": "Smoke", "repo": os.path.join(work, "repo"), "mode": "create"})["id"])
    print("repository created")
    ok("POST", "/api/job/save", {"id": jid, "new": True, "name": "Smoke", "destination": did, "sources": [src],
                                 "retention": {"last": 2}, "schedule": {"calendars": ["*-*-* 03:00:00"]}})
    st = ok("GET", "/api/status")
    job = next(x for x in st["jobs"] if x["id"] == jid)
    assert job["next"], f"no next run time: the schedule was not installed ({job})"
    print(f"job created, next run {time.ctime(job['next'])}")

    ok("POST", "/api/job/run", {"id": jid})
    print("Run now: started through the scheduler")
    runs = wait(lambda: [r for r in ok("GET", f"/api/runs?job={jid}") if r["status"] not in ("running", "unknown")],
                "the backup run", 600)
    log = ok("GET", f"/api/runlog?job={jid}&id={runs[0]['id']}")
    text = "\n".join(line["m"] for line in log["lines"])
    if runs[0]["status"] != "success":
        sys.exit(f"FAIL run status {runs[0]['status']}:\n{text}")
    print(f"backup succeeded: snapshot {runs[0].get('snapshot')}, added {runs[0].get('added')}")

    snaps = ok("GET", f"/api/snapshots?dest={did}&refresh=1")
    assert len(snaps) == 1, snaps
    sid = snaps[0]["id"]
    def find(path, depth=0):      # walk the snapshot from / like the UI does
        for n in ok("GET", f"/api/ls?dest={did}&snapshot={sid}&path={quote(path)}"):
            if n["name"] == "hello.txt":
                return n
            if n["type"] == "dir" and depth < 25:
                hit = find(n["path"], depth + 1)
                if hit:
                    return hit
        return None

    node = find("/")
    assert node, "hello.txt not found in the snapshot"
    print(f"browse ok: {node['path']}")

    roots = ok("GET", "/api/config")["settings"]["restore_roots"]
    target = os.path.join(roots[0], f"rc-smoke-restore-{tag}")
    wait_task(ok("POST", "/api/tasks", {"kind": "restore", "dest": did, "snapshot": sid, "path": node["path"],
                                        "type": "file", "target": target})["id"])
    got = [os.path.join(d, f) for d, _, fs in os.walk(target) for f in fs]
    assert len(got) == 1 and open(got[0]).read() == "smoke\n", got
    print(f"restore ok: {got[0]}")

    st, j = req("POST", "/api/notify/test", {"channel": "desktop"})
    print(f"desktop notification test (informational, CI has no desktop): {st} {j}")
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
