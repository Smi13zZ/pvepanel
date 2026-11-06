#!/usr/bin/env python3
"""Headless screenshot of the panel, taken on the fnOS box, pulled back locally.

The panel is a Vue app that fetches its data after mount, so a naive
"--screenshot" run catches the empty shell.  Two things fix that here:

  * --virtual-time-budget lets Chromium fast-forward timers *and* pause for
    pending network requests, so the first paint with real data is what gets
    captured (measured: a fetch + 1.2s timer completes inside a 6000ms budget
    while the whole run still takes ~0.5s of wall clock).
  * the same run also emits --dump-dom, which we parse locally.  That turns
    "did the page actually render?" from a guess into a check: missing data
    markers, lingering loading text and near-empty DOMs are reported.

Dark mode matters for the flag choice: on this Chromium (156) the ONLY way to
make 'prefers-color-scheme: dark' true from the CLI is --force-dark-mode.
--blink-settings=preferredColorScheme=2 is silently ignored (verified by
decoding screenshot pixels: 255,255,255 vs 16,16,16).

Usage
    python tools/shot.py                                  # desktop, newest panel on :8899
    python tools/shot.py --preset all                      # desktop + mobile + dark + dark-mobile
    python tools/shot.py --preset mobile --dpr 2
    python tools/shot.py --url http://127.0.0.1:8899/ --out image/home.png
    python tools/shot.py --url http://127.0.0.1:8771/test.html --expect "READY-DATA"
    python tools/shot.py --size 1440x2200 --preset tall    # long page
"""

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402

REMOTE_DIR = "/tmp/pvepanel-shot"
CHROME = "/usr/bin/chromium"

PRESETS = {
    "desktop": {"size": "1440x1000", "dark": False},
    "mobile": {"size": "390x844", "dark": False},
    "dark": {"size": "1440x1000", "dark": True},
    "mobile-dark": {"size": "390x844", "dark": True},
    "tall": {"size": "1440x2200", "dark": False},
}

# Text a half-rendered panel typically still shows.
#
# Matched on word boundaries, not as substrings. The settings page renders the
# panel's own runtime log, and that log legitimately contains "downloading" -
# a plain substring check reports "the page is still loading" for a page that
# finished loading and is showing a download line. A false alarm here is worse
# than no check at all: it trains you to ignore the result.
LOADING_TOKENS = [r"\bloading\b", "加载中", "載入中", "请稍候", "正在载入", r"\bskeleton\b"]

