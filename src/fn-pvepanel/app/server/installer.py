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
import subprocess
import threading
import time
import urllib.error
import urllib.request

APP_NAME = "fn-pvepanel"
# The state machine the API exposes. "failed" is terminal until the next start.
PHASES = ("idle", "downloading", "installing", "failed")

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
PREV_SEARCH_DIRS = (
    "/vol1/@appshare/%s",
    "/vol1/@appshare",
    "/tmp",
)

_LOCK = threading.RLock()
_STATE = {
    "phase": "idle",
    "version": "",
    "detail": "",
    "done": 0,
    "total": 0,
    "at": 0,
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
            "phase": "idle", "version": "", "detail": "",
            "done": 0, "total": 0, "at": 0,
        })


def _set(phase, version="", detail="", done=None, total=None, log=None):
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
        _STATE["detail"] = detail
        _STATE["at"] = int(time.time())
        if done is not None:
            _STATE["done"] = int(done)
        if total is not None:
            _STATE["total"] = int(total)


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
    """
    with _LOCK:
        out = dict(_STATE)
        out["current"] = _CTX.get("current") or ""
        out["percent"] = _percent(out.get("done"), out.get("total"))
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
        raise ValueError("下载地址为空")
    folder = os.path.dirname(os.path.abspath(dest))
    if folder:
        os.makedirs(folder, exist_ok=True)
    part = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
    total = 0
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise ValueError("下载失败：HTTP %d" % int(getattr(exc, "code", 0) or 0))
    except Exception as exc:
        raise ValueError("下载失败：%s" % (exc,))
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
                        raise ValueError("安装包超过 %d MB，已中止"
                                         % (limit // 1024 // 1024))
                    f.write(chunk)
                    if on_progress:
                        on_progress(total, expect)
                f.flush()
                os.fsync(f.fileno())
    except ValueError:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise
    except http.client.IncompleteRead as exc:
        # The server promised Content-Length and hung up early. Caught
        # explicitly because a half-downloaded package is the one failure that
        # would otherwise look like a successful download.
        try:
            os.unlink(part)
        except OSError:
            pass
        raise ValueError("下载不完整：只收到 %d 字节（连接中断）" % total)
    except Exception as exc:
        # Never leave a partial file behind for the next step to trip over.
        try:
            os.unlink(part)
        except OSError:
            pass
        raise ValueError("下载中断：%s" % (exc,))
    if total <= 0:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise ValueError("下载内容为空")
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
        raise ValueError("下载不完整：收到 %d 字节，应为 %d 字节" % (total, expect))
    os.replace(part, dest)
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

    Raises ValueError with a sentence the UI can show. Called before the script
    is even written, because the uninstall inside that script is irreversible
    from the panel's point of view.

    appcenter: an explicit path to appcenter-cli; None means search the usual
    places. Tests pass a stand-in so the check is exercised off-device.
    """
    import tarfile

    if not path or not os.path.isfile(path):
        raise ValueError("安装包不存在")
    if os.path.getsize(path) == 0:
        raise ValueError("安装包是空文件")
    try:
        with tarfile.open(path, "r:gz") as tf:
            names = tf.getnames()
            # fnpack always writes both; a release asset missing either is
            # either a different kind of archive or a broken one.
            if "manifest" not in names:
                raise ValueError("安装包缺少 manifest")
            if "app.tgz" not in names:
                raise ValueError("安装包缺少 app.tgz")
            raw = tf.extractfile("manifest").read().decode("utf-8", "replace")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("安装包无法解压：%s" % (exc,))

    fields = _manifest_fields(raw)
    found = fields.get("appname") or ""
    if found != app_name:
        raise ValueError("安装包应用名不匹配：%s" % (found or "?"))

    if expect_version is not None:
        want = str(expect_version or "").strip().lstrip("vV")
        have = (fields.get("version") or "").strip().lstrip("vV")
        if want and have and have != want:
            raise ValueError("安装包版本不匹配：期望 %s，实际 %s" % (want, have))
        if not have:
            raise ValueError("安装包 manifest 里没有版本号")

    cli = _find_appcenter(appcenter or "")
    if not cli:
        raise ValueError("找不到 appcenter-cli，无法自动安装")


