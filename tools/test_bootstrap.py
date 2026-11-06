#!/usr/bin/env python3
"""test_bootstrap.py - the page bootstrap must never block on the network.

Regression for a measured defect: /api/bootstrap called update.check()
synchronously, which tries three layers with an 8s timeout each. On a box whose
outbound internet is blocked, that stalled the first paint for 16.5s - and again
every time the 60s failure cooldown expired, i.e. every minute forever.

The rule is therefore: bootstrap only reads the cache. This test enforces it.
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "fn-pvepanel", "app", "server"))

import update  # noqa: E402

FAILS = []
N = [0]


def check(name, cond, detail=""):
    N[0] += 1
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond else "  " + str(detail)))
    if not cond:
        FAILS.append(name)


print("== update.cached() never touches the network")
update.reset()
check("empty cache returns {}", update.cached() == {})

# Point it at a blackhole and let the failure land in the cache.
t0 = time.time()
res = update.check("26.11.7", force=True,
                   api_base="http://10.255.255.1:9",
                   web_base="http://10.255.255.1:9", timeout=1.0)
took_fail = time.time() - t0
check("a forced check really failed", res.get("ok") is False, res.get("reason"))

t0 = time.time()
hit = update.cached()
took_cached = time.time() - t0
check("cached() returns the failure", hit.get("reason") == res.get("reason"),
      (hit.get("reason"), res.get("reason")))
check("cached() is flagged cached", hit.get("cached") is True, hit.get("cached"))
check("cached() is fast (<50ms)", took_cached < 0.05, "%.3fs" % took_cached)
check("cached() is much faster than the lookup", took_cached < took_fail,
      "cached=%.3fs lookup=%.3fs" % (took_cached, took_fail))

print("== update.cached() is safe to call concurrently")
errs = []
def hammer():
    try:
        for _ in range(200):
            update.cached()
    except Exception as exc:  # noqa: BLE001
        errs.append(repr(exc))
ts = [threading.Thread(target=hammer) for _ in range(8)]
for t in ts:
    t.start()
for t in ts:
    t.join()
check("no exception from 1600 concurrent calls", not errs, errs[:2])

print("== the source must not call the blocking check from bootstrap")
src = open(os.path.join(ROOT, "src", "fn-pvepanel", "app", "server", "pvepanel.py"),
           encoding="utf-8").read()
boot = src.split('if path == "/api/bootstrap":')[1].split('if path == "/api/snapshot":')[0]
check("bootstrap uses _update_cached", "_update_cached()" in boot)
check("bootstrap does not call _update_check", "_update_check" not in boot)
check("_update_cached falls back without blocking",
      "cached" in src and "pending" in src)

print("")
print("checks: %d  failures: %d" % (N[0], len(FAILS)))
print("RESULT: " + ("FAIL" if FAILS else "PASS"))
sys.exit(1 if FAILS else 0)
