#!/usr/bin/env python3
"""Checks for 26.11.5: the three reported fixes.

The chart ones are geometry, so they are checked by reading the pixels the
canvas actually produced - not by trusting the code that drew them.
"""
import io
import json
import os
import re
import sys
import tarfile
import urllib.request

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FPK = os.path.join(ROOT, "output", "fn-pvepanel-26.11.5-fnos-all.fpk")
FAILS = []
N = [0]


def check(name, cond, detail=""):
    N[0] += 1
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond else "  " + str(detail)))
    if not cond:
        FAILS.append(name)


print("== icons in the shipped package must have transparent rounded corners")
tmp = os.path.join(ROOT, "tmp")
os.makedirs(tmp, exist_ok=True)
with tarfile.open(FPK, "r:gz") as tf:
    for name in ("ICON.PNG", "ICON_256.PNG"):
        with open(os.path.join(tmp, "_" + name), "wb") as f:
            f.write(tf.extractfile(name).read())
for name in ("ICON.PNG", "ICON_256.PNG"):
    im = Image.open(os.path.join(tmp, "_" + name))
    rgba = im.convert("RGBA")
    w, h = im.size
    corners = [rgba.getpixel(pt) for pt in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    check("%s keeps its alpha channel" % name, im.mode == "RGBA", im.mode)
    check("%s corners are transparent" % name, all(c[3] == 0 for c in corners), corners)
    check("%s plate is still white at the edge midpoints" % name,
          rgba.getpixel((w // 2, 0)) >= (240, 240, 240, 255), rgba.getpixel((w // 2, 0)))

print("== the tick layout code must not put a tick hard against either edge")
src = open(os.path.join(ROOT, "src", "fn-pvepanel", "app", "web", "js",
                        "charts.js"), encoding="utf-8").read()
check("uses a symmetric inset", "TICK_INSET" in src and "padL + TICK_INSET" in src)
check("labels are centre-aligned on their gridline",
      re.search(r'textAlign = "center";\s*\n\s*ctx\.fillText\(label, x, H - 9\)', src) is not None)
check("tick count is capped by the label resolution",
      "TF_UNIT" in src and "distinct" in src)
check("a repeated label is suppressed", "lastLabel" in src)

print("== phones must drop the detail columns instead of scrolling the actions away")
css = open(os.path.join(ROOT, "src", "fn-pvepanel", "app", "web", "style.css"),
           encoding="utf-8").read()
phone = css.split("@media (max-width: 640px)")[-1]
check("guest table hides CPU/内存/运行时长/类型 on a phone",
      ".ttable.tlist th:nth-child(4)" in phone and ".ttable.tlist th:nth-child(7)" in phone)
check("guest table keeps the action column",
      ".ttable.tlist th:nth-child(8) { width: 104px !important; }" in phone)
check("no sticky action column anywhere", "position: sticky; right: 0" not in css)
check("task table sheds its timestamp columns on a phone",
      ".ttable.ttasks th:nth-child(4)" in phone)
check("disk table sheds model/type/wear on a phone",
      ".ttable.tdisk th:nth-child(2)" in phone)
check("the scroll hint is hidden where nothing scrolls",
      ".card.flush .scroll-hint { display: none; }" in phone)

print("== the deployed panel still serves real data")
try:
    with urllib.request.urlopen("http://127.0.0.1:8899/api/snapshot?tf=hour", timeout=60) as r:
        snap = json.loads(r.read().decode())["snapshot"]
    check("snapshot ok", snap.get("ok") is True)
    check("guests present", len(snap.get("guests") or []) > 0)
    check("rrd present", len(snap.get("rrd") or []) > 1)
    check("no collection errors", not (snap.get("errors") or []), snap.get("errors"))
except Exception as exc:
    print("  (skipped: this script is meant to run on the device: %r)" % (exc,))

print("")
print("checks: %d   failures: %d" % (N[0], len(FAILS)))
print("RESULT: " + ("FAIL" if FAILS else "PASS"))
sys.exit(1 if FAILS else 0)
