#!/usr/bin/env python3
"""Inspect an .fpk without installing it.

Checks the things that have historically broken installs:
  * required outer members (manifest / app.tgz / config/resource / ICON*)
  * exec bits on cmd/* and ui/index.cgi (missing -> install error 10111)
  * nine icon sizes referenced by ui/config (missing -> blank icon, no error)
  * __pycache__ / *.pyc / .DS_Store junk
  * CRLF inside the CGI script (-> 'bad interpreter')
  * the version actually packed, compared with the source manifest

Usage
    python tools/inspect_fpk.py                      # newest in output/
    python tools/inspect_fpk.py output/x.fpk
    python tools/inspect_fpk.py --strict             # exit 1 on any warning
"""

import argparse
import io
import os
import re
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402

ICON_SIZES = [16, 24, 48, 64, 72, 96, 128, 256, 512]
REQUIRED_OUTER = ["manifest", "app.tgz", "config/resource", "config/privilege", "ICON.PNG", "ICON_256.PNG"]
REQUIRED_INNER = ["ui/config", "ui/index.cgi", "web/index.html"]


def main():
    C.setup_stdio()
    ap = argparse.ArgumentParser(description="Inspect an fpk")
    ap.add_argument("path", nargs="?", default=None, help="fpk path (default: newest in output/)")
    ap.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    args = ap.parse_args()

    path = args.path or C.newest_fpk()
    if not path or not os.path.isfile(path):
        C.fail("no fpk found (build one first, or pass a path)")
    path = os.path.abspath(path)

    warnings = []
    failures = []

    print("=== %s (%s, md5 %s) ===" % (os.path.basename(path), C.human_size(os.path.getsize(path)),
                                      C.md5_of_file(path)))
    with tarfile.open(path) as outer:
        members = {m.name: m for m in outer.getmembers()}
        print("\n[outer] %d members" % len(members))
        for name in sorted(members):
            m = members[name]
            if m.isfile():
                print("  %-24s %s %9d B" % (name, oct(m.mode), m.size))
            elif m.isdir():
                print("  %-24s %s" % (name + "/", oct(m.mode)))

        for req in REQUIRED_OUTER:
            if req not in members:
                failures.append("outer member missing: " + req)

        manifest = C.fpk_manifest(path)
        print("\n[manifest as packed]")
        for key in sorted(manifest):
            value = manifest[key]
            if len(value) > 90:
                value = value[:87] + "..."
            print("  %-22s = %s" % (key, value))

        noexec = [
            m.name for m in members.values()
            if m.name.startswith("cmd/") and m.isfile() and not (m.mode & 0o111)
        ]
        print("\n[cmd exec bits] %s" % ("OK (%d scripts 0755)"
                                        % len([m for m in members.values() if m.name.startswith("cmd/")])
                                        if not noexec else "FAIL " + ", ".join(noexec)))
        if noexec:
            failures.append("cmd/* without exec bit -> install error 10111")

        if "app.tgz" in members:
            inner = tarfile.open(fileobj=io.BytesIO(outer.extractfile("app.tgz").read()))
            imembers = {m.name: m for m in inner.getmembers()}
            print("\n[app.tgz] %d members" % len(imembers))
            for name in sorted(imembers):
                m = imembers[name]
                if m.isfile():
                    print("  %-34s %s %9d B" % (name, oct(m.mode), m.size))

            for req in REQUIRED_INNER:
                if req not in imembers:
                    failures.append("app.tgz missing: " + req)

            cgi = imembers.get("ui/index.cgi")
            if cgi is None:
                failures.append("ui/index.cgi missing")
            else:
                if not (cgi.mode & 0o111):
                    failures.append("ui/index.cgi not executable -> install error 10111")
                print("\n[ui/index.cgi] %s %s" % (oct(cgi.mode),
                                                  "0755 OK" if cgi.mode & 0o111 else "NOT EXECUTABLE"))
                body = inner.extractfile(cgi).read()
                if b"\r\n" in body:
                    failures.append("ui/index.cgi still has CRLF -> 'bad interpreter'")
                print("[ui/index.cgi CRLF] %s" % ("YES (bad)" if b"\r\n" in body else "no"))
                if not body.startswith(b"#!"):
                    failures.append("ui/index.cgi has no shebang")

            for name in ("ui/config", "web/index.html"):
                if name in imembers:
                    body = inner.extractfile(imembers[name]).read()
                    if b"\r\n" in body:
                        warnings.append("%s has CRLF" % name)

            uicfg = imembers.get("ui/config")
            if uicfg is not None:
                try:
                    cfg = inner.extractfile(uicfg).read().decode("utf-8", "replace")
                    tpl = re.search(r'"icon"\s*:\s*"([^"]+)"', cfg).group(1)
                    missing = [
                        s for s in ICON_SIZES
                        if "ui/" + tpl.replace("{0}", str(s)) not in imembers
                    ]
                    print("[icon steps] %s -> %s" % (
                        tpl, "OK 9/9" if not missing else "MISSING " + str(missing)))
                    if missing:
                        warnings.append("icon steps missing: %s (blank icon on some devices)" % missing)
                    launch = re.search(r'"([\w.\-]+\.Application)"', cfg)
                    if launch:
                        expected = manifest.get("desktop_applaunchname", "")
                        got = launch.group(1)
                        if expected and expected != got:
                            failures.append("desktop_applaunchname %r != ui/config key %r"
                                            % (expected, got))
                        else:
                            print("[desktop entry] %s" % got)
                except Exception as exc:  # noqa: BLE001
                    warnings.append("ui/config unreadable: %s" % exc)

            crud = [n for n in imembers
                    if "__pycache__" in n or n.endswith((".pyc", ".pyo", ".DS_Store"))]
            print("[crud] %s" % ("none" if not crud else crud))
            if crud:
                warnings.append("crud inside app.tgz: %s" % crud)

    src_manifest = os.path.join(C.SRC_DEFAULT, "manifest")
    if os.path.isfile(src_manifest):
        src = C.manifest_fields(src_manifest)
        if src.get("version") != manifest.get("version"):
            warnings.append("packed version %s != source manifest %s (stale build?)"
                            % (manifest.get("version"), src.get("version")))
        else:
            print("\n[version] %s matches src/manifest" % manifest.get("version"))

    print("")
    for w in warnings:
        print("WARN  " + w)
    for f in failures:
        print("FAIL  " + f)
    if failures:
        print("\nRESULT: FAIL (%d)" % len(failures))
        return 1
    if warnings and args.strict:
        print("\nRESULT: WARN (%d) -- strict mode" % len(warnings))
        return 1
    print("RESULT: OK%s" % ("" if not warnings else " (with %d warning(s))" % len(warnings)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
