#!/usr/bin/env python3
"""Live check: start the real HTTP service and exercise /api/storage.

Uses a throwaway var dir under tmp/ and a port nobody else has, so it never
touches the installed panel. The point is to prove the route, the JSON shape
and the 409 path, not to re-test storage.py (test_storage.py does that).

Two things this harness has to respect, both of which it got wrong before and
therefore never ran:

  * ROOT is the repository, not this script's folder. Taking it from __file__
    alone produced tools/src/fn-pvepanel/... and the service could never start,
    which is why the file sat broken instead of failing loudly.
  * The service trims the package cache on its own at startup, in the
    background. Fixtures created before the process starts are therefore gone
    by the time the first assertion runs - that cleanup is a feature, so the
    harness verifies it rather than fighting it, then lays down fresh files for
    the explicit-cleanup half.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS_DIR)
SERVER = os.path.join(ROOT, "src", "fn-pvepanel", "app", "server")
BASE_DIR = os.path.join(ROOT, "tmp", "_unit", "storage_live")
VAR = os.path.join(BASE_DIR, "var")
PORT = "39411"
BASE = "http://127.0.0.1:" + PORT
UPD = os.path.join(VAR, "update")

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


def write_pkg(version, size, mtime=None):
    path = os.path.join(UPD, "fn-pvepanel-%s-fnos-all.fpk" % version)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def main():
    if os.path.isdir(BASE_DIR):
        shutil.rmtree(BASE_DIR, ignore_errors=True)
    os.makedirs(UPD, exist_ok=True)
    # Fixtures the STARTUP PASS should clean down to two packages.
    for i, ver in enumerate(("26.11.1", "26.11.2", "26.11.3")):
        write_pkg(ver, 100 + i, mtime=1000 + i)
    with open(os.path.join(UPD, "fn-pvepanel-26.11.9-fnos-all.fpk.part"), "wb") as f:
        f.write(b"y" * 50)
    with open(os.path.join(UPD, "install.log"), "wb") as f:
        f.write(b"log line\n" * 20)
    with open(os.path.join(VAR, "pvepanel.log"), "wb") as f:
        f.write(b"panel\n" * 30)
    with open(os.path.join(VAR, "pvepanel.log.1"), "wb") as f:
        f.write(b"old\n" * 10)
    os.makedirs(os.path.join(VAR, "backups"), exist_ok=True)
    with open(os.path.join(VAR, "backups", "keep.json"), "w",
              encoding="utf-8") as f:
        json.dump({"x": 1}, f)

    env = dict(os.environ)
    env["TRIM_PKGVAR"] = VAR
    env["PVEPANEL_VERSION"] = "26.11.99"
    env["PVEPANEL_PORT"] = PORT
    env["PVEPANEL_LISTEN"] = "127.0.0.1"
    env["PYTHONPATH"] = SERVER

    # stdout goes to a file, not a pipe: nothing drains a pipe while the
    # assertions run, and a server that logs a lot would fill the buffer and
    # block forever.
    LOG_PATH = os.path.join(BASE_DIR, "storage_live.log")
    _logf = open(LOG_PATH, "w", encoding="utf-8")
    srv = subprocess.Popen(
        [sys.executable, os.path.join(SERVER, "pvepanel.py")],
        env=env, stdout=_logf, stderr=subprocess.STDOUT, cwd=ROOT)

    try:
        ok = False
        for _ in range(80):
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
            return 1

        # The startup purge runs in a background thread; give it a moment.
        deadline = time.time() + 15
        while time.time() < deadline:
            if not any(n.endswith(".part") for n in os.listdir(UPD)):
                break
            time.sleep(0.25)

        print("[startup purge: the automatic pass that runs on every start]")
        left = sorted(n for n in os.listdir(UPD)
                      if n.endswith(".fpk") or ".fpk." in n)
        check("startup purge keeps exactly two packages", len(left) == 2,
              "left %s" % left)
        check("startup purge removes the interrupted .part",
              not any(n.endswith(".part") for n in left), "left %s" % left)
        check("startup purge keeps the newest two",
              "fn-pvepanel-26.11.3-fnos-all.fpk" in left
              and "fn-pvepanel-26.11.2-fnos-all.fpk" in left, "left %s" % left)

        print("[GET /api/storage]")
        code, doc = get("/api/storage")
        check("returns 200", code == 200, "code %r" % code)
        check("ok is true", doc.get("ok") is True, "ok %r" % (doc.get("ok"),))
        for key in ("items", "total", "var_dir", "updates", "updates_keep",
                    "current_version"):
            check("has key %r" % key, key in doc, "keys %s" % sorted(doc))
        # Row names are UI text: a label baked into the payload is Chinese on an
        # English page, so the contract is key-only and the browser translates.
        check("items carry key/bytes/files/path and NOT a label",
              all(set(("key", "bytes", "files", "path")) <= set(it)
                  and "label" not in it for it in doc["items"]),
              "items %r" % doc["items"][:1])
        check("updates_keep is 2", doc.get("updates_keep") == 2,
              "keep %r" % doc.get("updates_keep"))
        check("current_version is the running build",
              doc.get("current_version") == "26.11.99",
              "cur %r" % doc.get("current_version"))
        keys = [it["key"] for it in doc["items"]]
        for want in ("updates", "backups", "panel_log", "panel_log1", "update_log"):
            check("item %r present" % want, want in keys, "keys %s" % keys)
        # total is the sum of top-level rows only: the installer log lives
        # inside update/ and counting it twice would over-report.
        expect = sum(it["bytes"] for it in doc["items"] if not it.get("nested"))
        check("total excludes nested rows", doc["total"] == expect,
              "total %r expect %r" % (doc["total"], expect))
        check("updates entries carry name/bytes/t/partial",
              all(set(("name", "bytes", "t", "partial")) <= set(u)
                  for u in doc["updates"]))

        print("[POST /api/storage/cleanup target=updates]")
        # Fresh fixtures: the startup pass already emptied what was prepared
        # above, so lay down exactly the case the explicit cleanup must handle.
        write_pkg("26.11.1", 100, mtime=1000)
        write_pkg("26.11.2", 101, mtime=1001)
        write_pkg("26.11.3", 102, mtime=1002)
        write_pkg("26.11.4", 103, mtime=1003)
        with open(os.path.join(UPD, "fn-pvepanel-26.11.5-fnos-all.fpk.part"),
                  "wb") as f:
            f.write(b"z" * 50)
        code, res = post("/api/storage/cleanup", {"target": "updates", "keep": 2})
        check("returns 200", code == 200, "code %r body %r" % (code, res))
        check("ok is true", res.get("ok") is True, "res ok %r" % (res.get("ok"),))
        check("freed a positive number of bytes", (res.get("freed") or 0) > 0,
              "freed %r" % res.get("freed"))
        left = sorted(n for n in os.listdir(UPD)
                      if n.endswith(".fpk") or ".fpk." in n)
        check("keep=2 honoured", len(left) == 2, "left %s" % left)
        check("the .part is gone", not any(n.endswith(".part") for n in left),
              "left %s" % left)
        check("the newest two survived",
              "fn-pvepanel-26.11.4-fnos-all.fpk" in left
              and "fn-pvepanel-26.11.3-fnos-all.fpk" in left, "left %s" % left)
        check("a fresh report came back with the cleanup",
              isinstance(res.get("report"), dict),
              "report %r" % type(res.get("report")))

        print("[POST /api/storage/cleanup target=logs]")
        code, res = post("/api/storage/cleanup", {"target": "logs"})
        check("returns 200", code == 200, "code %r" % code)
        # The panel log is the file this process is writing to, so it cannot be
        # asserted to be 0 bytes afterwards - a fresh line may land between the
        # truncate and the stat. Assert it was truncated, i.e. shrank a lot.
        size_now = os.path.getsize(os.path.join(VAR, "pvepanel.log"))
        check("panel log was truncated", size_now < 100, "size %r" % size_now)
        check("rotated log removed",
              not os.path.exists(os.path.join(VAR, "pvepanel.log.1")))
        check("installer log truncated",
              os.path.getsize(os.path.join(UPD, "install.log")) == 0)
        check("freed covers the logs", (res.get("freed") or 0) > 0,
              "freed %r" % res.get("freed"))

        print("[POST /api/storage/cleanup invalid target]")
        code, res = post("/api/storage/cleanup", {"target": "everything"})
        check("invalid target is refused", res.get("ok") is False,
              "res ok %r" % (res.get("ok"),))
        check("invalid target carries a code", res.get("code") == "checkFailed",
              "res %r" % res)

        print("[409 while an update is in flight]")
        # The busy branch cannot be reached from outside the process: it reads
        # the installer's in-memory phase, and this harness is a different
        # process. Patching it here would pass while proving nothing - the old
        # version of this file did exactly that and could never have worked.
        # The mapping itself is pinned by source contract below, and the state
        # machine by tools/test_upgrade_installer.py; storage.cleanup's own
        # busy refusal is covered in tools/test_storage.py.
        src = open(os.path.join(SERVER, "pvepanel.py"), encoding="utf-8").read()
        block = src.split("def _storage_cleanup")[1].split("def _backup_export")[0]
        check("busy answer is a 409", "409" in block, "block has no 409")
        check("busy answer carries json code 'busy'", '"busy": True' in block,
              "block %r" % block[:400])
        check("busy answer keeps the language-neutral code",
              '"code": "busy"' in block, "block %r" % block[:400])
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
        shutil.rmtree(BASE_DIR, ignore_errors=True)

    print("=" * 62)
    if FAILS:
        print("FAILED %d / %d" % (len(FAILS), CHECKS[0]))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("passed %d / %d" % (CHECKS[0], CHECKS[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