# One script, all shots: 6 chromium launches over 6 ssh round-trips would be slow.
REMOTE_SHOT = r"""set -u
DIR="$1"; CHROME="$2"; shift 2

mkdir -p "$DIR"
rm -f "$DIR"/*.png "$DIR"/*.dom "$DIR"/*.err 2>/dev/null || true

# --- why two chromium runs per shot -------------------------------------
# Passing --screenshot and --dump-dom to the SAME run silently yields a blank
# frame as soon as the page needs more than a moment to paint: measured on the
# real panel, shot+dom gave a 6.4 KB all-white 1440x1000 png while shot alone
# gave the fully rendered 100 KB page with identical flags otherwise.  The DOM
# dump is still worth having (it is how a rendered page is told from a loading
# one), so each shot costs one extra ~0.5s run instead of a wrong png.
n=0
for spec in "$@"; do
    n=$((n + 1))
    # spec = name|url|size|dark|dpr|budget
    name=$(printf '%s' "$spec"   | cut -d'|' -f1)
    url=$(printf '%s'  "$spec"   | cut -d'|' -f2)
    size=$(printf '%s' "$spec"   | cut -d'|' -f3)
    dark=$(printf '%s' "$spec"   | cut -d'|' -f4)
    dpr=$(printf '%s'  "$spec"   | cut -d'|' -f5)
    budget=$(printf '%s' "$spec" | cut -d'|' -f6)

    common="--headless=new --disable-gpu --no-sandbox --disable-dev-shm-usage
            --hide-scrollbars --no-first-run --no-default-browser-check
            --disable-extensions --disable-background-networking
            --disable-features=Translate,MediaRouter
            --window-size=$size --virtual-time-budget=$budget
            --force-device-scale-factor=$dpr"
    [ "$dark" = "1" ] && common="$common --force-dark-mode"

    # A stale lock in a shared profile is the usual cause of an empty frame,
    # so every run gets its own throwaway profile.  Even then chromium has been
    # seen to exit 0 in ~400ms having written nothing at all, so a shot that
    # produced no file is retried once with a fresh profile before we give up.
    shot() {
        rm -f "$DIR/$name.png"
        rm -rf "$DIR/p-$name"
        # shellcheck disable=SC2086
        timeout 120 "$CHROME" $common --user-data-dir="$DIR/p-$name" --screenshot="$DIR/$name.png" "$url" < /dev/null > /dev/null 2> "$DIR/$name.err"
        rc=$?
        bytes=0
        [ -f "$DIR/$name.png" ] && bytes=$(stat -c%s "$DIR/$name.png")
        return 0
    }
    t0=$(date +%s%N)
    shot
    [ "$bytes" = "0" ] && shot && echo "  retried $name (first attempt wrote no png)"
    if [ "$bytes" = "0" ]; then
        echo "  chromium stderr: $(tail -4 "$DIR/$name.err" 2>/dev/null | tr '\n' ' ')"
    fi

    dombytes=0
    if [ "$rc" = "0" ] && [ "$bytes" -gt 0 ]; then
        # shellcheck disable=SC2086
        timeout 120 "$CHROME" $common --user-data-dir="$DIR/d-$name" --dump-dom "$url" < /dev/null > "$DIR/$name.dom" 2> "$DIR/$name.domerr"
        [ -f "$DIR/$name.dom" ] && dombytes=$(stat -c%s "$DIR/$name.dom")
    fi
    t1=$(date +%s%N)
    echo "SHOT|$name|$rc|$bytes|$dombytes|$(( (t1 - t0) / 1000000 ))|$size|$dark"
    if [ "$rc" != "0" ]; then
        echo "  stderr: $(tail -3 "$DIR/$name.err" 2>/dev/null | tr '\n' ' ')"
    fi
done
rm -rf "$DIR"/p-* "$DIR"/d-* 2>/dev/null || true
echo "SHOT-DONE"
"""


def parse_size(text):
    m = re.fullmatch(r"\s*(\d+)\s*[xX*]\s*(\d+)\s*", text)
    if not m:
        C.fail("--size expects WxH like 1440x1000, got %r" % text)
    return int(m.group(1)), int(m.group(2))


