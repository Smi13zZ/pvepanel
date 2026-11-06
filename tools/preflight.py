#!/usr/bin/env python3
"""preflight.py - run every gate the CI runs, in the same order.

The point is that "it passed locally" and "CI passed" mean the same thing. If a
step here is added to the workflow, add it here too.

    python tools/preflight.py

Exits non-zero on the first failure.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "fn-pvepanel", "app", "server")
WEB = os.path.join(ROOT, "src", "fn-pvepanel", "app", "web")

FAILS = []


def step(title, argv, cwd=None):
    print("== " + title)
    res = subprocess.run(argv, cwd=cwd or ROOT, capture_output=True, text=True)
    out = (res.stdout or "").strip().splitlines()
    for line in out[-4:]:
        print("   " + line)
    if res.returncode != 0:
        print("   FAILED (exit %d)" % res.returncode)
        for line in (res.stderr or "").strip().splitlines()[-10:]:
            print("   ! " + line)
        FAILS.append(title)
    else:
        print("   ok")
    return res.returncode == 0


step("structure check", [sys.executable, os.path.join(ROOT, "tools", "check_structure.py")])
step("python compiles", [sys.executable, "-m", "compileall", "-q", SERVER])

# node --check on every front-end script; skip politely when node is absent
node = os.environ.get("NODE_BIN") or "node"
script_ok = True
for fn in sorted(os.listdir(os.path.join(WEB, "js"))):
    if not fn.endswith(".js"):
        continue
    res = subprocess.run([node, "--check", os.path.join(WEB, "js", fn)],
                         capture_output=True, text=True)
    if res.returncode != 0:
        script_ok = False
        print("   FAILED node --check %s" % fn)
        print((res.stderr or "").strip()[:400])
print("== front-end syntax: " + ("ok" if script_ok else "FAILED"))
if not script_ok:
    FAILS.append("front-end syntax")

for name in ("test_bootstrap", "test_maint", "test_upgrade_installer",
             "test_notify", "test_update"):
    step(name, [sys.executable, os.path.join(ROOT, "tools", name + ".py")])

print("")
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: PASS - every CI gate passed locally")
