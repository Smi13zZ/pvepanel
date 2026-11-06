#!/usr/bin/env python3
"""update.py - does a newer build exist?

Three layers, tried in order, pure standard library:

  1. GitHub's REST API "releases/latest" - the richest answer (release notes
     plus the real asset sizes and download links).
  2. The releases Atom feed - not metered by the API quota. A home network
     shares one egress address and GitHub allows only 60 anonymous API calls
     per hour, so a 403 from step 1 is a normal detour rather than a failure.
  3. The "releases/expanded_assets/<tag>" HTML fragment - the only place the
     download links can still be read while the API refuses to talk.

Two invariants worth stating out loud:

  * A version is compared as **numbers**, never as text: 26.11.10 is newer
    than 26.11.9, and lexicographic comparison gets that backwards.
  * "Nothing published yet" is an answer, not an error. The repository may not
    exist yet, or may exist without a single release; GitHub reports both as
    HTTP 404, and both must leave this module quiet and returning a dict.
    A monitoring panel that throws inside its update check is worse than one
    that simply shows no update.

Caching keeps the hourly poll and every page load from spending the quota: a
successful answer is reused for SUCCESS_TTL seconds, a failed one for FAIL_TTL.
The success stamp is written only *after* the lookup came back - stamping on
entry would let a single offline minute swallow the next fifteen minutes of
retries.

Public interface:

    check(current, force=False, repo=DEFAULT_REPO) -> dict
    poll_loop(get_settings, log, stop_event, initial_delay=60, interval=3600)

check() never raises: every failure comes back as
{ok: False, has_update: False, reason: <code>, error: <text>}.
"""

import html as _html
import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.github.com"
WEB_BASE = "https://github.com"
DEFAULT_REPO = "Smi13zZ/pvepanel"

SUCCESS_TTL = 900          # 15 min: a page load every refresh must be free
FAIL_TTL = 60              # 1 min: a rate-limited API should not be hammered
HTTP_TIMEOUT = 8.0
NOTES_MAX = 6000
MAX_ASSETS = 100

# Set by the embedding service (pvepanel.py) so the User-Agent names the build
# that is asking. Empty is fine: the header then says so honestly.
CURRENT_VERSION = ""

# The caches hold the last answer and when it was produced. "at" is only ever
# written after a completed lookup, which is the whole point of the design.
_CACHE = {"at": 0.0, "payload": None}
_FAIL = {"at": 0.0, "payload": None}
_LOCK = threading.RLock()
# One network lookup at a time: a background poll and a "check now" click
# landing together would otherwise spend two API calls on the same question.
_FETCH = threading.Lock()


def _noop(_msg):
    pass


_LOG = _noop


def set_logger(fn):
    """Install the service's log() so fallbacks and failures land in its log."""
    global _LOG
    _LOG = fn if callable(fn) else _noop


def set_version(version):
    """Tell the module which build is running.

    check() already takes the version as an argument, but poll_loop() only gets
    a settings dict, and the settings carry no build number. Setting it here
    keeps the background poll honest without making the caller thread a value
    through the settings store.
    """
    global CURRENT_VERSION
    CURRENT_VERSION = str(version or "").strip()


def _now():
    """Single clock source so tests can move time without sleeping."""
    return time.time()


def cached():
    """The newest answer already in memory, or {} when there is none.

    Exists so a caller that must not block (the page bootstrap) can show the
    last known state instead of paying for a network round trip. Returns the
    same shape check() does, flagged cached=True; it never touches the
    network.
    """
    with _LOCK:
        if _CACHE["payload"]:
            return _cached_copy(_CACHE["payload"])
        if _FAIL["payload"]:
            return _cached_copy(_FAIL["payload"])
    return {}


def reset():
    """Drop both caches: for tests, and after a repo switch."""
    with _LOCK:
        _CACHE["at"] = 0.0
        _CACHE["payload"] = None
        _FAIL["at"] = 0.0
        _FAIL["payload"] = None


# ------------------------------------------------------------------ versions

_NUM_RE = re.compile(r"\d+(?:\.\d+)*")


