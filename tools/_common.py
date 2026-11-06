#!/usr/bin/env python3
r"""Shared helpers for the pve-panel fpk toolchain.

All three tools (build_fpk / deploy / shot) talk to a fnOS box over SSH.
Key auth only -- this module never handles a password, and never writes one
anywhere.  Remote shell snippets are fed to 'bash -s' over stdin so nobody has
to fight Windows quoting.

Run these with the DSH bundled interpreter, e.g.

  C:\Users\PVE\.dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\python\python.exe tools\build_fpk.py
"""

import hashlib
import os
import re
import shlex
import subprocess
import sys
import time

HOST_DEFAULT = "fnos-test"

# Fallback only: the real port always comes from the packed/installed manifest.
# Kept in one place so a port change never has to be chased through the tools.
DEFAULT_PORT = 39205

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TOOLS_DIR)
SRC_DEFAULT = os.path.join(PROJECT_DIR, "src", "fn-pvepanel")
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
IMAGE_DIR = os.path.join(PROJECT_DIR, "image")

SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "LogLevel=ERROR",
]

# Directories that must never end up inside the package.
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".idea", ".vscode"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".fpk", ".tar", ".tgz", ".orig", ".rej"}


# ---------------------------------------------------------------- misc helpers

def setup_stdio():
    """Windows consoles default to cp936; make Chinese output safe."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def fail(msg, code=1):
    print("")
    print("FAIL: " + msg, file=sys.stderr)
    raise SystemExit(code)


def human_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def md5_of_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


class Result:
    def __init__(self, argv, rc, out, err, seconds):
        self.argv = argv
        self.rc = rc
        self.out = out
        self.err = err
        self.seconds = seconds

    def __repr__(self):
        return "<Result rc=%s %.2fs %r>" % (self.rc, self.seconds, self.argv[:2])


def run(argv, input_bytes=None, check=False, quiet=False):
    """Run a local process, capture text output as UTF-8 with replacement."""
    t0 = time.time()
    proc = subprocess.run(
        argv,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    res = Result(argv, proc.returncode, out, err, time.time() - t0)
    if check and proc.returncode != 0:
        if not quiet:
            sys.stderr.write(err or out)
        fail("command failed (rc=%d): %s" % (proc.returncode, " ".join(argv[:3]) + " ..."))
    return res


SPINNER_RE = re.compile(r"^\s*[\\|/\-]\s+(stopping|uninstalling|installing|Verifying files|starting)\b.*$")


def clean_appcenter_log(text):
    """appcenter-cli redraws a spinner; keep the real lines, drop the animation."""
    kept = []
    for line in text.splitlines():
        if SPINNER_RE.match(line):
            continue
        kept.append(line.rstrip())
    # collapse runs of blank lines left behind
    out = []
    blank = 0
    for line in kept:
        if line.strip():
            blank = 0
            out.append(line)
        else:
            blank += 1
            if blank <= 1 and out:
                out.append("")
    return "\n".join(out).strip()


def ssh(host, script, args=(), check=False, quiet=False):
    """Run a bash snippet on the remote host; the script itself goes over stdin.

    ssh joins its trailing arguments with single spaces and hands the result to
    the remote *login shell*, which re-parses it.  Passing a value such as
    'a|b|c' unquoted therefore turns into a pipeline and the call dies with
    rc=127 -- so every argument is shell-quoted here.
    """
    argv = ["ssh"] + SSH_OPTS + [host, "bash -s --"]
    argv += [shlex.quote(str(a)) for a in args]
    res = run(argv, input_bytes=script.encode("utf-8"), check=check, quiet=quiet)
    return res


def scp_to(host, local, remote, check=True):
    return run(["scp", "-q"] + SSH_OPTS + [local, "%s:%s" % (host, remote)], check=check)


def scp_from(host, remote, local, check=True):
    return run(["scp", "-q"] + SSH_OPTS + ["%s:%s" % (host, remote), local], check=check)


# ------------------------------------------------------------- manifest/parsing

def manifest_fields(path):
    """Parse the fnOS ini-style manifest into a dict (first '=' wins, trimmed)."""
    fields = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip().lstrip("\ufeff")
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            fields.setdefault(key.strip(), value.strip())
    return fields


def fpk_manifest(fpk_path):
    """Read the manifest that is actually packed inside an .fpk."""
    import tarfile

    with tarfile.open(fpk_path) as tar:
        try:
            member = tar.extractfile("manifest")
        except KeyError:
            fail("%s has no outer 'manifest' member" % os.path.basename(fpk_path))
        body = member.read().decode("utf-8", "replace")
    fields = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields.setdefault(key.strip(), value.strip())
    return fields


def app_paths(app):
    """Well-known fnOS paths for an installed app."""
    return {
        "var": "/usr/local/apps/@appdata/" + app,   # TRIM_PKGVAR, survives uninstall
        "dest": "/usr/local/apps/@appcenter/" + app,
        "web": "/var/apps/%s/target" % app,
        "log": "/var/log/apps/%s.log" % app,
    }


def newest_fpk(out_dir=OUTPUT_DIR):
    if not os.path.isdir(out_dir):
        return None
    cands = [
        os.path.join(out_dir, name)
        for name in os.listdir(out_dir)
        if name.endswith(".fpk") and os.path.isfile(os.path.join(out_dir, name))
    ]
    if not cands:
        return None
    cands.sort(key=lambda p: (os.path.getmtime(p), p))
    return cands[-1]


# ---------------------------------------------------------------------- timing

class Timer:
    """Step timer that prints '[n] label ... 0.4s' lines like the old scripts."""

    def __init__(self):
        self.t0 = time.time()
        self.n = 0
        self.marks = []

    def lap(self, label):
        now = time.time()
        self.n += 1
        self.marks.append((label, now - self.t0))
        print("[%d] %-24s %5.1fs" % (self.n, label, now - self.t0))
        return now - self.t0

    def total(self):
        return "TOTAL %.1fs" % (time.time() - self.t0)


def png_size(data):
    """Return (width, height) from a PNG header, or None."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))


def slugify(text):
    text = re.sub(r"https?://", "", text)
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return text or "shot"
