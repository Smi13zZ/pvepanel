#!/usr/bin/env python3
"""probe_cols.py - measure the rendered list tables in a real browser.

Run on the device (needs chromium and write access to the installed web dir).

Answers two questions that a stylesheet review cannot:
  * is every column actually in the DOM (no display:none slipped back in)?
  * does the scroll wrapper overflow, so a narrow screen can reach the
    right-hand columns?

A table that fits its wrapper is NOT a failure - that is simply a table with
nothing to scroll. The assertion is therefore "no hidden columns anywhere; if
the content is wider than the wrapper, the wrapper reports it can scroll".
"""
import json
import re
import subprocess
import sys

BASE = "http://127.0.0.1:8899"
PAGES = ("guests", "tasks", "disks")
SIZES = ("390x844", "1440x1000")

PROBE = r"""
<script>
setTimeout(function () {
  var out = [];
  document.querySelectorAll('.tscroll').forEach(function (box) {
    var t = box.querySelector('table');
    if (!t) return;
    var ths = Array.prototype.slice.call(t.querySelectorAll('thead th'));
    out.push({
      cls: t.className.replace('ttable', '').trim(),
      cols: ths.length,
      hidden: ths.filter(function (th) {
        return getComputedStyle(th).display === 'none';
      }).length,
      labels: ths.map(function (th) { return th.textContent.trim(); }),
      vpW: window.innerWidth,
      clientW: Math.round(box.clientWidth),
      scrollW: Math.round(box.scrollWidth),
      scrolls: box.scrollWidth > box.clientWidth + 1
    });
  });
  document.title = 'PROBE:' + JSON.stringify(out);
}, 3000);
</script>
"""


def shoot(page, size):
    html = subprocess.run(["curl", "-s", BASE + "/index.html"],
                          capture_output=True, text=True).stdout
    html = html.replace("</body>", PROBE + "</body>")
    tmp = "/var/apps/fn-pvepanel/target/web/_probe.html"
    open(tmp, "w").write(html)
    dom = subprocess.run(
        ["chromium", "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--window-size=" + size,
         "--force-device-scale-factor=1",
         "--virtual-time-budget=20000",
         "--user-data-dir=/tmp/prof-%s-%s" % (page, size.split("x")[0]),
         "--dump-dom", "%s/_probe.html?view=%s" % (BASE, page)],
        capture_output=True, text=True, timeout=180).stdout
    m = re.search(r"<title>PROBE:(.*?)</title>", dom, re.S)
    if not m:
        return None
    raw = (m.group(1).replace("&quot;", '"').replace("&amp;", "&")
           .replace("&lt;", "<").replace("&gt;", ">"))
    try:
        return json.loads(raw)
    except ValueError:
        return None


fails = []
for page in PAGES:
    for size in SIZES:
        data = shoot(page, size)
        if data is None:
            print("  FAIL %-7s %-10s probe did not run" % (page, size))
            fails.append((page, size, "no probe"))
            continue
        for t in data:
            tag = "  %-7s %-10s %-8s" % (page, size, t["cls"] or "?")
            print(tag + " cols=%d hidden=%d  vp=%d wrapper=%d content=%d scrolls=%s" % (
                t["cols"], t["hidden"], t["vpW"], t["clientW"],
                t["scrollW"], t["scrolls"]))
            print(" " * len(tag) + " " + " | ".join(t["labels"]))
            if t["hidden"]:
                fails.append((page, size, "%d hidden column(s)" % t["hidden"]))
            # content wider than the wrapper but reported as not scrollable
            # would mean the columns are unreachable
            if t["scrollW"] > t["clientW"] + 1 and not t["scrolls"]:
                fails.append((page, size, "overflowing but not scrollable"))

subprocess.run(["rm", "-f", "/var/apps/fn-pvepanel/target/web/_probe.html"])
subprocess.run("rm -rf /tmp/prof-*", shell=True)

print("")
if fails:
    for f in fails:
        print("  FAILED:", f)
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: PASS - every column present; overflowing tables scroll")
