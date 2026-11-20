#!/usr/bin/env python3
"""preflight.py - run every gate the CI runs, in the same order.

The point is that "it passed locally" and "CI passed" mean the same thing. If a
step here is added to the workflow, add it here too.

    python tools/preflight.py

Exits non-zero on the first failure.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "fn-pvepanel", "app", "server")
WEB = os.path.join(ROOT, "src", "fn-pvepanel", "app", "web")

FAILS = []


def run(argv, cwd=None):
    """Run a child and decode its output as UTF-8.

    text=True alone uses the locale codec: on Windows that is GBK, and a Chinese
    message encoded as UTF-8 makes the reader thread die with UnicodeDecodeError.
    The child's stderr then comes back empty, so a failing gate reports nothing.
    """
    return subprocess.run(argv, cwd=cwd or ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def step(title, argv, cwd=None):
    print("== " + title)
    res = run(argv, cwd)
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
step("docs references", [sys.executable, os.path.join(ROOT, "tools", "check_docs.py")])
# 升级遮罩的阶段收敛：阶段只前进 / attempt 隔离 / total 只增。
# 这是「更新完卡在遮罩里」的直接成因，纯静态即可判定。
step("update stage monotonicity", [sys.executable, os.path.join(ROOT, "tools", "check_update_mono.py")])
step("python compiles", [sys.executable, "-m", "compileall", "-q", SERVER])

# node --check on every front-end script; skip politely when node is absent.
# "absent" has to be a real check: subprocess raises FileNotFoundError instead of
# returning non-zero, which used to abort the whole preflight on machines without node.
node = os.environ.get("NODE_BIN") or shutil.which("node") or shutil.which("node.exe")
if not node:
    print("== front-end syntax: skipped (no node on PATH; set NODE_BIN to enable)")
else:
    script_ok = True
    for fn in sorted(os.listdir(os.path.join(WEB, "js"))):
        if not fn.endswith(".js"):
            continue
        res = run([node, "--check", os.path.join(WEB, "js", fn)])
        if res.returncode != 0:
            script_ok = False
            print("   FAILED node --check %s" % fn)
            print((res.stderr or "").strip()[:400])
    print("== front-end syntax: " + ("ok" if script_ok else "FAILED"))
    if not script_ok:
        FAILS.append("front-end syntax")

for name in ("test_bootstrap", "test_maint", "test_upgrade_installer",
             "test_notify", "test_notify_log", "test_storage", "test_update"):
    step(name, [sys.executable, os.path.join(ROOT, "tools", name + ".py")])

# Template tags: an unbalanced one reparents the rest of the settings page in
# the browser, which only shows up as a half-missing page on a device.
if node:
    step("template check", [node, os.path.join(ROOT, "tools", "check_template.js")])

# The delivery-record and storage endpoints are exercised against a real HTTP
# service: the unit tests cover the rules, these cover the routes wiring them up.
step("notify log api", [sys.executable,
                        os.path.join(ROOT, "tools", "check_notify_log_api.py")])
step("storage api", [sys.executable,
                     os.path.join(ROOT, "tools", "check_storage_api.py")])

# The one-click update UI contract: phases, translatable failure codes, the
# "any page" failure bar, attempt baseline, byte counters, rollback honesty.
# All of it is statically checkable and all of it has silently regressed before.
step("update ui contract", [sys.executable,
                            os.path.join(ROOT, "tools", "check_update_ui.py")])

print("")
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: PASS - every CI gate passed locally")
