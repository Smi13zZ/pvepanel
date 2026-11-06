#!/usr/bin/env python3
"""console_check.py - render every view and report browser console errors.

A Vue template that references a name the component does not define throws at
render time and is invisible to any static check (the compiler happily emits
`_ctx.foo`). The only honest test is to render the page and listen.

Run on the device (needs chromium + write access to the installed web dir).
"""
import json
import os
import re
import subprocess
import sys

PORT = os.environ.get("PVEPANEL_PORT") or "39205"
BASE = "http://127.0.0.1:" + PORT
VIEWS = [
    ("dash", "?view=dash"),
    ("guests", "?view=guests"),
    ("storage", "?view=storage"),
    ("disks", "?view=disks"),
    ("tasks", "?view=tasks"),
    ("tasks/sub", "?view=tasks"),
    ("set/servers", "?view=settings&tab=servers"),
    ("set/notify", "?view=settings&tab=notify"),
    ("set/maint", "?view=settings&tab=maint"),
    ("set/update", "?view=settings&tab=update"),
    ("set/ui", "?view=settings&tab=ui"),
    ("dash/en", "?view=dash&lang=en"),
    ("guests/en", "?view=guests&lang=en"),
    ("dash/dark", "?view=dash&theme=dark"),
    ("dash/day", "?view=dash&tf=day"),
    ("dash/week", "?view=dash&tf=week"),
    ("dash/month", "?view=dash&tf=month"),
    ("dash/year", "?view=dash&tf=year"),
]

CAPTURE = r"""
<script>
window.__errs = [];
window.addEventListener('error', function (e) {
  window.__errs.push('uncaught: ' + (e.message || e.type));
});
window.addEventListener('unhandledrejection', function (e) {
  window.__errs.push('rejection: ' + (e.reason && e.reason.message || e.reason));
});
var origError = console.error;
console.error = function () {
  window.__errs.push('console.error: ' +
    Array.prototype.map.call(arguments, String).join(' ').slice(0, 200));
  origError.apply(console, arguments);
};
var origWarn = console.warn;
console.warn = function () {
  window.__errs.push('console.warn: ' +
    Array.prototype.map.call(arguments, String).join(' ').slice(0, 200));
  origWarn.apply(console, arguments);
};
setTimeout(function () {
  document.title = 'ERRS:' + JSON.stringify(window.__errs);
}, 4500);
</script>
"""


def run(name, query):
    html = subprocess.run(["curl", "-s", BASE + "/index.html"],
                          capture_output=True, text=True).stdout
    html = html.replace("<head>", "<head>" + CAPTURE)
    tmp = "/var/apps/fn-pvepanel/target/web/_console.html"
    open(tmp, "w").write(html)
    dom = subprocess.run(
        ["chromium", "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--window-size=1280x900",
         "--virtual-time-budget=20000",
         "--user-data-dir=/tmp/cons-" + re.sub(r"\W", "", name),
         "--dump-dom", BASE + "/_console.html" + query],
        capture_output=True, text=True, timeout=200).stdout
    m = re.search(r"<title>ERRS:(.*?)</title>", dom, re.S)
    if not m:
        return None
    raw = (m.group(1).replace("&quot;", '"').replace("&amp;", "&")
           .replace("&lt;", "<").replace("&gt;", ">").replace("&#x27;", "'"))
    try:
        return json.loads(raw)
    except ValueError:
        return [raw[:200]]


bad = 0
for name, query in VIEWS:
    errs = run(name, query)
    if errs is None:
        print("  %-14s probe did not run" % name)
        bad += 1
        continue
    real = [e for e in errs if e]
    if real:
        bad += 1
        print("  %-14s %d problem(s)" % (name, len(real)))
        for e in real[:6]:
            print("        " + e[:180])
    else:
        print("  %-14s clean" % name)

subprocess.run(["rm", "-f", "/var/apps/fn-pvepanel/target/web/_console.html"])
subprocess.run("rm -rf /tmp/cons-*", shell=True)
print("")
print("views with problems: %d / %d" % (bad, len(VIEWS)))
print("RESULT: " + ("FAIL" if bad else "PASS"))
sys.exit(1 if bad else 0)
