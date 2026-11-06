#!/usr/bin/env python3
"""find_overflow.py - which element is wider than its container?

Run on the device. Renders a page and reports every element whose scrollWidth
exceeds its clientWidth, plus its own text. Assumes nothing: the first version
of a similar script took --window-size as the phone width, but this chromium
ignores that flag, so the measurement is done at whatever viewport the browser
actually has.
"""
import re
import subprocess
import sys

BASE = "http://127.0.0.1:8899"

PROBE = r"""
<script>
setTimeout(function () {
  var out = [];
  var all = document.querySelectorAll('*');
  for (var i = 0; i < all.length; i++) {
    var el = all[i];
    if (el.scrollWidth > el.clientWidth + 1 && el.clientWidth > 0) {
      out.push({
        tag: el.tagName.toLowerCase(),
        cls: (el.className || '').toString().slice(0, 60),
        cw: el.clientWidth,
        sw: el.scrollWidth,
        ox: getComputedStyle(el).overflowX,
        text: (el.textContent || '').trim().slice(0, 50)
      });
    }
  }
  document.title = 'OVF:' + JSON.stringify(out);
}, 3500);
</script>
"""


def run(page, vw):
    html = subprocess.run(["curl", "-s", BASE + "/index.html"],
                          capture_output=True, text=True).stdout
    html = html.replace("</body>", PROBE + "</body>")
    tmp = "/var/apps/fn-pvepanel/target/web/_ovf.html"
    open(tmp, "w").write(html)
    dom = subprocess.run(
        ["chromium", "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--window-size=" + vw,
         "--virtual-time-budget=22000",
         "--user-data-dir=/tmp/ovf-" + page,
         "--dump-dom", "%s/_ovf.html?view=%s&tab=servers" % (BASE, page)],
        capture_output=True, text=True, timeout=200).stdout
    subprocess.run(["rm", "-f", tmp])
    m = re.search(r"<title>OVF:(.*?)</title>", dom, re.S)
    if not m:
        print("  probe did not run")
        return
    raw = (m.group(1).replace("&quot;", '"').replace("&amp;", "&")
           .replace("&lt;", "<").replace("&gt;", ">").replace("&#x27;", "'"))
    import json
    try:
        items = json.loads(raw)
    except ValueError as exc:
        print("  parse failed:", exc, raw[:200])
        return
    if not items:
        print("  nothing overflows")
    for it in items:
        print("  %-6s .%-28s cw=%-5d sw=%-5d overflowX=%-8s %r" % (
            it["tag"], it["cls"], it["cw"], it["sw"], it["ox"], it["text"]))


print("== settings/servers (where the log lives)")
run("settings", "390x844")
subprocess.run("rm -rf /tmp/ovf-*", shell=True)