def version_tuple(value):
    """'v26.11.10' -> (26, 11, 10, 0) so plain tuple comparison orders builds.

    Only the leading dotted run of digits counts. A suffix such as '-beta1'
    must not silently turn a pre-release into a newer build, and a missing
    segment must compare as zero rather than shifting the rest.
    """
    m = _NUM_RE.search(str(value or ""))
    if not m:
        return (0, 0, 0, 0)
    parts = [int(p) for p in m.group(0).split(".")][:4]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)


def is_newer(latest, current):
    """True only when both sides name a version and latest really sorts higher."""
    if not str(latest or "").strip() or not str(current or "").strip():
        return False
    return version_tuple(latest) > version_tuple(current)


# ------------------------------------------------------------------- errors

class CheckError(Exception):
    """One layer failed. reason is machine-readable, message is for humans."""

    def __init__(self, reason, message):
        Exception.__init__(self, message)
        self.reason = reason
        self.message = message


# Strongest first: when two layers disagree about why, the more actionable
# reason wins. "no_release" outranks "not_found" on purpose - GitHub answers
# 404 for releases/latest both when the repository is missing *and* when it
# exists but has published nothing, so a feed that came back 200 and empty is
# the better evidence: it proves the repository is there.
_REASON_ORDER = ("no_release", "not_found", "rate_limited", "http",
                 "timeout", "network", "parse")

# One readable sentence per failure code, so the settings page can say
# something useful instead of echoing a machine token.
_FAIL_HINT = {
    "not_found": "仓库或发布页还不存在，暂时无法检查更新",
    "no_release": "仓库已就绪，但还没有发布任何版本",
    "rate_limited": "GitHub 接口限流（匿名每小时 60 次），稍后再试",
    "http": "GitHub 接口返回异常状态",
    "timeout": "连接 GitHub 超时",
    "network": "网络不可达，无法连接 GitHub",
    "parse": "发布信息解析失败",
}


def _pick_reason(pairs):
    for reason in _REASON_ORDER:
        for candidate, _msg in pairs:
            if candidate == reason:
                return reason
    return pairs[0][0] if pairs else "error"


def _messages(pairs):
    seen = []
    for _candidate, msg in pairs:
        if msg and msg not in seen:
            seen.append(msg)
    return "；".join(seen)


def _describe(exc):
    if isinstance(exc, CheckError):
        return exc.reason, exc.message
    if isinstance(exc, urllib.error.HTTPError):
        code = int(getattr(exc, "code", 0) or 0)
        if code in (403, 429):
            return "rate_limited", "HTTP %d（GitHub 接口限流）" % code
        if code == 404:
            return "not_found", "HTTP 404（仓库或发布不存在）"
        return "http", "HTTP %d" % code
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout", "请求超时"
    return "error", str(exc) or exc.__class__.__name__


# --------------------------------------------------------------------- http

def _user_agent():
    version = str(CURRENT_VERSION or "").strip() or "unknown"
    return "fn-pvepanel/" + version


def _http_get(url, timeout, accept=None):
    """GET a URL as text, translating every failure into a CheckError.

    No layer above this one is allowed to see a bare urllib exception: the
    three-layer fallback is built on comparing reasons, not on sniffing types.
    """
    headers = {"User-Agent": _user_agent()}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        if url.lower().startswith("https"):
            resp = urllib.request.urlopen(
                req, timeout=timeout, context=ssl.create_default_context())
        else:
            resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        if code in (403, 429):
            raise CheckError("rate_limited", "HTTP %d（GitHub 接口限流）" % code)
        if code == 404:
            raise CheckError("not_found", "HTTP 404（仓库或发布不存在）")
        raise CheckError("http", "HTTP %d" % code)
    except (socket.timeout, TimeoutError):
        raise CheckError("timeout", "请求超时")
    except urllib.error.URLError as exc:
        detail = getattr(exc, "reason", None) or exc
        if isinstance(detail, (socket.timeout, TimeoutError)):
            raise CheckError("timeout", "请求超时")
        raise CheckError("network", "网络不可达：%s" % (detail,))
    except ssl.SSLError as exc:
        raise CheckError("network", "TLS 握手失败：%s" % (exc,))
    except (http.client.HTTPException, OSError) as exc:
        raise CheckError("network", "网络错误：%s" % (exc,))
    try:
        with resp:
            body = resp.read()
    except (socket.timeout, TimeoutError):
        raise CheckError("timeout", "读取响应超时")
    except (http.client.HTTPException, OSError) as exc:
        raise CheckError("network", "读取响应失败：%s" % (exc,))
    return body.decode("utf-8", "replace")


