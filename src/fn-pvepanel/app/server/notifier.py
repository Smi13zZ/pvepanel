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

import threading
import time

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


class Notifier:
    def __init__(self, store, notify_mod, log):
        self.store = store
        self.notify = notify_mod
        self.log = log
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
        """Send one message to every configured channel."""
        if self.notify is None:
            return 0
        channels = self._channels()
        if not channels:
            return 0
        sent = 0
        for ch in channels:
            try:
                ok, detail = self.notify.send(ch, title, body)
            except Exception as e:
                self.log("notify %s failed: %s" % (ch.get("type"), e))
                continue
            if ok:
                sent += 1
            else:
                self.log("notify %s rejected: %s" % (ch.get("type"), detail))
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
        lang = self.store.settings().get("lang") or "zh"
        idx = 0 if lang != "en" else 1
        prefix = "[PVEpanel]"
        label = server_name or (snap.get("server") or {}).get("name") or ""

        if snap.get("meta", {}).get("auth_failed") and self._enabled("auth_failed"):
            if self._allowed("auth_failed:%s" % label):
                title = "%s %s" % (prefix, HEADLINE["auth_failed"][idx])
                body = (label + " Token 无效或已过期，面板已停止轮询。"
                        if idx == 0 else
                        label + ": the token is invalid or expired; polling has stopped.")
                self.push("auth_failed", title, body)

        for alert in snap.get("alerts") or []:
            key = TITLE_TO_EVENT.get(alert.get("title") or "")
            if not key or not self._enabled(key):
                continue
            dedupe = "%s:%s:%s" % (key, label, alert.get("detail") or "")
            if not self._allowed(dedupe):
                continue
            head = HEADLINE.get(key, (alert.get("title"), alert.get("title")))[idx]
            title = "%s %s" % (prefix, head)
            body = "%s%s\n%s" % (label + " · " if label else "",
                                  time.strftime("%Y-%m-%d %H:%M"),
                                  alert.get("detail") or "")
            self.push(key, title, body)
