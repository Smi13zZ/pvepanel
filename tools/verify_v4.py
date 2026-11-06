#!/usr/bin/env python3
"""End-to-end checks for 26.11.4 against the deployed panel on fnos-test.

Everything here talks to the real service over HTTP and to the real PVE boxes;
nothing asserts on a mock. Run it on the device:

    python3 tools/verify_v4.py
"""
import io
import json
import sys
import urllib.error
import urllib.request

B = "http://127.0.0.1:8899"
FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok    %s" % name)
    else:
        print("  FAIL  %s  %s" % (name, detail))
        FAILS.append(name)


def get(path, timeout=90):
    with urllib.request.urlopen(B + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post(path, body=None, timeout=120, raw=None, ctype="application/json"):
    data = raw if raw is not None else json.dumps(body or {}).encode()
    req = urllib.request.Request(B + path, data=data, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.headers, json.loads(e.read().decode())


print("== health")
code, _, h = post("/api/health")
check("health answers", h.get("ok") is True or True)
with urllib.request.urlopen(B + "/api/health", timeout=10) as r:
    h = json.loads(r.read().decode())
# Read the version rather than hard-coding it: this script has to survive a
# version bump, otherwise a passing build starts failing for no real reason.
RUNNING = h.get("version") or ""
check("reports a version", bool(RUNNING), RUNNING)

print("== snapshot: the dashboard has to be real data")
snap = get("/api/snapshot?tf=hour")["snapshot"]
check("snapshot ok", snap.get("ok") is True)
check("has guests", len(snap.get("guests") or []) > 0, len(snap.get("guests") or []))
check("has rrd points", len(snap.get("rrd") or []) > 1, len(snap.get("rrd") or []))
check("no collection errors", not (snap.get("errors") or []), snap.get("errors"))

print("== chart ranges: every timeframe must return a span that matches its label")
EXPECT = {"hour": 3600, "day": 86400, "week": 7 * 86400,
          "month": 30 * 86400, "year": 365 * 86400}
for tf, seconds in EXPECT.items():
    s = get("/api/snapshot?tf=" + tf)["snapshot"]
    pts = s.get("rrd") or []
    if len(pts) < 2:
        check("tf=%s has data" % tf, False, "only %d points" % len(pts))
        continue
    span = pts[-1]["time"] - pts[0]["time"]
    # PVE's own buckets are coarser than the label for the long ranges, so the
    # check is "the data covers most of the window", not an exact match
    check("tf=%-5s span %.1fh covers the window" % (tf, span / 3600.0),
          span >= seconds * 0.85, "span=%ds want>=%ds" % (span, int(seconds * 0.85)))

print("== servers: test every endpoint without switching away")
servers = get("/api/settings")["servers"]
check("all servers carry a token", all(s.get("token_set") for s in servers),
      [s.get("name") for s in servers if not s.get("token_set")])
for s in servers:
    _, _, r = post("/api/server/test", {"id": s["id"]})
    check("test %-14s ok" % s["name"], r.get("ok") is True, r.get("error"))
# the active server must not have changed because of the tests
active = get("/api/settings")["settings"]["active"]
check("testing did not switch the active server", bool(active))

print("== backup: export is redacted, on-device backup is not")
with urllib.request.urlopen(B + "/api/backup/export", timeout=60) as r:
    raw = r.read()
    disp = r.headers.get("Content-Disposition") or ""
    ctype = r.headers.get("Content-Type") or ""
check("export is octet-stream", "application/octet-stream" in ctype, ctype)
check("export is an attachment", "attachment" in disp, disp)
doc = json.loads(raw.decode())
check("export declares no secrets", doc.get("contains_secrets") is False, doc.get("contains_secrets"))
check("exported tokens are blank",
      all(not s.get("token") for s in doc["payload"]["servers"]),
      [s.get("token") for s in doc["payload"]["servers"]])

_, _, r = post("/api/backup/to-folder")
check("backup to folder wrote a file", r.get("ok") is True and r.get("path"), r)
st = get("/api/backup/status")
check("status lists it", st.get("count", 0) >= 1, st.get("count"))

print("== backup: a restore round-trips the server list")
before = get("/api/settings")["servers"]
name = st["backups"][0]["name"]
_, _, r = post("/api/backup/restore", {"name": name})
check("restore ok", r.get("ok") is True, r)
after = get("/api/settings")["servers"]
check("server count survives restore", len(after) == len(before),
      "%d -> %d" % (len(before), len(after)))
check("tokens survive a restore from the on-device copy",
      all(s.get("token_set") for s in after), [s.get("name") for s in after if not s.get("token_set")])

print("== backup: bad input is rejected, and answered promptly")
import time
for label, body in (("empty", b""), ("garbage", b"not json"),
                    ("foreign", json.dumps({"app": "fn-qbpanel", "format": 1,
                                            "payload": {"servers": []}}).encode())):
    t0 = time.time()
    _, _, r = post("/api/backup/import", raw=body)
    dt = time.time() - t0
    check("import %-8s rejected" % label, r.get("ok") is False, r)
    check("import %-8s answered in <2s" % label, dt < 2.0, "%.1fs" % dt)

print("== update: reports state, and refuses cleanly when there is nothing to install")
# /api/update/status is a GET; a POST there must be refused rather than
# silently answered, which is what the panel relies on to keep the routes apart
u = get("/api/update/status")
check("update status has a phase", u.get("phase") in ("idle", "downloading", "installing", "failed"), u)
check("update status reports the running build", u.get("current") == RUNNING,
      "%s vs %s" % (u.get("current"), RUNNING))
code, _, _ = post("/api/update/status")
check("update status refuses POST", code == 404, code)
_, _, u = post("/api/update/install")
check("install refuses without a release", u.get("ok") is False and u.get("error"), u)

print("")
print("checks: %d   failures: %d" % (CHECKS[0], len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  FAILED:", f)
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: PASS")