# ----------------------------------------------------------------- rollback

def find_previous_fpk(current, app_name=APP_NAME, extra_dirs=(), exclude=()):
    """Locate the package matching the running version, for the rollback step.

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
    dirs = list(extra_dirs)
    for pattern in PREV_SEARCH_DIRS:
        dirs.append(pattern % app_name if "%s" in pattern else pattern)
    seen = set()
    for folder in dirs:
        if not folder or folder in seen:
            continue
        seen.add(folder)
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            continue
        for fn in entries:
            if not fn.startswith(prefix) or not fn.lower().endswith(".fpk"):
                continue
            full = os.path.join(folder, fn)
            try:
                if not os.path.isfile(full) or os.path.abspath(full) in skip:
                    continue
            except OSError:
                continue
            return full
    return ""


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

printf '%s\n' "wizard_backend_port=$PORT" > "$ENVF" || {
  echo "=== cannot write the wizard env file, aborting ==="
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

def start(info, current, port, var_dir, log=None, *, prev_fpk="",
          appcenter="", app_name=APP_NAME, spawn=None, timeout=HTTP_TIMEOUT,
          limit=DOWNLOAD_LIMIT):
    """Download, verify and stage the update, then hand it to the script.

    Returns {"ok": True, "version": .., "file": ..} once the detached installer
    has been spawned; the download itself is synchronous, which is what lets
    the frontend poll for real percentages while it runs.

    spawn is injected by tests so the staging logic can be exercised without a
    real /bin/sh. It receives the same (argv, cwd) the real spawner would.
    """
    with _LOCK:
        phase = _STATE.get("phase")
    if phase in ("downloading", "installing"):
        return {"ok": False, "error": "更新已在进行中"}

    info = info or {}
    if not info.get("has_update"):
        return {"ok": False, "error": "当前已是最新版本",
                "latest": info.get("latest") or ""}
    latest = str(info.get("latest") or "").strip().lstrip("vV")
    if not latest:
        return {"ok": False, "error": "版本信息里没有版本号"}
    asset = pick_asset(info)
    if not asset:
        return {"ok": False, "error": "该版本没有附带 .fpk 安装包"}

    updir = _update_dir(var_dir)
    if not updir:
        return {"ok": False, "error": "无法确定更新目录"}
    try:
        os.makedirs(updir, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": "无法创建更新目录：%s" % exc}

    set_context(var_dir=var_dir, current=current)
    name = _safe_name(asset.get("name"))
    dest = os.path.join(updir, name)
    log_path = os.path.join(updir, "install.log")

    def on_progress(done, total):
        _set("downloading", latest, "正在下载 %s" % name, done=done, total=total)

    _set("downloading", latest, "正在下载 %s" % name, done=0, total=0, log=log)
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
        _set("failed", latest, str(exc))
        _emit("update: staging failed: %s" % (exc,))
        return {"ok": False, "error": str(exc)}

    prev = ""
    try:
        source = prev_fpk or find_previous_fpk(current, app_name=app_name,
                                               exclude=[dest])
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

    script = os.path.join(updir, "install.sh")
    try:
        text = build_script(latest, port, dest, prev, log_path)
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(script, 0o755)
    except Exception as exc:
        _set("failed", latest, "无法写入安装脚本：%s" % exc)
        _emit("update: cannot write installer script: %r" % (exc,))
        return {"ok": False, "error": "无法写入安装脚本：%s" % exc}

    spawned = None
    try:
        runner = spawn or _spawn_detached
        spawned = runner(script, updir)
    except Exception as exc:
        _set("failed", latest, "无法启动安装脚本：%s" % exc)
        _emit("update: cannot spawn installer: %r" % (exc,))
        return {"ok": False, "error": "无法启动安装脚本：%s" % exc}

    _set("installing", latest, "安装包已就绪，正在重启服务")
    _emit("update: installer started for %s" % latest)
    return {"ok": True, "version": latest, "file": name, "script": script,
            "rollback": bool(prev), "pid": getattr(spawned, "pid", None)}


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
