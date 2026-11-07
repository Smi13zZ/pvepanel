#!/usr/bin/env python3
"""Live check: start the real HTTP service and exercise /api/storage.

Uses a throwaway var dir under tmp/ and a port nobody else has, so it never
touches the installed panel. The point is to prove the route, the JSON shape
and the 409 path, not to re-test storage.py (test_storage.py does that).
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
VAR = os.path.join(ROOT, "tmp", "_unit", "storage_live", "var")
PORT = "39411"

if os.path.isdir(os.path.dirname(VAR)):
    shutil.rmtree(os.path.dirname(VAR), ignore_errors=True)
os.makedirs(VAR, exist_ok=True)

# A cache with three packages and an interrupted download, so cleanup has work.
upd = os.path.join(VAR, "update")
os.makedirs(upd, exist_ok=True)
for i, ver in enumerate(("26.11.1", "26.11.2", "26.11.3")):
    p = os.path.join(upd, "fn-pvepanel-%s-fnos-all.fpk" % ver)
    open(p, "wb").write(b"x" * (100 + i))
    os.utime(p, (1000 + i, 1000 + i))
open(os.path.join(upd, "fn-pvepanel-26.11.9-fnos-all.fpk.part"), "wb").write(b"y" * 50)
open(os.path.join(upd, "install.log"), "wb").write(b"log line\n" * 20)
open(os.path.join(VAR, "pvepanel.log"), "wb").write(b"panel\n" * 30)
open(os.path.join(VAR, "pvepanel.log.1"), "wb").write(b"old\n" * 10)

env = dict(os.environ)
env["TRIM_PKGVAR"] = VAR
env["PVEPANEL_VERSION"] = "26.11.99"
env["PVEPANEL_PORT"] = PORT
env["PVEPANEL_LISTEN"] = "127.0.0.1"
env["PYTHONPATH"] = os.path.join(ROOT, "src", "fn-pvepanel", "app", "server")

# stdout goes to a file, not a pipe: nothing drains a pipe while the assertions
# run, and a server that logs a lot would fill the buffer and block forever.
LOG_PATH = os.path.join(ROOT, "tmp", "_unit", "storage_live.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
_logf = open(LOG_PATH, "w", encoding="utf-8")
srv = subprocess.Popen(
    [sys.executable, os.path.join(ROOT, "src", "fn-pvepanel", "app", "server", "pvepanel.py")],
    env=env, stdout=_logf, stderr=subprocess.STDOUT, cwd=ROOT)

BASE = "http://127.0.0.1:" + PORT
FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % name)
        return True
    FAILS.append("%s %s" % (name, detail))
    print("  FAIL %s %s" % (name, detail))
    return False


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def post(path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


try:
    # wait for the listener
    ok = False
    for _ in range(60):
        try:
            get("/api/health")
            ok = True
            break
        except Exception:
            time.sleep(0.25)
    if not ok:
        print("server did not start; log follows")
        try:
            _logf.flush()
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                print(f.read()[-4000:])
        except OSError:
            pass
        sys.exit(1)

    print("[GET /api/storage]")
    code, doc = get("/api/storage")
    check("returns 200", code == 200, "code %r" % code)
    check("ok is true", doc.get("ok") is True, "doc %r" % (doc.get("ok"),))
    for key in ("items", "total", "var_dir", "updates", "updates_keep", "current_version"):
        check("has key %r" % key, key in doc, "keys %s" % sorted(doc))
    check("items carry key/label/bytes/files/path",
          all(set(("key", "label", "bytes", "files", "path")) <= set(it) for it in doc["items"]),
          "items %r" % doc["items"][:1])
    check("updates_keep is 2", doc.get("updates_keep") == 2, "keep %r" % doc.get("updates_keep"))
    check("current_version is the running build",
          doc.get("current_version") == "26.11.99", "cur %r" % doc.get("current_version"))
    keys = [it["key"] for it in doc["items"]]
    for want in ("updates", "backups", "panel_log", "panel_log1", "update_log"):
        check("item %r present" % want, want in keys, "keys %s" % keys)
    check("updates list has 4 entries (3 packages + 1 partial)",
          len(doc["updates"]) == 4, "updates %r" % [u["name"] for u in doc["updates"]])
    partials = [u for u in doc["updates"] if u["partial"]]
    check("the .part is flagged partial", len(partials) == 1,
          "partials %r" % partials)
    check("updates entries carry name/bytes/t/partial",
          all(set(("name", "bytes", "t", "partial")) <= set(u) for u in doc["updates"]))

    print("[POST /api/storage/cleanup target=updates]")
    code, res = post("/api/storage/cleanup", {"target": "updates", "keep": 2})
    check("returns 200", code == 200, "code %r body %r" % (code, res))
    check("ok is true", res.get("ok") is True, "res %r" % res)
    check("freed a positive number of bytes", (res.get("freed") or 0) > 0,
          "freed %r" % res.get("freed"))
    left = sorted(n for n in os.listdir(upd) if n.endswith(".fpk") or ".fpk." in n)
    check("keep=2 honoured", len(left) == 2, "left %s" % left)
    check("the .part is gone", not any(n.endswith(".part") for n in left), "left %s" % left)
    check("running version 26.11.99 has no package so nothing is protected here",
          len(left) == 2, "left %s" % left)
    check("a fresh report came back with the cleanup",
          isinstance(res.get("report"), dict), "report %r" % type(res.get("report")))

    print("[POST /api/storage/cleanup target=logs]")
    code, res = post("/api/storage/cleanup", {"target": "logs"})
    check("returns 200", code == 200, "code %r" % code)
    check("panel log truncated to 0", os.path.getsize(os.path.join(VAR, "pvepanel.log")) == 0)
    check("rotated log removed", not os.path.exists(os.path.join(VAR, "pvepanel.log.1")))
    check("installer log truncated",
          os.path.getsize(os.path.join(upd, "install.log")) == 0)

    print("[POST /api/storage/cleanup invalid target]")
    code, res = post("/api/storage/cleanup", {"target": "everything"})
    check("invalid target is refused", res.get("ok") is False, "res %r" % res)

    print("[POST /api/storage/cleanup while installing -> 409]")
    sys.path.insert(0, os.path.join(ROOT, "src", "fn-pvepanel", "app", "server"))
    import storage
    orig = storage.installer_busy
    storage.installer_busy = lambda: True
    try:
        code, res = post("/api/storage/cleanup", {"target": "all"})
        check("busy cleanup answers 409", code == 409, "code %r body %r" % (code, res))
        check("409 body says busy", res.get("busy") is True, "res %r" % res)
        check("409 body carries an error message", bool(res.get("error")), "res %r" % res)
    finally:
        storage.installer_busy = orig
finally:
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except subprocess.TimeoutExpired:
        srv.kill()
    try:
        _logf.close()
    except Exception:
        pass
    shutil.rmtree(os.path.join(ROOT, "tmp", "_unit", "storage_live"), ignore_errors=True)

print("=" * 62)
if FAILS:
    print("FAILED %d / %d" % (len(FAILS), CHECKS[0]))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("passed %d / %d" % (CHECKS[0], CHECKS[0]))
