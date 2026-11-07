#!/usr/bin/env python3
"""notifier.py - turns collector alerts into pushed messages.

Two rules keep this from becoming a second monitoring engine:

1. **It watches the same alert list the UI shows.** If a condition is not worth
   a row in the panel it is not worth a push, and the two can never disagree.
2. **It never repeats itself.** Each event carries a key; once fired, the key is
   suppressed for `COOLDOWN` seconds (and re-armed when the condition clears),
   so a disk that stays at 91% does not page every poll.

The alert text is already localised-ish in the collector (Chinese, with the
numbers embedded). Pushes are sent in the panel's current language when the
event has a translation, otherwise the collector's text is used verbatim - that
keeps this module independent of the i18n table on the server side.
"""

import json
import os
import threading
import time
import uuid

#: Delivery records: one entry per push attempt, newest first.
#:
#: The point is answering "did it actually go out, and what did it say" without
#: reading the service log - a webhook that silently 404s is invisible otherwise.
#: The body is kept (that is the whole value of the record) but channel secrets
#: never are: they live in config.json / servers.json only, and this file is
#: written 0600 next to them. Ring buffered, atomic replace, corrupt file = empty.
NOTIFY_LOG_NAME = "notify-log.json"
NOTIFY_LOG_MAX = 300
NOTIFY_LOG_BODY = 600
NOTIFY_LOG_LOCK = threading.Lock()


def notify_log_path(var_dir):
    return os.path.join(var_dir, NOTIFY_LOG_NAME) if var_dir else ""


def load_notify_log(var_dir):
    """Every stored record, newest first. A missing or damaged file is empty."""
    path = notify_log_path(var_dir)
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    recs = data.get("records") if isinstance(data, dict) else data
    if not isinstance(recs, list):
        return []
    return [r for r in recs if isinstance(r, dict)]


def _save_notify_log(var_dir, records):
    """Atomic write: a crash mid-save must not destroy the whole history."""
    path = notify_log_path(var_dir)
    if not path:
        return False
    try:
        os.makedirs(var_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"records": records}, f, ensure_ascii=False, indent=1)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def clear_notify_log(var_dir):
    with NOTIFY_LOG_LOCK:
        return _save_notify_log(var_dir, [])


def record_delivery(var_dir, channel, title, body, event, ok, detail=""):
    """Append one attempt. Called for successes and failures alike."""
    ch = channel if isinstance(channel, dict) else {}
    rec = {
        "id": uuid.uuid4().hex[:12],
        "at": int(time.time()),
        "event": str(event or "manual"),
        "channel": str(ch.get("name") or ch.get("type") or "")[:80],
        "channel_id": str(ch.get("id") or "")[:40],
        "type": str(ch.get("type") or "")[:40],
        "ok": bool(ok),
        "detail": str(detail or "")[:300],
        "title": str(title or "")[:200],
        "body": str(body or "")[:NOTIFY_LOG_BODY],
    }
    with NOTIFY_LOG_LOCK:
        recs = load_notify_log(var_dir)
        recs.insert(0, rec)
        del recs[NOTIFY_LOG_MAX:]
        _save_notify_log(var_dir, recs)
    return rec


# key -> (cooldown seconds, re-arm delay)
COOLDOWN = {
    "node_offline": 600,
    "storage_full": 6 * 3600,
    "disk_temp": 3600,
    "disk_health": 6 * 3600,
    "backup_failed": 3 * 3600,
    "backup_missing": 12 * 3600,
    "load_high": 1800,
    "mem_high": 1800,
    "sub_due": 24 * 3600,
    "auth_failed": 3600,
}

# How the collector's alert titles map onto the event keys the settings page
# offers. A title that is not here is a UI-only alert and is never pushed.
TITLE_TO_EVENT = {
    "节点离线": "node_offline",
    "存储水位": "storage_full",
    "磁盘温度过高": "disk_temp",
    "磁盘温度偏高": "disk_temp",
    "磁盘 SMART 异常": "disk_health",
    "备份任务失败": "backup_failed",
    "虚拟机无备份": "backup_missing",
    "负载偏高": "load_high",
    "内存紧张": "mem_high",
    "订阅即将到期": "sub_due",
    "订阅已过期": "sub_due",
}

# One push carries both languages instead of picking one: the panel's UI
# language is a per-browser preference, while the phone it is pushed to belongs
# to a person who may read either. Sending "中文 / English" costs one extra line
# and removes a whole class of "I set the panel to English but the alerts are
# still Chinese" confusion.
HEADLINE = {
    "node_offline": ("节点离线", "Node offline"),
    "storage_full": ("存储将满", "Storage filling up"),
    "disk_temp": ("磁盘温度高", "Disk running hot"),
    "disk_health": ("磁盘健康异常", "Disk health problem"),
    "backup_failed": ("备份失败", "Backup failed"),
    "backup_missing": ("虚拟机无备份", "Guest without backup"),
    "load_high": ("负载偏高", "High load"),
    "mem_high": ("内存紧张", "Memory pressure"),
    "sub_due": ("订阅到期", "Subscription due"),
    "auth_failed": ("Token 失效", "Token invalid"),
}


def bilingual(zh, en):
    """'中文 / English' - the title form every push uses."""
    return "%s / %s" % (zh, en)


def bilingual_body(zh_lines, en_lines):
    """Chinese lines, a blank line, then the English lines.

    The blank line matters: without it a body that already wraps reads as one
    run-on paragraph and the reader cannot tell where one language stops.
    """
    zh = [ln for ln in zh_lines if ln is not None and ln != ""]
    en = [ln for ln in en_lines if ln is not None and ln != ""]
    return "\n".join(zh) + ("\n\n" + "\n".join(en) if en else "")


class Notifier:
    def __init__(self, store, notify_mod, log, var_dir=None):
        self.store = store
        self.notify = notify_mod
        self.log = log
        # Where notify-log.json lives. Falling back to the store's var_dir keeps
        # existing call sites working without threading a new argument through.
        self.var_dir = str(var_dir or getattr(store, "var_dir", "") or "")
        self._last = {}
        self._armed = {}
        self._lock = threading.Lock()
        self._seen_servers = {}

    def _enabled(self, event):
        cfg = self.store.settings()
        if not cfg.get("notify_enabled"):
            return False
        events = cfg.get("notify_events") or {}
        return bool(events.get(event))

    def _channels(self):
        cfg = self.store.settings()
        out = []
        for ch in cfg.get("notify_channels") or []:
            if ch.get("enabled") is False:
                continue
            if ch.get("type") == "webhook" and not ch.get("url"):
                continue
            out.append(ch)
        return out

    def _allowed(self, key):
        now = time.time()
        with self._lock:
            last = self._last.get(key, 0)
            if now - last < COOLDOWN.get(key, 3600):
                return False
            self._last[key] = now
            return True

    def push(self, event, title, body):
        """Send one message to every configured channel.

        Every attempt is written to the delivery log whether it succeeded or
        not, so the settings page can answer "did it go out" without anyone
        tailing the service log.
        """
        if self.notify is None:
            return 0
        channels = self._channels()
        if not channels:
            return 0
        sent = 0
        for ch in channels:
            ok = False
            detail = ""
            try:
                ok, detail = self.notify.send(ch, title, body)
            except Exception as e:
                # A channel that raises is a failure like any other; the record
                # must still say something went wrong for this destination.
                detail = "exception: %s" % (e,)
                self.log("notify %s failed: %s" % (ch.get("type"), e))
            if ok:
                sent += 1
            else:
                self.log("notify %s rejected: %s" % (ch.get("type"), detail))
            try:
                record_delivery(self.var_dir, ch, title, body, event, ok, detail)
            except Exception as e:  # noqa: BLE001 - logging must never break a push
                self.log("delivery record failed: %r" % (e,))
        self.log("notified [%s] to %d/%d channels" % (event, sent, len(channels)))
        return sent

    def observe(self, snap, server_name=""):
        """Feed one snapshot in; push whatever crossed a threshold.

        Called from the same refresh path the UI uses, so there is no extra
        polling traffic and the alerts a user sees are exactly the ones that
        were pushed.
        """
        if not self.store.settings().get("notify_enabled"):
            return
        prefix = "[PVEpanel]"
        label = server_name or (snap.get("server") or {}).get("name") or ""
        stamp = time.strftime("%Y-%m-%d %H:%M")

        if snap.get("meta", {}).get("auth_failed") and self._enabled("auth_failed"):
            if self._allowed("auth_failed:%s" % label):
                title = "%s %s" % (prefix,
                                   bilingual(*HEADLINE["auth_failed"]))
                body = bilingual_body(
                    ["%s Token 无效或已过期，面板已停止轮询。" % label,
                     "时间：%s" % stamp],
                    ["%s: the token is invalid or expired; polling has stopped." % label,
                     "Time: %s" % stamp])
                self.push("auth_failed", title, body)

        for alert in snap.get("alerts") or []:
            key = TITLE_TO_EVENT.get(alert.get("title") or "")
            if not key or not self._enabled(key):
                continue
            dedupe = "%s:%s:%s" % (key, label, alert.get("detail") or "")
            if not self._allowed(dedupe):
                continue
            head = HEADLINE.get(key)
            title = "%s %s" % (prefix,
                               bilingual(*head) if head else (alert.get("title") or key))
            # The alert detail comes from the collector in Chinese; it is the
            # same sentence in both halves so a reader of either language still
            # gets the numbers, and nobody sees a body with no content at all.
            detail = alert.get("detail") or ""
            body = bilingual_body(
                ["%s%s" % (label + " · " if label else "", stamp), detail],
                ["%s%s" % (label + " · " if label else "", stamp), detail])
            self.push(key, title, body)
