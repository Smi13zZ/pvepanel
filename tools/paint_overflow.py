#!/usr/bin/env python3
"""paint_overflow.py - outline horizontally overflowing elements and print
their identities into the page, then screenshot at phone width.

This chromium honours --window-size for --screenshot but ignores it for
--dump-dom (measured: three widths all reported innerWidth 780 under
--dump-dom, while the screenshots really are 390 CSS px wide), so the phone
layout can only be inspected through a screenshot. Writing the findings onto
the page is therefore the only way to read them back.
"""
import os
import subprocess
import sys

PORT = os.environ.get("PVEPANEL_PORT") or "39205"
BASE = "http://127.0.0.1:" + PORT
WEB = "/var/apps/fn-pvepanel/target/web"

PAINT = r"""
<script>
setTimeout(function () {
  var all = document.querySelectorAll('*');
  var lines = [];
  for (var i = 0; i < all.length; i++) {
    var el = all[i];
    if (el.classList && el.classList.contains('sr-only')) continue;
    if (el.closest && el.closest('#ovfpanel')) continue;
    if (el.scrollWidth > el.clientWidth + 1 && el.clientWidth > 0) {
      el.style.outline = '2px solid #e7000b';
      lines.push(el.tagName.toLowerCase() +
                 (el.className ? '.' + String(el.className).split(' ')[0] : '') +
                 ' cw=' + el.clientWidth + ' sw=' + el.scrollWidth +
                 ' ox=' + getComputedStyle(el).overflowX);
    }
  }
  var panel = document.createElement('div');
  panel.id = 'ovfpanel';
  panel.style.cssText = 'position:fixed;left:0;top:0;right:0;z-index:99999;' +
    'background:rgba(0,0,0,.88);color:#0f0;font:10px/1.35 monospace;' +
    'padding:6px;max-height:60vh;overflow:auto;white-space:pre-wrap';
  panel.textContent = 'viewport ' + window.innerWidth + 'px | overflowing ' +
    lines.length + '\n' + lines.join('\n');
  document.body.appendChild(panel);
}, 3500);
</script>
"""


def shoot(page, tab, out):
    html = subprocess.run(["curl", "-s", BASE + "/index.html"],
                          capture_output=True, text=True).stdout
    html = html.replace("</body>", PAINT + "</body>")
    tmp = WEB + "/_paint.html"
    open(tmp, "w").write(html)
    subprocess.run(
        ["chromium", "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--window-size=390x844",
         "--force-device-scale-factor=2", "--virtual-time-budget=22000",
         "--user-data-dir=/tmp/paintprof",
         "--screenshot=" + out,
         "%s/_paint.html?view=%s&tab=%s" % (BASE, page, tab)],
        capture_output=True, text=True, timeout=200)
    subprocess.run(["rm", "-f", tmp])


shoot("settings", "servers", "/tmp/paint-settings.png")
subprocess.run("rm -rf /tmp/paintprof", shell=True)
print("wrote /tmp/paint-settings.png")
