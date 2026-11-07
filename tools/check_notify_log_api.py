#!/usr/bin/env python3
"""Live check: the delivery-record endpoints, end to end.

Starts the real service against a throwaway var dir and drives the three
endpoints the settings page uses. The channel points at a dead port on purpose:
a push that FAILS must still be recorded, which is the whole reason the log
exists.

Run:  python tools/check_notify_log_api.py
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "fn-pvepanel", "app", "server")
BASE_DIR = os.path.join(ROOT, "tmp", "_unit", "notifylog_api")
VAR = os.path.join(BASE_DIR, "var")
PORT = "39432"
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


def req(path, method="GET", body=None, timeout=15):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def main():
    if os.path.isdir(BASE_DIR):
        shutil.rmtree(BASE_DIR, ignore_errors=True)
    os.makedirs(VAR, exist_ok=True)
    # One enabled webhook pointing at a dead port: the push fails, the record
    # must not.
    cfg = {
        "notify_enabled": True,
        "notify_channels": [{
            "id": "ch1", "type": "webhook", "name": "ops", "enabled": True,
            "config": {"url": "http://127.0.0.1:1/nothing",
                       "headers": "X-Token: s3cret-should-not-be-stored"},
        }],
        "notify_events": {"node_offline": True, "storage_full": True},
        "lang": "zh",
    }
    with open(os.path.join(VAR, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    logpath = os.path.join(BASE_DIR, "panel.out")
    logf = open(logpath, "w", encoding="utf-8")
    env = dict(os.environ)
    env.update({"TRIM_PKGVAR": VAR, "PVEPANEL_VERSION": "26.11.99",
                "PVEPANEL_PORT": PORT, "PVEPANEL_LISTEN": "127.0.0.1"})
    srv = subprocess.Popen(
        [sys.executable, os.path.join(SERVER, "pvepanel.py")],
        env=env, cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT)
    try:
        up = False
        for _ in range(80):
            try:
                req("/api/health", timeout=3)
                up = True
                break
            except Exception:
                time.sleep(0.25)
        if not up:
            logf.flush()
            print("server did not start")
            with open(logpath, encoding="utf-8", errors="replace") as f:
                print(f.read()[-3000:])
            return 1

        print("[empty log]")
        code, doc = req("/api/notify/log")
        check("GET returns 200", code == 200, code)
        check("ok is true", doc.get("ok") is True, doc.get("ok"))
        check("records starts empty", doc.get("records") == [], doc.get("records"))
        check("total is 0", doc.get("total") == 0, doc.get("total"))
        check("max is a positive cap", isinstance(doc.get("max"), int) and doc["max"] > 0,
              doc.get("max"))

        print("[test push is recorded even though the channel is dead]")
        code, meta = req("/api/notify")
        channels = (meta.get("notify") or {}).get("channels") or []
        check("the configured channel is visible", len(channels) == 1, channels)
        cid = channels[0].get("id") if channels else ""
        code, res = req("/api/notify/test", method="POST", body={"id": cid}, timeout=30)
        check("test endpoint answers 200", code == 200, code)
        check("test reports failure (dead port)", res.get("ok") is False, res)

        code, doc = req("/api/notify/log")
        recs = doc.get("records") or []
        check("the failed push produced a record", len(recs) == 1, len(recs))
        if recs:
            r = recs[0]
            for key in ("id", "at", "event", "channel", "channel_id", "type",
                        "ok", "detail", "title", "body"):
                check("record has %r" % key, key in r, sorted(r))
            check("recorded as a failure", r.get("ok") is False, r.get("ok"))
            check("event is 'test'", r.get("event") == "test", r.get("event"))
            check("channel name recorded", r.get("channel") == "ops", r.get("channel"))
            check("channel id recorded", r.get("channel_id") == "ch1", r.get("channel_id"))
            check("failure reason recorded", bool(r.get("detail")), r.get("detail"))
            title = r.get("title") or ""
            check("title is bilingual",
                  "通知测试" in title and "Notification test" in title, title)
            body = r.get("body") or ""
            check("body carries both languages",
                  "渠道配置正确" in body and "configured correctly" in body, body[:120])

        print("[the record file never holds the channel secret]")
        raw = ""
        npath = os.path.join(VAR, "notify-log.json")
        check("notify-log.json was written", os.path.isfile(npath), npath)
        if os.path.isfile(npath):
            with open(npath, encoding="utf-8") as f:
                raw = f.read()
            check("secret is absent from the file", "s3cret" not in raw, raw[:200])
            check("file parses as json", isinstance(json.loads(raw), dict), raw[:80])

        print("[GET honours the limit]")
        code, doc = req("/api/notify/log?limit=1")
        check("limit still reports the real total",
              doc.get("total") == 1 and len(doc.get("records") or []) == 1, doc.get("total"))

        print("[DELETE clears]")
        code, res = req("/api/notify/log", method="DELETE")
        check("DELETE answers 200", code == 200, code)
        check("DELETE reports ok", res.get("ok") is True, res)
        code, doc = req("/api/notify/log")
        check("log is empty after DELETE", doc.get("records") == [], doc.get("records"))
        check("total is 0 after DELETE", doc.get("total") == 0, doc.get("total"))
        check("the file was rewritten, not deleted",
              os.path.isfile(npath), "notify-log.json missing")

        print("[unknown DELETE path answers 404]")
        code, res = req("/api/notify/whatever", method="DELETE")
        check("unknown DELETE is 404", code == 404, code)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
        try:
            logf.close()
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