# ------------------------------------------------------------------ parsing

def _strip_html(raw):
    """Atom carries escaped HTML - flatten it into readable plain text."""
    text = _html.unescape(raw or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6]|tr|blockquote)>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "· ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _attr(tag, name):
    m = re.search(r'\b%s\s*=\s*"([^"]*)"' % re.escape(name), tag or "")
    return _html.unescape(m.group(1)) if m else ""


def _release_from_api(repo, api, timeout):
    raw = _http_get("%s/repos/%s/releases/latest" % (api, repo), timeout,
                    accept="application/vnd.github+json")
    try:
        rel = json.loads(raw)
    except ValueError:
        raise CheckError("parse", "接口返回的不是合法 JSON")
    if not isinstance(rel, dict):
        raise CheckError("parse", "接口返回的发布信息格式不对")
    tag = str(rel.get("tag_name") or rel.get("name") or "").strip()
    if not tag:
        raise CheckError("parse", "发布信息里没有版本号")
    assets = []
    for item in (rel.get("assets") or [])[:MAX_ASSETS]:
        if not isinstance(item, dict) or not item.get("browser_download_url"):
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        assets.append({
            "name": str(item.get("name") or ""),
            "size": size,
            "url": str(item.get("browser_download_url") or ""),
        })
    return {
        "tag": tag,
        "name": str(rel.get("name") or tag),
        "notes": str(rel.get("body") or ""),
        "published_at": str(rel.get("published_at") or ""),
        "html_url": str(rel.get("html_url") or ""),
        "assets": assets,
        "source": "api",
    }


def _release_from_atom(repo, web, timeout):
    xml = _http_get("%s/%s/releases.atom" % (web, repo), timeout)
    entry = re.search(r"<entry>(.*?)</entry>", xml, re.S)
    if not entry:
        # A 200 feed without a single entry means the repository is there but
        # has published nothing yet - distinct from a 404 on the whole repo.
        raise CheckError("no_release", "发布订阅源里没有条目")
    block = entry.group(1)
    title_raw = re.search(r"<title>(.*?)</title>", block, re.S)
    title = _html.unescape(title_raw.group(1)) if title_raw else ""
    link_tag = ""
    for m in re.finditer(r"<link\b[^>]*>", block):
        if _attr(m.group(0), "rel") == "alternate":
            link_tag = m.group(0)
            break
    url = _attr(link_tag, "href")
    updated = re.search(r"<updated>(.*?)</updated>", block, re.S)
    content = re.search(r"<content[^>]*>(.*?)</content>", block, re.S)
    tag = ""
    m = re.search(r"/tag/([^/?#]+)", url)
    if m:
        tag = urllib.parse.unquote(m.group(1))
    if not tag:
        m = re.search(r"(v?\d+(?:\.\d+)+)", title)
        tag = m.group(1) if m else ""
    if not tag:
        raise CheckError("parse", "无法从发布订阅源识别版本号")
    return {
        "tag": tag,
        "name": _strip_html(title) or tag,
        "notes": _strip_html(content.group(1)) if content else "",
        "published_at": updated.group(1) if updated else "",
        "html_url": url,
        "assets": [],
        "source": "atom",
    }


def _assets_from_html(repo, tag, web, timeout):
    """Read the download links out of the release page's asset fragment.

    That fragment is a plain HTML response: no API quota involved, which is
    exactly why it is the last resort when the API answers 403.
    """
    if not tag:
        return []
    page = _http_get("%s/%s/releases/expanded_assets/%s"
                     % (web, repo, urllib.parse.quote(tag, safe="")), timeout)
    out = []
    seen = set()
    # The visible name sits in a nested <span>, so take it from the URL itself.
    for href in re.findall(r'href="(/[^"]+/releases/download/[^"]+)"', page):
        href = _html.unescape(href)
        if href in seen:
            continue
        seen.add(href)
        name = urllib.parse.unquote(href.rsplit("/", 1)[-1])
        out.append({"name": name, "size": 0, "url": web + href})
        if len(out) >= MAX_ASSETS:
            break
    return out


# --------------------------------------------------------------------- check