def inspect_dom(dom_text, expect=None):
    """Cheap but honest render check: what did the browser actually get?

    Script/style bodies are dropped first.  A Vue app keeps its whole template
    as JS string literals, so counting "<div" in the raw dump-dom output happily
    reports dozens of elements for a page that rendered nothing -- that mistake
    makes the check useless exactly when it matters.
    """
    findings = []
    body = dom_text.split("<body", 1)[-1] if "<body" in dom_text else dom_text
    body = re.sub(r"<script\b[^>]*>.*?</script>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<style\b[^>]*>.*?</style>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    text_only = re.sub(r"<[^>]+>", " ", body)
    text_only = re.sub(r"\s+", " ", text_only).strip()
    elements = len(re.findall(r"<(div|span|section|article|canvas|svg|table|button|li|p|h[1-6])\b", body))
    info = {"elements": elements, "text_len": len(text_only), "text": text_only[:120]}
    if elements < 12:
        findings.append("DOM has only %d layout elements -- page probably did not render" % elements)
    if len(text_only) < 40:
        findings.append("rendered text is only %d chars (%r)" % (len(text_only), text_only[:60]))
    hits = [tok for tok in LOADING_TOKENS
            if re.search(tok, text_only, flags=re.I)]
    if hits:
        findings.append("DOM still shows loading text %s -- data had not arrived" % hits)
    if expect and not re.search(expect, dom_text):
        findings.append("--expect /%s/ did not match the rendered DOM" % expect)
    return info, findings


def main():
    C.setup_stdio()
    ap = argparse.ArgumentParser(description="Headless screenshot of the pve panel")
    ap.add_argument("--host", default=C.HOST_DEFAULT)
    ap.add_argument("--url", default=None,
                    help="page to shoot (default http://127.0.0.1:<service_port>/)")
    ap.add_argument("--port", type=int, default=None, help="panel port for the default URL")
    ap.add_argument("--preset", action="append", default=[], metavar="NAME",
                    help="desktop | mobile | dark | mobile-dark | tall | all")
    ap.add_argument("--size", default=None, help="WxH, overrides the preset")
    ap.add_argument("--dark", action="store_true", help="emulate prefers-color-scheme: dark")
    ap.add_argument("--mobile", action="store_true", help="shorthand for --preset mobile (390x844)")
    ap.add_argument("--dpr", type=float, default=1, help="device pixel ratio (2 = retina)")
    ap.add_argument("--budget", type=int, default=8000, help="virtual time budget in ms")
    ap.add_argument("--out", default=None, help="output png (single shot) or directory")
    ap.add_argument("--expect", default=None, help="regex that must appear in the rendered DOM")
    ap.add_argument("--no-verify", action="store_true", help="skip the DOM sanity check")
    ap.add_argument("--keep-remote", action="store_true", help="leave the pngs in /tmp on the host")
    args = ap.parse_args()

    # --- what to shoot ---------------------------------------------------
    url = args.url
    if not url:
        port = args.port
        if port is None:
            # The port is baked into the installed manifest at build time.
            app = "fn-pvepanel"
            probe = (
                'set -u; for f in "/var/apps/$1/manifest" "$2/manifest"; do '
                "if sudo test -r \"$f\"; then "
                "sudo awk -F= '/^service_port/ { v = $2; gsub(/[ \\t\\r]/, \"\", v); print v; exit }' \"$f\"; "
                "exit 0; fi; done"
            )
            res = C.ssh(args.host, probe, args=(app, C.app_paths(app)["var"]))
            picked = ""
            for line in (res.out or "").splitlines():
                if line.strip().isdigit():
                    picked = line.strip()
            if picked:
                port = int(picked)
            else:
                fpk = C.newest_fpk()
                if fpk:
                    try:
                        port = int(C.fpk_manifest(fpk).get("service_port") or C.DEFAULT_PORT)
                    except Exception:  # noqa: BLE001
                        pass
        url = "http://127.0.0.1:%d/" % port

    names = list(args.preset)
    if args.mobile and not any(n.startswith("mobile") for n in names):
        names.append("mobile-dark" if args.dark else "mobile")
    if not names:
        names = ["custom"] if (args.size or args.dark) else ["desktop"]
    if "all" in names:
        names = ["desktop", "mobile", "dark", "mobile-dark"]
    specs = []
    for name in names:
        base = dict(PRESETS.get(name, {"size": "1440x1000", "dark": False}))
        if args.size:
            base["size"] = args.size
        if args.dark:
            base["dark"] = True
        specs.append({
            "name": name,
            "url": url,
            "size": base["size"],
            "dark": base["dark"],
            "dpr": args.dpr,
            "budget": args.budget,
        })
    # de-duplicate identical size/dark combos (e.g. --size with --preset all)
    seen = set()
    unique = []
    for s in specs:
        key = (s["size"], s["dark"], s["dpr"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)
    specs = unique

    out_arg = os.path.abspath(args.out) if args.out else C.IMAGE_DIR
    single = len(specs) == 1 and args.out and os.path.splitext(args.out)[1].lower() == ".png"
    out_dir = os.path.dirname(out_arg) if single else out_arg
    os.makedirs(out_dir if single else out_arg, exist_ok=True)

    # 127.0.0.1_8899-... is noise in the filename; the panel has one obvious name.
    path_only = re.sub(r"^https?://", "", url).split("?")[0].rstrip("/")
    if re.match(r"^(127\.0\.0\.1|localhost)(:\d+)?(/.*)?$", path_only):
        slug = "panel"
    else:
        slug = C.slugify(path_only.replace(":", "_"))
    print("shoot  %s" % url)
    print("host   %s:%s  chromium" % (args.host, REMOTE_DIR))
    print("shots  " + ", ".join("%s (%s%s)" % (s["name"], s["size"], ", dark" if s["dark"] else "")
                                for s in specs))
    print("")

    timer = C.Timer()

    argv_args = [REMOTE_DIR, CHROME] + [
        "|".join([s["name"], s["url"], s["size"], "1" if s["dark"] else "0",
                  ("%g" % s["dpr"]), str(s["budget"])])
        for s in specs
    ]
    res = C.ssh(args.host, REMOTE_SHOT, args=argv_args)
    lines = [ln for ln in res.out.splitlines() if ln.startswith("SHOT|") or ln.startswith("  stderr")]
    if res.rc != 0 and not lines:
        sys.stderr.write(res.err)
        C.fail("remote chromium run failed (rc=%d)" % res.rc)
    results = {}
    for line in lines:
        if line.startswith("  stderr"):
            print(line)
            continue
        parts = line.split("|")
        _, name, rc, nbytes, dombytes, ms, size, dark = parts[:8]
        results[name] = {"rc": int(rc), "bytes": int(nbytes), "dombytes": int(dombytes),
                         "ms": int(ms), "size": size, "dark": dark == "1"}
        print("      %-12s rc=%s %8d B png  %7d B dom  %5d ms  %s%s"
              % (name, rc, int(nbytes), int(dombytes), int(ms), size,
                 "  dark" if dark == "1" else ""))
    timer.lap("chromium")

    problems = []
    pulled = []
    for spec in specs:
        name = spec["name"]
        info = results.get(name)
        if not info:
            problems.append("%s: chromium produced no output" % name)
            continue
        if info["rc"] != 0:
            problems.append("%s: chromium exited %d" % (name, info["rc"]))
        if info["bytes"] == 0:
            problems.append("%s: no png written" % name)
            continue
        if info["dombytes"] == 0:
            problems.append("%s: the follow-up --dump-dom run produced nothing" % name)
        if single:
            local = out_arg
        else:
            local = os.path.join(out_dir, "%s-%s.png" % (slug, name))
        C.scp_from(args.host, "%s/%s.png" % (REMOTE_DIR, name), local)
        data = open(local, "rb").read()
        dims = C.png_size(data)
        want = parse_size(spec["size"])
        want_px = (int(round(want[0] * spec["dpr"])), int(round(want[1] * spec["dpr"])))
        if dims != want_px:
            problems.append("%s: png is %s, expected %s" % (name, dims, want_px))
        if len(data) < 6000:
            problems.append("%s: png is only %d bytes -- probably a blank frame" % (name, len(data)))
        pulled.append((name, local, dims, len(data)))

        if not args.no_verify:
            dom = C.ssh(args.host, 'cat "$1"', args=("%s/%s.dom" % (REMOTE_DIR, name),)).out
            dom_info, findings = inspect_dom(dom, args.expect)
            print("      %-12s DOM: %d elements, %d chars text" %
                  (name, dom_info["elements"], dom_info["text_len"]))
            for finding in findings:
                problems.append("%s: %s" % (name, finding))
            if not data and dom_info["text_len"] == 0:
                problems.append("%s: also empty DOM" % name)
    timer.lap("download + verify")

    if not args.keep_remote:
        C.ssh(args.host, 'rm -rf "$1"', args=(REMOTE_DIR,))

    print("")
    for name, local, dims, nbytes in pulled:
        print("PNG %s  %sx%s  %s" % (name, dims[0], dims[1], C.human_size(nbytes)))
        print("    " + local)
    print("")
    print(timer.total())
    if problems:
        for p in problems:
            print("WARN  " + p)
        print("RESULT: %d problem(s) -- look at the pngs above" % len(problems))
        return 1
    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())