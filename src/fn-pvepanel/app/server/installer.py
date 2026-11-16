#!/usr/bin/env python3
"""installer.py - the one-click upgrade.

Turns "a newer release exists" into "a newer release is installed". Three
things make this harder than it looks, and each one shapes the design:

1. **fnOS cannot upgrade in place.** appcenter-cli install-fpk on an installed
   app fails with 18000, so the only order that works is stop -> uninstall ->
   install. Which means the step that replaces the app is the step that deletes
   the process running this code.

2. **Therefore the install cannot be a child of this process.** It is written
   to a script and spawned detached (start_new_session), so the uninstall
   killing the service does not take the installer with it. The script lives in
   @appdata, which fnOS deliberately keeps across an uninstall - a copy in
   @appcenter would be deleted at exactly the wrong moment.

3. **Nothing may be uninstalled until the replacement is proven good.** The
   package is downloaded to a .part file, moved into place, then opened as a
   tar and checked for the members fnpack always writes (manifest + app.tgz),
   the expected appname and the expected version. Only then is it handed to the
   script. A truncated download or the wrong asset must cost a failed update,
   never a missing application.

The previous package is kept as update/rollback.fpk; if the new build does not
report itself as running, the script puts the old one back.

Everything here is standard library. Nothing in this module reads a secret: the
download URL comes from the public release metadata and carries no credential.
"""

import http.client
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request

APP_NAME = "fn-pvepanel"
# The state machine the API exposes. "failed" is terminal until the next attempt.
#
# "checking" is a phase of its own rather than being folded into "downloading":
# between the click and the first byte there is a real network round trip, and
# reporting that as a download shows a progress bar that cannot move until the
# round trip is over. The browser renders "checking" as "preparing", which is
# the truth.
PHASES = ("idle", "checking", "downloading", "installing", "failed")

#: Phases in which a second click must be refused. Derived from PHASES so a new
#: phase cannot be added above and silently become re-entrant.
ACTIVE_PHASES = tuple(p for p in PHASES if p not in ("idle", "failed"))


class UpdateError(ValueError):
    """A failure carrying a language-neutral code the browser can translate.

    Subclasses ValueError on purpose: every caller (and every test) that catches
    ValueError and prints the sentence keeps working, while the code travels
    alongside for interfaces that are not Chinese. detail is the fallback text
    for a human when the code has no translation yet.
    """

    def __init__(self, code, detail="", arg=""):
        ValueError.__init__(self, detail or code)
        self.code = code
        self.arg = arg


def error_code(exc):
    """Stable identifier for a failure the browser can translate.

    Empty string means "no known code" - the caller then shows the detail text.
    Unwrapping urllib's URLError matters: it hides the real cause (a gaierror, a
    refused connection) one level down.
    """
    if isinstance(exc, UpdateError):
        return exc.code
    if isinstance(exc, urllib.error.HTTPError):
        code = int(getattr(exc, "code", 0) or 0)
        if code in (403, 429):
            return "gh403"
        if code == 404:
            return "gh404"
        return ""
    if isinstance(exc, urllib.error.URLError):
        inner = getattr(exc, "reason", None)
        return error_code(inner) if inner is not None else "netReset"
    # socket.gaierror before the generic timeout branch: it is an OSError but
    # never a timeout, and mislabeling it would point at the wrong fix.
    if isinstance(exc, socket.gaierror):
        return "netDns"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "netTimeout"
    if isinstance(exc, ConnectionRefusedError):
        return "netRefused"
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "netReset"
    if isinstance(exc, ssl.SSLError):
        return "netTls"
    return ""

DOWNLOAD_LIMIT = 128 * 1024 * 1024
CHUNK = 256 * 1024
HTTP_TIMEOUT = 180
LOG_TAIL_LINES = 12

# Where appcenter-cli actually lives on fnOS. shutil.which() covers the normal
# case, but the panel is started by the app framework with a minimal PATH, so
# the absolute paths are checked too rather than trusting PATH alone.
APPCENTER_CANDIDATES = (
    "/usr/local/bin/appcenter-cli",
    "/usr/bin/appcenter-cli",
    "/usr/sbin/appcenter-cli",
    "/vol1/@appcenter/appcenter-cli",
)

