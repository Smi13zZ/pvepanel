#!/usr/bin/env python3
"""Build the fn-pvepanel .fpk on a fnOS box and pull the artefact back.

Why not build locally: an fpk must carry the executable bits of cmd/* and
app/ui/index.cgi.  On Windows every file lands as 0666 and the install dies
with code 10111, so the packaging step always happens on Linux.

Pipeline
    [1] source tar (local, junk filtered)
    [2] scp to <host>:/tmp/...
    [3] remote: unpack -> strip CRLF -> chmod 755/644 -> fnpack build
    [4] scp the .fpk back to pve-panel/output/
    [5] verify the packed manifest (version/appname/port) + list the package

Usage
    python tools/build_fpk.py
    python tools/build_fpk.py --host fnos-test --out-dir output
    python tools/build_fpk.py --no-verify
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402


REMOTE_BUILD = r"""set -eu
WD="$1"; APP="$2"
rm -rf "$WD"
mkdir -p "$WD"
tar -xf /tmp/pvepanel-src.tar -C "$WD"
cd "$WD"

# 1) CRLF anywhere in a shell script turns the shebang into /bin/bash\r
#    -> 'bad interpreter'.  Strip on every script-ish file, not a fixed list.
find . -type f \( -name '*.cgi' -o -name '*.sh' -o -path './cmd/*' \) \
     -exec sed -i 's/\r$//' {} \;

# 2) junk that must never reach the package
find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find . -name '*.pyc' -o -name '*.pyo' -o -name '.DS_Store' | xargs -r rm -f

# 3) permissions: dirs 755, files 644, then the few executables 755.
find . -type d -exec chmod 755 {} \;
find . -type f -exec chmod 644 {} \;
chmod 755 cmd/* 2>/dev/null || true
[ -f app/ui/index.cgi ] && chmod 755 app/ui/index.cgi

echo "--- required files ---"
for f in manifest config/resource config/privilege app/ui/config wizard/install; do
    [ -e "$f" ] && echo "  ok   $f" || echo "  MISS $f"
done

echo "--- fnpack ---"
fnpack build
ls -l "$APP.fpk"
"""


def main():
    C.setup_stdio()
    ap = argparse.ArgumentParser(description="Build fn-pvepanel fpk on fnOS and fetch it back")
    ap.add_argument("--host", default=C.HOST_DEFAULT, help="ssh target (default fnos-test)")
    ap.add_argument("--src", default=C.SRC_DEFAULT, help="source tree with the manifest")
    ap.add_argument("--out-dir", default=C.OUTPUT_DIR, help="where the .fpk lands (default pve-panel/output)")
    ap.add_argument("--workdir", default="/tmp/pvepanel-build", help="remote scratch dir")
    ap.add_argument("--no-verify", action="store_true", help="skip the post-build package check")
    ap.add_argument("--no-check", action="store_true", help="skip the pre-build structure check")
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    manifest_path = os.path.join(src, "manifest")
    if not os.path.isfile(manifest_path):
        C.fail("no manifest at %s" % manifest_path)

    man = C.manifest_fields(manifest_path)
    app = man.get("appname", "")
    ver = man.get("version", "")
    plat = man.get("platform", "all")
    if not app or not ver:
        C.fail("manifest is missing appname/version")

    scratch = os.path.join(C.PROJECT_DIR, "tmp")
    os.makedirs(scratch, exist_ok=True)
    local_tar = os.path.join(scratch, "pvepanel-src.tar")
    os.makedirs(args.out_dir, exist_ok=True)

    out_name = "%s-%s-fnos-%s.fpk" % (app, ver, plat)
    out_path = os.path.join(args.out_dir, out_name)

    print("build  %s %s (platform=%s)" % (app, ver, plat))
    print("host   %s:%s" % (args.host, args.workdir))
    print("")

    timer = C.Timer()

    # Gate 1: structure. py_compile only proves the files parse; an indentation
    # slip that moves a method out of its class, or a name used but never
    # defined, both compile fine and only fail on the device at request time.
    if not args.no_check:
        check = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "check_structure.py")
        if os.path.isfile(check):
            res = subprocess.run([sys.executable, check], capture_output=True, text=True)
            tail = [ln for ln in res.stdout.splitlines() if ln.strip()][-2:]
            for ln in tail:
                print("      " + ln)
            if res.returncode != 0:
                sys.stderr.write(res.stdout)
                C.fail("structure check failed - not packaging a broken build")
            timer.lap("structure check")

    # [1] local tar ------------------------------------------------------
    import tarfile

    def _filter(ti):
        name = ti.name.replace("\\", "/")
        parts = name.split("/")
        if any(p in C.SKIP_DIRS for p in parts):
            return None
        if name.endswith(".DS_Store"):
            return None
        if os.path.splitext(name)[1].lower() in C.SKIP_EXT:
            return None
        return ti

    with tarfile.open(local_tar, "w") as tar:
        for name in sorted(os.listdir(src)):
            tar.add(os.path.join(src, name), arcname=name, filter=_filter)
    print("      tar %s (%s)" % (os.path.basename(local_tar), C.human_size(os.path.getsize(local_tar))))
    timer.lap("source tar")

    # [2] upload ---------------------------------------------------------
    C.scp_to(args.host, local_tar, "/tmp/pvepanel-src.tar")
    timer.lap("upload scp")

    # [3] remote build ---------------------------------------------------
    res = C.ssh(args.host, REMOTE_BUILD, args=(args.workdir, app))
    sys.stdout.write(res.out)
    if res.rc != 0:
        if res.err.strip():
            sys.stderr.write("\n--- remote stderr ---\n" + res.err)
        C.fail("remote fnpack build failed (rc=%d)" % res.rc)
    timer.lap("fnpack build")

    # [4] download -------------------------------------------------------
    C.scp_from(args.host, "%s/%s.fpk" % (args.workdir, app), out_path)
    size = os.path.getsize(out_path)
    print("      -> %s (%s, md5 %s)" % (out_path, C.human_size(size), C.md5_of_file(out_path)))
    timer.lap("download scp")

    # [5] verify the package itself --------------------------------------
    if not args.no_verify:
        packed = C.fpk_manifest(out_path)
        problems = []
        if packed.get("appname") != app:
            problems.append("appname %r != %r" % (packed.get("appname"), app))
        if packed.get("version") != ver:
            problems.append("version %r != %r" % (packed.get("version"), ver))
        if not packed.get("checksum"):
            problems.append("no checksum line (fnpack did not finish?)")
        for key in ("service_port", "install_type", "ctl_stop", "checkport"):
            if key not in packed:
                problems.append("manifest packed without %s" % key)
        timer.lap("verify packed manifest")
        if problems:
            for p in problems:
                print("      ! " + p)
            C.fail("package check failed: " + "; ".join(problems))
        print("      packed manifest ok (service_port=%s install_type=%s ctl_stop=%s)"
              % (packed.get("service_port"), packed.get("install_type"), packed.get("ctl_stop")))

    print("")
    print(timer.total())
    print("ARTIFACT " + out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())