def _blank(current, repo):
    cur = str(current or "").strip()
    if cur[:1] in ("v", "V"):
        cur = cur[1:]
    return {
        "ok": False,
        "current": cur,
        "latest": "",
        "has_update": False,
        "name": "",
        "notes": "",
        "published_at": "",
        "url": "",
        "html_url": "",
        "repo": repo,
        "assets": [],
        "source": "",
        # reason is the machine-readable code (not_found, rate_limited, ...);
        # detail is the sentence a human should read. The UI shows detail.
        "reason": "",
        "detail": "",
        "error": "",
        "cached": False,
        "checked": False,
        "checked_at": 0,
    }


def _freeze(payload):
    """An independent copy of a result dict, assets included.

    The cache hands the same answer to every caller. Without the nested copy a
    handler that sorted or trimmed info["assets"] in place would rewrite what
    the next request sees - and since a miss returns the freshly built dict,
    the caller and the cache would be sharing one list object.
    """
    out = dict(payload)
    out["assets"] = [dict(a) for a in (payload.get("assets") or [])]
    return out


def _cached_copy(payload):
    out = _freeze(payload)
    out["cached"] = True
    return out


def _resolve_base(explicit, env_name, fallback):
    value = str(explicit or os.environ.get(env_name) or fallback).strip()
    return value.rstrip("/") or fallback


def check(current="", force=False, repo=DEFAULT_REPO, *,
          api_base="", web_base="", timeout=HTTP_TIMEOUT, log=None):
    """Ask GitHub for the newest release and compare it with current.

    force=True skips both caches (the user pressed "check now"). api_base and
    web_base exist so the whole three-layer path can be exercised against a
    local fake server without touching the internet; the environment variables
    PVEPANEL_UPDATE_API_BASE and PVEPANEL_UPDATE_WEB_BASE do the same from
    outside the process.

    Always returns a dict. Never raises.
    """
    repo = str(repo or DEFAULT_REPO).strip().strip("/") or DEFAULT_REPO
    api = _resolve_base(api_base, "PVEPANEL_UPDATE_API_BASE", API_BASE)
    web = _resolve_base(web_base, "PVEPANEL_UPDATE_WEB_BASE", WEB_BASE)
    emit = log if callable(log) else _LOG

    def cached_hit():
        if force:
            return None
        stamp = _now()
        with _LOCK:
            if _CACHE["payload"] and stamp - _CACHE["at"] < SUCCESS_TTL:
                return _cached_copy(_CACHE["payload"])
            if _FAIL["payload"] and stamp - _FAIL["at"] < FAIL_TTL:
                return _cached_copy(_FAIL["payload"])
        return None

    hit = cached_hit()
    if hit is not None:
        return hit
    with _FETCH:
        # Another thread may have finished the same lookup while we queued.
        hit = cached_hit()
        if hit is not None:
            return hit
        return _lookup(current, repo, api, web, timeout, emit)


def _lookup(current, repo, api, web, timeout, emit):
    out = _blank(current, repo)
    rel = None
    errors = []
    try:
        rel = _release_from_api(repo, api, timeout)
    except Exception as exc:
        reason, message = _describe(exc)
        errors.append((reason, message))
        emit("版本检查：接口查询失败（%s），改走发布订阅源" % message)
    if rel is None:
        try:
            rel = _release_from_atom(repo, web, timeout)
        except Exception as exc:
            reason, message = _describe(exc)
            errors.append((reason, message))
            out["reason"] = _pick_reason(errors)
            out["error"] = _messages(errors)
            out["detail"] = _FAIL_HINT.get(out["reason"], out["error"])
            stamp = _now()
            out["checked"] = True
            out["checked_at"] = int(stamp)
            with _LOCK:
                _FAIL["at"] = stamp
                _FAIL["payload"] = _freeze(out)
            return out

    tag = str(rel.get("tag") or "").strip()
    latest = tag[1:] if tag[:1] in ("v", "V") else tag
    out["ok"] = True
    out["latest"] = latest or tag
    out["name"] = rel.get("name") or tag
    out["notes"] = str(rel.get("notes") or "")[:NOTES_MAX]
    out["published_at"] = rel.get("published_at") or ""
    out["html_url"] = rel.get("html_url") or ""
    out["url"] = out["html_url"]
    out["source"] = rel.get("source") or ""
    assets = rel.get("assets") or []
    if not assets:
        try:
            assets = _assets_from_html(repo, tag or ("v" + (latest or "")),
                                       web, timeout)
        except Exception as exc:
            # The update itself is already known; a missing download link is
            # not a reason to hide it.
            emit("版本检查：下载列表不可用（%s）" % _describe(exc)[1])
    out["assets"] = assets or []
    out["has_update"] = is_newer(out["latest"], out["current"])
    if not out["current"]:
        out["reason"] = "unknown_current"
        out["error"] = "当前版本未知，无法判断是否有更新"
        out["detail"] = out["error"]
    stamp = _now()
    out["checked"] = True
    out["checked_at"] = int(stamp)
    with _LOCK:
        # Stamped only now, with the lookup done: a failure must not extend
        # the life of the previous success, and vice versa.
        _CACHE["at"] = stamp
        _CACHE["payload"] = _freeze(out)
        _FAIL["at"] = 0.0
        _FAIL["payload"] = None
    return out