# Directories that may still hold the package matching the running build. Only
# used to make rollback possible; a miss simply means "no rollback available".
#
# The update folder is NOT here because it is not a constant: it depends on
# TRIM_PKGVAR and is searched explicitly, first, by find_previous_fpk(). It has
# to come first - it is where this panel downloads every package, so after one
# upgrade the package that is currently running is sitting right in it. Leaving
# it out was the reason the installer's rollback branch never ran on any device
# that had upgraded through the panel.
PREV_SEARCH_DIRS = (
    "/vol1/@appshare/%s",
    "/vol1/@appshare",
    "/tmp",
)

_LOCK = threading.RLock()
_STATE = {
    "phase": "idle",
    "version": "",
    # Language-neutral reason code; empty means "use detail as-is".
    "code": "",
    "detail": "",
    "done": 0,
    "total": 0,
    "at": 0,
    # Bumped once per install attempt. The browser uses it to tell "the failure
    # I already reported" from "the failure of the run I just started": the
    # state never returns to idle by itself, so without a counter the first poll
    # of a retry reads the previous run's death notice.
    "attempt": 0,
}
# var_dir is needed by status() to find install.log, and current is echoed back
# to the UI; both are configured once by the service and then read lock-free.
_CTX = {"var_dir": "", "current": ""}

_LOG = None


def _noop(_msg):
    pass


def _emit(msg):
    fn = _LOG
    if callable(fn):
        try:
            fn(msg)
        except Exception:
            pass


def set_context(var_dir=None, current=None):
    """Tell the module where its files live and which build is running.

    status() takes no arguments (it is polled directly by an HTTP handler), so
    it needs this context installed once at startup or before a test.
    """
    with _LOCK:
        if var_dir is not None:
            _CTX["var_dir"] = str(var_dir)
        if current is not None:
            _CTX["current"] = str(current)


def reset():
    """Back to a clean idle state. Used by tests, and by the service on boot."""
    with _LOCK:
        _STATE.update({
            "phase": "idle", "version": "", "code": "", "detail": "",
            "done": 0, "total": 0, "at": 0,
        })


def _set(phase, version="", detail="", done=None, total=None, log=None,
         code=""):
    """Update the shared progress state.

    The byte counters are only overwritten when a caller passes them. A new
    download seeds 0/0 and the transfer fills them in; the later phases leave
    them alone, so the bar stays at whatever the download reached instead of
    snapping back to empty the moment the install starts.
    """
    if log is not None:
        global _LOG
        _LOG = log
    with _LOCK:
        _STATE["phase"] = phase
        _STATE["version"] = version
        _STATE["code"] = code or ""
        _STATE["detail"] = detail
        _STATE["at"] = int(time.time())
        if done is not None:
            _STATE["done"] = int(done)
        if total is not None:
            _STATE["total"] = int(total)


def _fail(exc, version=""):
    """Publish a failure twice: readable text for the log, a code for the UI."""
    detail = str(exc) or exc.__class__.__name__
    _set("failed", version, detail, code=error_code(exc))
    return detail


def _percent(done, total):
    """Whole percent, 0 when the server did not send a length.

    Unknown total stays 0 rather than pretending to be 100: a progress bar that
    claims to be finished while bytes are still arriving is worse than an
    indeterminate one.
    """
    try:
        done = int(done)
        total = int(total)
    except (TypeError, ValueError):
        return 0
    if total <= 0:
        return 0
    if done <= 0:
        return 0
    return max(0, min(100, int(done * 100 / total)))


def _update_dir(var_dir=None):
    base = var_dir or _CTX.get("var_dir") or os.environ.get("TRIM_PKGVAR") or ""
    if not base:
        return ""
    return os.path.join(base, "update")


def _install_log_path(var_dir=None):
    d = _update_dir(var_dir)
    return os.path.join(d, "install.log") if d else ""


def _tail(path, lines=LOG_TAIL_LINES):
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    rows = text.strip().splitlines()
    return "\n".join(rows[-lines:])


