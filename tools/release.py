#!/usr/bin/env python3
"""release.py - check CI, inspect the published package, and confirm the panel
sees the new release.

The three things worth automating after a tag push, because each one has caught
a real problem that "CI is green" did not:

  1. the Actions run for this tag actually succeeded (and which step failed)
  2. the published .fpk downloads, matches its advertised size, and passes
     inspect_fpk --strict (exec bits, nine icons, version)
  3. a panel on the test box reads that release back through its own update
     check - i.e. the release is discoverable, not just uploaded

Uses the Git credential manager for the token so no secret is written anywhere.
The token is fine-grained and needs Contents: read/write.

    python tools/release.py v26.11.7
    python tools/release.py v26.11.7 --skip-panel
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DEFAULT = "Smi13zZ/pvepanel"
GCM = r"C:\Users\PVE\AppData\Local\Programs\Git\ucrt64\bin\git-credential-manager.exe"
# The panel only ever runs on an fnOS box, so the useful default is the test
# machine tunnelled to a local port rather than this workstation's localhost.
PANEL = os.environ.get("PVEPANEL_URL", "http://127.0.0.1:18899")


def token():
    inp = "protocol=https\nhost=github.com\n\n"
    try:
        out = subprocess.run([GCM, "get"], input=inp, capture_output=True,
                             text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit("cannot read the git credential: %r" % (exc,))
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise SystemExit("no github token in the credential manager")


class Api:
    def __init__(self):
        self.tok = token()

    def get(self, path):
        req = urllib.request.Request("https://api.github.com" + path)
        req.add_header("Authorization", "Bearer " + self.tok)
        req.add_header("Accept", "application/vnd.github+json")
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def download(self, url, dest):
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + self.tok)
        req.add_header("Accept", "application/octet-stream")
        with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
            f.write(r.read())


def wait_for_run(api, repo, tag, timeout=1200):
    deadline = time.time() + timeout
    seen = None
    while time.time() < deadline:
        code, data = api.get("/repos/%s/actions/runs?per_page=20" % repo)
        runs = [r for r in (data.get("workflow_runs") or [])
                if r.get("head_branch") == tag]
        if runs:
            run = runs[0]
            if run.get("status") != (seen or ""):
                print("  %-12s %s" % (run.get("status"), run.get("html_url")))
                seen = run.get("status")
            if run.get("status") == "completed":
                return run
        time.sleep(15)
    return None


def main():
    ap = argparse.ArgumentParser(description="Verify a published PVEpanel release")
    ap.add_argument("tag", help="release tag, e.g. v26.11.7")
    ap.add_argument("--repo", default=REPO_DEFAULT)
    ap.add_argument("--panel", default=PANEL,
                    help="panel base URL for the update check "
                         "(default: $PVEPANEL_URL or a tunnel on :18899)")
    ap.add_argument("--skip-panel", action="store_true",
                    help="skip the on-device update-check probe")
    args = ap.parse_args()

    api = Api()
    fails = []

    print("== 1) workflow run for " + args.tag)
    run = wait_for_run(api, args.repo, args.tag)
    if run is None:
        print("  no run finished in time")
        fails.append("no CI run")
    else:
        code, jobs = api.get("/repos/%s/actions/runs/%s/jobs" % (args.repo, run["id"]))
        for j in jobs.get("jobs") or []:
            print("  job %s -> %s" % (j["name"], j.get("conclusion")))
            for s in j.get("steps") or []:
                mark = {"success": "ok  ", "failure": "FAIL",
                        "skipped": "skip"}.get(s.get("conclusion") or "", "..  ")
                print("      %s %s" % (mark, s["name"]))
        if run.get("conclusion") != "success":
            fails.append("CI %s" % run.get("conclusion"))

    print("== 2) the published package")
    code, rel = api.get("/repos/%s/releases/tags/%s" % (args.repo, args.tag))
    if code != 200:
        print("  no release for this tag (HTTP %s)" % code)
        fails.append("no release")
    else:
        assets = [a for a in rel.get("assets") or [] if a["name"].endswith(".fpk")]
        if not assets:
            print("  release has no .fpk")
            fails.append("no fpk asset")
        else:
            asset = assets[0]
            os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
            dest = os.path.join(ROOT, "output", "_release_" + asset["name"])
            api.download(asset["url"], dest)
            size = os.path.getsize(dest)
            sha = hashlib.sha256(open(dest, "rb").read()).hexdigest()
            print("  %s  %d bytes  sha256=%s" % (asset["name"], size, sha[:16]))
            if size != asset["size"]:
                fails.append("size mismatch")
                print("  ! advertised %d, downloaded %d" % (asset["size"], size))
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            res = subprocess.run(
                [sys.executable, os.path.join(ROOT, "tools", "inspect_fpk.py"),
                 dest, "--strict"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env)
            tail = [ln for ln in (res.stdout or "").splitlines() if ln.strip()][-6:]
            for line in tail:
                print("  " + line)
            if res.returncode != 0:
                fails.append("inspect_fpk")
            with tarfile.open(dest, "r:gz") as tf:
                man = tf.extractfile("manifest").read().decode()
            ver = ""
            for line in man.splitlines():
                if line.strip().startswith("version"):
                    ver = line.split("=", 1)[1].strip()
            print("  packed version=%s  tag=%s" % (ver, args.tag))
            if ver != args.tag.lstrip("v"):
                fails.append("version/tag mismatch")
            try:
                os.remove(dest)
            except OSError:
                pass

    print("== 3) the panel reads the release back")
    if args.skip_panel:
        print("  skipped")
    else:
        try:
            with urllib.request.urlopen(
                    args.panel + "/api/update/check?force=1", timeout=120) as r:
                info = json.loads(r.read().decode())
            print("  current=%s latest=%s has_update=%s source=%s" % (
                info.get("current"), info.get("latest"),
                info.get("has_update"), info.get("source")))
            print("  assets=%s" % [(a["name"], a["size"])
                                   for a in (info.get("assets") or [])])
            if not info.get("ok"):
                fails.append("panel update check failed: %s" % (info.get("reason"),))
            elif info.get("latest") != args.tag.lstrip("v"):
                fails.append("panel sees %r, expected %r"
                             % (info.get("latest"), args.tag.lstrip("v")))
        except Exception as exc:  # noqa: BLE001
            print("  could not reach the panel: %r" % (exc,))
            print("  (the panel runs on fnOS, not here - tunnel it first:")
            print("   ssh -N -L 18899:127.0.0.1:8899 fnos-test)")
            fails.append("panel unreachable")

    print("")
    if fails:
        print("FAILED: " + ", ".join(fails))
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