# ----------------------------------------------------------------- poll loop

def _current_from(cfg):
    """Which version are we running?

    Three sources, most specific first: an explicit key in the settings dict,
    the value passed to set_version(), then PVEPANEL_VERSION - which is the
    same environment variable the service itself reads its VERSION from, so
    the ordinary fpk process gets a correct answer with no wiring at all.
    """
    for key in ("current", "version"):
        value = str(cfg.get(key) or "").strip()
        if value:
            return value
    value = str(CURRENT_VERSION or "").strip()
    if value:
        return value
    value = str(os.environ.get("PVEPANEL_VERSION") or "").strip()
    # "0.0.0" is the service's own placeholder for "not deployed"; treating it
    # as a real version would report every release as an update.
    return "" if value == "0.0.0" else value


def poll_loop(get_settings, log, stop_event, initial_delay=60, interval=3600,
              *, notify=None):
    """Check for a new release from a background thread, forever.

    The panel must learn about a release even when nobody has the page open,
    which is why this exists at all. The first lookup waits initial_delay
    seconds so the HTTP service gets a chance to come up first, then it repeats
    every interval seconds.

    get_settings() is re-read on every round, so toggling update_check or
    switching update_repo takes effect without a restart. It may also carry
    update_api_base / update_web_base to point the check elsewhere. notify(),
    when given, is called with the result dict only when has_update becomes
    true for a release it has not announced yet, so a long-lived process does
    not repeat itself every hour.
    """
    emit = log if callable(log) else _noop
    if stop_event.wait(max(0.0, float(initial_delay))):
        return
    announced = ""
    while not stop_event.is_set():
        try:
            cfg = get_settings()
            if not isinstance(cfg, dict):
                cfg = {}
            if cfg.get("update_check", True):
                current = _current_from(cfg)
                info = check(current, force=False,
                             repo=cfg.get("update_repo") or DEFAULT_REPO,
                             api_base=cfg.get("update_api_base") or "",
                             web_base=cfg.get("update_web_base") or "",
                             log=emit)
                if not info.get("ok"):
                    emit("版本检查失败（%s）：%s"
                         % (info.get("reason") or "error", info.get("error") or ""))
                elif info.get("has_update"):
                    latest = info.get("latest") or ""
                    if latest != announced:
                        announced = latest
                        emit("发现新版本 %s（当前 %s），来源 %s"
                             % (latest, current or "未知", info.get("source") or "?"))
                        if callable(notify):
                            try:
                                notify(info)
                            except Exception as exc:
                                emit("新版本通知回调失败：%r" % (exc,))
                elif info.get("latest") and info.get("latest") != announced:
                    # One line per distinct remote version keeps the log
                    # honest without an hourly heartbeat for years.
                    announced = info.get("latest")
                    emit("版本检查：当前 %s 已是最新（远端 %s）"
                         % (current or "未知", info.get("latest")))
        except Exception as exc:
            # Offline, rate limited, DNS down, disk full - the thread must
            # survive all of it. This is the only place that may swallow.
            try:
                emit("版本检查线程异常：%r" % (exc,))
            except Exception:
                pass
        if stop_event.wait(max(0.05, float(interval))):
            break