def status():
    """The current progress snapshot plus the tail of the installer's log.

    The log is read from disk on every call on purpose: the interesting lines
    are written by the detached script *after* this process has been killed, so
    the file is the only channel that survives.

    rollback reports whether the installer actually has an older package to fall
    back to. The browser needs it before anything goes wrong: promising an
    automatic rollback that the script cannot perform is worse than saying so.
    """
    with _LOCK:
        out = dict(_STATE)
        out["current"] = _CTX.get("current") or ""
        out["percent"] = _percent(out.get("done"), out.get("total"))
    updir = _update_dir()
    out["rollback"] = bool(updir) and os.path.isfile(os.path.join(updir, "rollback.fpk"))
    out["log"] = _tail(_install_log_path())
    return out


# ------------------------------------------------------------------- assets

def pick_asset(info):
    """Choose the fpk to install out of a release's asset list.

    The platform-suffixed build goes first ("-all" is what the fpk build script
    emits for every architecture). Anything that is not an .fpk - checksums,
    build logs, source archives - is ignored: downloading one of those and
    uninstalling first would destroy the installation.
    """
    assets = (info or {}).get("assets") or []
    fpks = []
    for a in assets:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        url = str(a.get("url") or "")
        if not name.lower().endswith(".fpk") or not url:
            continue
        fpks.append(a)
    if not fpks:
        return None
    for a in fpks:
        if "-all" in str(a.get("name") or "").lower():
            return a
    return fpks[0]


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(name):
    """Reduce an asset name to something harmless to drop into a shell script.

    The name comes from a remote JSON document and ends up inside a quoted
    argument, so it is filtered rather than trusted.
    """
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = _SAFE_NAME.sub("-", base).strip("-.")
    if not base.lower().endswith(".fpk"):
        base = (base or APP_NAME) + ".fpk"
    return base or (APP_NAME + ".fpk")


# ----------------------------------------------------------------- download

def download(url, dest, limit=DOWNLOAD_LIMIT, on_progress=None, timeout=HTTP_TIMEOUT):
    """Stream a URL to dest, returning the byte count.

    Written to dest + ".part" and renamed only after the last byte arrives: a
    half-transferred file must never sit at the path the validator is about to
    open, otherwise a dropped connection looks exactly like a good download.
    """
    if not str(url or "").strip():
        raise UpdateError("noAsset", "下载地址为空")
    folder = os.path.dirname(os.path.abspath(dest))
    if folder:
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            raise UpdateError("noDir", "无法创建下载目录：%s" % exc)
    part = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
    total = 0
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        raise UpdateError(error_code(exc) or "checkFailed",
                          "下载失败：HTTP %d" % code)
    except Exception as exc:
        # Every network cause keeps its own code: "DNS is broken" and "the
        # server refused" need different fixes and must not read the same.
        raise UpdateError(error_code(exc) or "checkFailed", "下载失败：%s" % (exc,))
    try:
        with resp:
            try:
                expect = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                expect = 0
            if on_progress:
                on_progress(0, expect)
            with open(part, "wb") as f:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise UpdateError("pkgTooBig", "安装包超过 %d MB，已中止"
                                         % (limit // 1024 // 1024))
                    f.write(chunk)
                    if on_progress:
                        on_progress(total, expect)
                f.flush()
                os.fsync(f.fileno())
    except UpdateError:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise
    except http.client.IncompleteRead:
        # The server promised Content-Length and hung up early. Caught
        # explicitly because a half-downloaded package is the one failure that
        # would otherwise look like a successful download.
        try:
            os.unlink(part)
        except OSError:
            pass
        raise UpdateError("netReset", "下载不完整：只收到 %d 字节（连接中断）" % total)
    except (socket.timeout, TimeoutError):
        try:
            os.unlink(part)
        except OSError:
            pass
        raise UpdateError("netTimeout", "下载超时：只收到 %d 字节" % total)
    except Exception as exc:
        # Never leave a partial file behind for the next step to trip over.
        try:
            os.unlink(part)
        except OSError:
            pass
        raise UpdateError(error_code(exc) or "netReset", "下载中断：%s" % (exc,))
    if total <= 0:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise UpdateError("netReset", "下载内容为空")
    # A chunked read() reports EOF by returning b"", not by raising - so a
    # server that promises Content-Length and hangs up early looks exactly like
    # a complete download. Comparing the byte count against the promised length
    # is the only way to catch it, and a truncated package must never reach
    # validate_fpk's successors (the uninstall).
    if expect > 0 and total != expect:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise UpdateError("netReset",
                          "下载不完整：收到 %d 字节，应为 %d 字节" % (total, expect))
    try:
        os.replace(part, dest)
    except OSError as exc:
        raise UpdateError("pkgUnreadable", "无法保存安装包：%s" % exc)
    return total


# ----------------------------------------------------------------- validate

def _find_appcenter(explicit=""):
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    found = shutil.which("appcenter-cli")
    if found:
        return found
    for cand in APPCENTER_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return ""


def _manifest_fields(raw):
    fields = {}
    for line in (raw or "").splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # first occurrence wins, matching how fnpack and _common read it
        fields.setdefault(key.strip(), value.strip())
    return fields


def validate_fpk(path, app_name=APP_NAME, appcenter=None, expect_version=None):
    """Refuse to uninstall anything unless the package really is our new build.

    Raises UpdateError (a ValueError) with a sentence the UI can show. Called
    before the script is even written, because the uninstall inside that script
    is irreversible from the panel's point of view.

    appcenter: an explicit path to appcenter-cli; None means search the usual
    places. Tests pass a stand-in so the check is exercised off-device.
    """
    import tarfile

    if not path or not os.path.isfile(path):
        raise UpdateError("pkgUnreadable", "安装包不存在")
    if os.path.getsize(path) == 0:
        raise UpdateError("pkgUnreadable", "安装包是空文件")
    try:
        with tarfile.open(path, "r:gz") as tf:
            names = tf.getnames()
            # fnpack always writes both; a release asset missing either is
            # either a different kind of archive or a broken one.
            if "manifest" not in names:
                raise UpdateError("pkgStructure", "安装包缺少 manifest")
            if "app.tgz" not in names:
                raise UpdateError("pkgStructure", "安装包缺少 app.tgz")
            raw = tf.extractfile("manifest").read().decode("utf-8", "replace")
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError("pkgUnreadable", "安装包无法解压：%s" % (exc,))

    fields = _manifest_fields(raw)
    found = fields.get("appname") or ""
    if found != app_name:
        raise UpdateError("pkgAppName", "安装包应用名不匹配：%s" % (found or "?"),
                          arg=found or "?")

    if expect_version is not None:
        want = str(expect_version or "").strip().lstrip("vV")
        have = (fields.get("version") or "").strip().lstrip("vV")
        if want and have and have != want:
            raise UpdateError("pkgVersion",
                              "安装包版本不匹配：期望 %s，实际 %s" % (want, have),
                              arg="%s / %s" % (want, have))
        if not have:
            raise UpdateError("pkgStructure", "安装包 manifest 里没有版本号")

    cli = _find_appcenter(appcenter or "")
    if not cli:
        raise UpdateError("noCli", "找不到 appcenter-cli，无法自动安装")


# ----------------------------------------------------------------- rollback

def find_previous_fpk(current, app_name=APP_NAME, extra_dirs=(), exclude=(),
                      var_dir=None):
    """Locate the package matching the running version, for the rollback step.

    The update folder is searched FIRST and it is the one that matters: that is
    where every download lands, so the build that is running right now is
    sitting in there after any upgrade made through this panel. Searching only
    the shared folders and /tmp left the rollback empty on exactly the devices
    that had used one-click update - the installer's "put the old one back"
    branch then never ran at all.

    When several copies match, the newest by mtime wins: a re-download updates
    the file, and picking an older copy of the same version risks installing a
    package that was once truncated. exclude drops the file this run just
    downloaded, which has the new version number and would otherwise never
    match the running one anyway.

    Not finding one is normal (a fresh install has no earlier package on disk),
    and is reported as an empty string rather than an error: the update still
    proceeds, it just cannot roll back.
    """
    want = str(current or "").strip()
    if not want:
        return ""
    prefix = "%s-%s" % (app_name, want)
    skip = set()
    for p in exclude:
        try:
            skip.add(os.path.abspath(p))
        except OSError:
            continue
    dirs = []
    updir = _update_dir(var_dir)
    if updir:
        dirs.append(updir)
    dirs.extend(extra_dirs)
    for pattern in PREV_SEARCH_DIRS:
        dirs.append(pattern % app_name if "%s" in pattern else pattern)
    seen = set()
    best = ""
    best_t = -1.0
    for folder in dirs:
        if not folder or folder in seen:
            continue
        seen.add(folder)
        try:
            entries = os.listdir(folder)
        except OSError:
            continue
        for fn in entries:
            if not fn.startswith(prefix) or not fn.lower().endswith(".fpk"):
                continue
            full = os.path.join(folder, fn)
            try:
                if not os.path.isfile(full) or os.path.abspath(full) in skip:
                    continue
                stamp = os.path.getmtime(full)
            except OSError:
                continue
            if stamp > best_t:
                best, best_t = full, stamp
    return best


# ------------------------------------------------------------------- script

# The installer that runs detached. Tokens are replaced as literal text rather
# than with % formatting so the shell's own printf/date percent signs need no
# escaping and cannot corrupt the substitution.
#
# Raw string on purpose: this is shell source, and the two-character sequence
# that printf uses for a newline must reach the script verbatim. As a normal
# triple-quoted string that sequence became a real line break, which left the
# printf format string unterminated and broke the installer on the device.
# A syntax check passes either way, which is why it went unnoticed until the
# script actually ran.
_SCRIPT = r"""#!/bin/sh
# fn-pvepanel one-click update.
#
# Runs detached from the panel: it stops and uninstalls the very service that
# spawned it, so it must not share that process group. Everything it does is
# appended to the log below, which is the only channel left once the old
# process is gone.
set -u

LOG="@LOG@"
exec >>"$LOG" 2>&1
echo "=== $(date '+%F %T') update to @VERSION@ ==="

APP="@APP@"
PKG="@PKG@"
PREV="@PREV@"
ENVF="@ENVF@"
PORT="@PORT@"

if [ ! -f "$PKG" ]; then
  echo "=== package is missing, nothing was changed: $PKG ==="
  exit 1
fi

# Give the browser a moment to read the 'installing' state it is polling for.
sleep 2

# Keep the SAME port across the update: the browser tab that started this
# update is polling a fixed port and must reconnect to it. That is the one case
# where pinning the port is correct, so it is passed explicitly rather than left
# to the app's own remembered value (which the uninstall does not preserve).
printf '%s\n' "BACKEND_PORT=$PORT" > "$ENVF" || {
  echo "=== cannot write the env file, aborting ==="
  exit 1
}

echo "=== stop $APP ==="
appcenter-cli stop "$APP" 2>&1 || true
sleep 1

echo "=== uninstall $APP ==="
appcenter-cli uninstall "$APP" 2>&1 || true
sleep 3

echo "=== install $PKG ==="
appcenter-cli install-fpk "$PKG" -e "$ENVF" -v 1 2>&1 || true

# install-fpk returns before the app centre has finished registering the app,
# so start right away often answers 10500 and looks like a failure while the
# system brings it up a few seconds later. Poll the status instead of trusting
# either return code.
started=0
n=0
last=""
while [ "$n" -lt 8 ]; do
  if appcenter-cli status "$APP" 2>/dev/null | grep -qE 'running|starting'; then
    started=1
    break
  fi
  last=$(appcenter-cli start "$APP" 2>&1 || true)
  sleep 3
  if appcenter-cli status "$APP" 2>/dev/null | grep -qE 'running|starting'; then
    started=1
    break
  fi
  n=$((n + 1))
  echo "=== start attempt $n did not report running, retrying ==="
done

if [ "$started" = "1" ]; then
  echo "=== update to @VERSION@ finished ==="
  exit 0
fi

echo "=== new package did not start ==="
[ -n "$last" ] && echo "$last"

if [ -n "$PREV" ] && [ -f "$PREV" ]; then
  echo "=== rolling back to the previous package ==="
  appcenter-cli uninstall "$APP" 2>&1 || true
  sleep 3
  appcenter-cli install-fpk "$PREV" -e "$ENVF" -v 1 2>&1 || true
  appcenter-cli start "$APP" 2>&1 || true
  echo "=== rollback attempted ==="
else
  echo "=== no previous package available, cannot roll back ==="
fi
exit 1
"""


def _valid_port(port):
    try:
        value = int(str(port).strip())
    except (TypeError, ValueError):
        raise ValueError("端口不是数字：%r" % (port,))
    if not 1 <= value <= 65535:
        raise ValueError("端口超出范围：%d" % value)
    return value


def build_script(version, port, pkg, prev, log_path):
    """Render the detached installer.

    Kept as a separate function so the text can be asserted on without running
    it, and so a bad port or an empty path fails here rather than halfway
    through an uninstall.
    """
    if not str(pkg or "").strip():
        raise ValueError("安装包路径为空")
    if not str(log_path or "").strip():
        raise ValueError("日志路径为空")
    local = {"VERSION": str(version or "").strip() or "unknown",
             "PORT": str(_valid_port(port)),
             "PKG": str(pkg),
             "PREV": str(prev or ""),
             "LOG": str(log_path),
             "APP": APP_NAME,
             "ENVF": os.path.join(os.path.dirname(str(log_path)) or ".",
                                  "update.env")}
    text = _SCRIPT
    for key, value in local.items():
        text = text.replace("@" + key + "@", value)
    return text


# -------------------------------------------------------------------- start

def _refuse(code, detail, **extra):
    """A refusal shaped like every other failure: code plus fallback text."""
    out = {"ok": False, "code": code, "error": detail}
    out.update(extra)
    return out


def start(info, current, port, var_dir, log=None, *, prev_fpk="",
          appcenter="", app_name=APP_NAME, spawn=None, timeout=HTTP_TIMEOUT,
          limit=DOWNLOAD_LIMIT, resolve=None):
    """Download, verify and stage the update, then hand it to the script.

    Returns {"ok": True, "version": .., "file": .., "attempt": n} once the
    detached installer has been spawned; the download itself is synchronous,
    which is what lets the frontend poll for real percentages while it runs.

    Every refusal comes back with a language-neutral code as well as the text:
    the panel is bilingual and the server's sentences are Chinese only.

    resolve, when given, is called for the release info AFTER "checking" has
    been published. That ordering is the whole reason it exists: the caller can
    prefer its cached answer and only pay for a network lookup on a cold cache,
    and either way the browser is already showing a phase that can be displayed
    honestly instead of a progress bar with no bytes behind it.

    spawn is injected by tests so the staging logic can be exercised without a
    real /bin/sh. It receives the same (argv, cwd) the real spawner would.
    """
    with _LOCK:
        phase = _STATE.get("phase")
        # "checking" counts as in flight too: between the click and the first
        # byte there is a version lookup, and two of those must never start two
        # downloads. Derived from PHASES so a new phase cannot slip through.
        if phase in ACTIVE_PHASES:
            return _refuse("busy", "更新已在进行中", attempt=_STATE.get("attempt", 0))
        _STATE["attempt"] = int(_STATE.get("attempt") or 0) + 1
        attempt = _STATE["attempt"]
        # Published before the version lookup: this is what makes the retry's
        # first poll see "checking" instead of the previous run's failure.
        _STATE["phase"] = "checking"
        _STATE["code"] = ""
        _STATE["detail"] = ""
        _STATE["at"] = int(time.time())
        _STATE["done"] = 0
        _STATE["total"] = 0

    if resolve is not None:
        try:
            info = resolve() or {}
        except Exception as exc:
            detail = _fail(UpdateError("checkFailed", "无法获取版本信息：%s" % exc))
            _emit("update: release lookup failed: %r" % (exc,))
            return _refuse("checkFailed", detail, attempt=attempt)

    info = info or {}
    if not info.get("has_update"):
        detail = _fail(UpdateError("upToDate", "当前已是最新版本"))
        return _refuse("upToDate", detail, latest=info.get("latest") or "",
                       attempt=attempt)
    latest = str(info.get("latest") or "").strip().lstrip("vV")
    if not latest:
        detail = _fail(UpdateError("checkFailed", "版本信息里没有版本号"))
        return _refuse("checkFailed", detail, attempt=attempt)
    asset = pick_asset(info)
    if not asset:
        detail = _fail(UpdateError("noAsset", "该版本没有附带 .fpk 安装包"))
        return _refuse("noAsset", detail, attempt=attempt)

    updir = _update_dir(var_dir)
    if not updir:
        detail = _fail(UpdateError("noDir", "无法确定更新目录"))
        return _refuse("noDir", detail, attempt=attempt)
    try:
        os.makedirs(updir, exist_ok=True)
    except OSError as exc:
        detail = _fail(UpdateError("noDir", "无法创建更新目录：%s" % exc))
        return _refuse("noDir", detail, attempt=attempt)

    set_context(var_dir=var_dir, current=current)
    name = _safe_name(asset.get("name"))
    dest = os.path.join(updir, name)
    log_path = os.path.join(updir, "install.log")

    def on_progress(done, total):
        _set("downloading", latest, "正在下载 %s" % name, done=done, total=total)

    # Start every attempt with an empty installer log. The detached script only
    # appends, so leaving the previous run in place would show last time's
    # rollback lines while this download is still going.
    try:
        with open(log_path, "wb"):
            pass
    except OSError:
        pass

    if log is not None:
        global _LOG
        _LOG = log
    _set("downloading", latest, "正在下载 %s" % name, done=0, total=0)
    _emit("update: downloading %s (%s)" % (name, latest))
    try:
        download(asset.get("url") or "", dest, limit=limit, on_progress=on_progress,
                 timeout=timeout)
        _emit("update: downloaded %d bytes" % os.path.getsize(dest))
        # Verified before anything is uninstalled, and checked against the
        # version the release claimed so a stale asset cannot be installed.
        validate_fpk(dest, app_name=app_name, appcenter=appcenter,
                     expect_version=latest)
    except Exception as exc:
        detail = _fail(exc, latest)
        _emit("update: staging failed: %s" % (exc,))
        return _refuse(error_code(exc), detail, attempt=attempt)

    prev = ""
    try:
        source = prev_fpk or find_previous_fpk(current, app_name=app_name,
                                               exclude=[dest], var_dir=var_dir)
        if source:
            prev = os.path.join(updir, "rollback.fpk")
            if os.path.abspath(source) != os.path.abspath(prev):
                shutil.copyfile(source, prev)
            _emit("update: rollback package staged from %s" % source)
    except Exception as exc:
        # A missing rollback weakens the safety net but must not block the
        # update: the new package has already been verified.
        _emit("update: rollback staging skipped: %r" % (exc,))
        prev = ""
    if not prev:
        # The installer will skip its rollback branch. Saying so beats promising
        # an automatic rollback that cannot happen.
        _emit("update: no rollback package found for %s" % (current or "?"))

    script = os.path.join(updir, "install.sh")
    try:
        text = build_script(latest, port, dest, prev, log_path)
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(script, 0o755)
    except Exception as exc:
        detail = _fail(UpdateError("noScript", "无法写入安装脚本：%s" % exc))
        _emit("update: cannot write installer script: %r" % (exc,))
        return _refuse("noScript", detail, attempt=attempt)

    spawned = None
    try:
        runner = spawn or _spawn_detached
        spawned = runner(script, updir)
    except Exception as exc:
        detail = _fail(UpdateError("noScript", "无法启动安装脚本：%s" % exc))
        _emit("update: cannot spawn installer: %r" % (exc,))
        return _refuse("noScript", detail, attempt=attempt)

    _set("installing", latest, "安装包已就绪，正在重启服务")
    _emit("update: installer started for %s" % latest)
    return {"ok": True, "version": latest, "file": name, "script": script,
            "rollback": bool(prev), "attempt": attempt,
            "pid": getattr(spawned, "pid", None)}


def _spawn_detached(script, workdir):
    """Run the installer outside this process group.

    start_new_session is the whole point: the script's second step uninstalls
    the application, which stops the service this code runs in. As a plain
    child it would receive the same termination and die mid-upgrade.
    """
    return subprocess.Popen(
        ["/bin/sh", script],
        cwd=workdir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )