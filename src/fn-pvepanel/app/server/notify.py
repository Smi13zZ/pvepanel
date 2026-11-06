#!/usr/bin/env python3
"""notify.py - outbound notification channels for PVEpanel.

Every channel is a small adapter that turns a (title, body) pair into exactly
one HTTP request. Only the standard library is used, matching the rest of the
app: fnOS ships no third-party Python packages and the fpk must stay
dependency-free.

Secret handling is the part that has already caused a production incident, so
the rules are stated once here and enforced in one place:

  * A value listed in a channel type's "secrets" tuple never leaves the box.
    The browser only ever sees the literal SECRET_MARK ("__SET__").
  * A save that submits SECRET_MARK, an empty string or nothing at all means
    "keep what is on disk". merge_config() is the only function allowed to
    decide that, and it refuses to write the mark itself to disk for ANY field,
    not just the declared secrets.
  * channel_public()/mask() are the only functions that turn a stored config
    into something safe to hand to a browser.

Success/failure is never decided by the HTTP status alone. WeCom and DingTalk
answer 200 with an "errcode" in the body; Telegram answers 200 with
"ok": false. Judging on status turned silent failures into "delivered".

The module-level endpoint constants are the vendor URLs. Tests point them at a
local sink server to inspect the real request each channel builds; nothing else
reads them.
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

SECRET_MARK = "__SET__"

# Vendor endpoints. Overridable at runtime so a local sink can verify the exact
# request shape (path, query, body, headers) without any network access.
BARK_DEFAULT_SERVER = "https://api.day.app"
SERVERCHAN_API = "https://sctapi.ftqq.com"
PUSHPLUS_API = "https://www.pushplus.plus/send"
WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"
TELEGRAM_API = "https://api.telegram.org"
WECOM_API = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"

USER_AGENT = "PVEpanel"

# type -> display name / secret fields / field order / per-field labels+hints.
#
# "fields" fixes the form order and is also the whitelist a saved channel is
# reduced to; "secrets" marks the fields masked on the way out and kept-if-blank
# on the way in. labels/hints carry both languages so the panel can re-render
# the form when the user flips languages without another round trip.
CHANNEL_TYPES = {
    "bark": {
        "label_zh": "Bark",
        "label_en": "Bark",
        "secrets": ("key",),
        "fields": ("server", "key"),
        "labels": {
            "server": {"zh": "服务器地址", "en": "Server URL"},
            "key": {"zh": "设备 Key", "en": "Device key"},
        },
        "hints": {
            "server": {"zh": "留空则使用官方地址 https://api.day.app",
                       "en": "Leave empty to use https://api.day.app"},
            "key": {"zh": "Bark App 里显示的设备密钥",
                    "en": "The device key shown in the Bark app"},
        },
    },
    "serverchan": {
        "label_zh": "Server 酱",
        "label_en": "ServerChan",
        "secrets": ("sendkey",),
        "fields": ("sendkey",),
        "labels": {
            "sendkey": {"zh": "SendKey", "en": "SendKey"},
        },
        "hints": {
            "sendkey": {"zh": "Server 酱后台的 SendKey，通常以 SCT 开头",
                        "en": "The SendKey from your ServerChan account, usually starting with SCT"},
        },
    },
    "pushplus": {
        "label_zh": "PushPlus",
        "label_en": "PushPlus",
        "secrets": ("token",),
        "fields": ("token",),
        "labels": {
            "token": {"zh": "Token", "en": "Token"},
        },
        "hints": {
            "token": {"zh": "PushPlus 账号页给出的推送 token",
                      "en": "The push token shown on your PushPlus account page"},
        },
    },
    "wxpusher": {
        "label_zh": "WxPusher",
        "label_en": "WxPusher",
        "secrets": ("app_token",),
        "fields": ("app_token", "uids", "topics"),
        "labels": {
            "app_token": {"zh": "AppToken", "en": "AppToken"},
            "uids": {"zh": "UID 列表", "en": "UID list"},
            "topics": {"zh": "主题 ID 列表", "en": "Topic ID list"},
        },
        "hints": {
            "app_token": {"zh": "WxPusher 应用的 AppToken",
                          "en": "AppToken of your WxPusher application"},
            "uids": {"zh": "可留空；多个 UID 用英文逗号分隔",
                     "en": "Optional; separate multiple UIDs with commas"},
            "topics": {"zh": "可留空；多个主题 ID 用英文逗号分隔",
                       "en": "Optional; separate multiple topic IDs with commas"},
        },
    },
    "telegram": {
        "label_zh": "Telegram",
        "label_en": "Telegram",
        "secrets": ("bot_token",),
        "fields": ("bot_token", "chat_id", "api_base"),
        "labels": {
            "bot_token": {"zh": "Bot Token", "en": "Bot token"},
            "chat_id": {"zh": "Chat ID", "en": "Chat ID"},
            "api_base": {"zh": "API 地址", "en": "API base"},
        },
        "hints": {
            "bot_token": {"zh": "向 @BotFather 申请机器人时拿到的 token",
                          "en": "The token @BotFather gives you for the bot"},
            "chat_id": {"zh": "接收消息的会话 ID；私聊填用户 ID，群组填负数 ID",
                        "en": "Target chat ID; a user ID for private chats, a negative ID for groups"},
            "api_base": {"zh": "留空使用 https://api.telegram.org，走自建中转时填这里",
                         "en": "Empty means https://api.telegram.org; set it when using a relay"},
        },
    },
    "wecom": {
        "label_zh": "企业微信机器人",
        "label_en": "WeCom bot",
        "secrets": ("key",),
        "fields": ("key",),
        "labels": {
            "key": {"zh": "机器人 Key", "en": "Bot key"},
        },
        "hints": {
            "key": {"zh": "群机器人 Webhook 地址里 key= 后面那一段",
                    "en": "The part after key= in the group bot webhook URL"},
        },
    },
    "dingtalk": {
        "label_zh": "钉钉机器人",
        "label_en": "DingTalk bot",
        "secrets": ("url", "secret"),
        "fields": ("url", "secret"),
        "labels": {
            "url": {"zh": "Webhook 地址", "en": "Webhook URL"},
            "secret": {"zh": "加签密钥", "en": "Signing secret"},
        },
        "hints": {
            "url": {"zh": "群机器人的 Webhook 完整地址",
                    "en": "Full webhook URL of the group bot"},
            "secret": {"zh": "机器人开启「加签」时必填，未开启可留空",
                       "en": "Required when the bot has signing enabled, otherwise leave empty"},
        },
    },
    "slack": {
        "label_zh": "Slack",
        "label_en": "Slack",
        "secrets": ("url",),
        "fields": ("url",),
        "labels": {
            "url": {"zh": "Webhook 地址", "en": "Webhook URL"},
        },
        "hints": {
            "url": {"zh": "Slack App 的 Incoming Webhook 地址",
                    "en": "Incoming webhook URL of your Slack app"},
        },
    },
    "webhook": {
        "label_zh": "自定义 Webhook",
        "label_en": "Custom webhook",
        "secrets": ("headers",),
        "fields": ("url", "headers"),
        "labels": {
            "url": {"zh": "Webhook 地址", "en": "Webhook URL"},
            "headers": {"zh": "自定义请求头", "en": "Custom headers"},
        },
        "hints": {
            "url": {"zh": "接收 POST 请求的完整地址",
                    "en": "Full address that receives the POST request"},
            "headers": {"zh": "每行一个，格式为「名称: 值」",
                        "en": "One per line, formatted as name: value"},
        },
    },
}

# 事件清单与 store.DEFAULTS["notify_events"] 的键一一对应：前端据此渲染开关，
# 后端拿它拼告警标题。两边共用一份，加事件时只改这里。
EVENTS = {
    "node_offline":     {"zh": "节点离线",      "en": "Node offline",         "level": "err"},
    "storage_full":     {"zh": "存储水位告警",  "en": "Storage usage",        "level": "warn"},
    "disk_temp":        {"zh": "磁盘温度告警",  "en": "Disk temperature",     "level": "warn"},
    "disk_health":      {"zh": "磁盘健康告警",  "en": "Disk health",          "level": "err"},
    "backup_failed":    {"zh": "备份任务失败",  "en": "Backup failed",        "level": "err"},
    "backup_missing":   {"zh": "虚拟机无备份",  "en": "Guest without backup", "level": "warn"},
    "guest_stopped":    {"zh": "虚拟机停机",    "en": "Guest stopped",        "level": "info"},
    "load_high":        {"zh": "负载过高",      "en": "High load",            "level": "warn"},
    "mem_high":         {"zh": "内存吃紧",      "en": "High memory",          "level": "warn"},
    "sub_due":          {"zh": "订阅到期",      "en": "Subscription due",     "level": "warn"},
    "auth_failed":      {"zh": "认证失败",      "en": "Authentication failed", "level": "err"},
    "update_available": {"zh": "有新版本",      "en": "Update available",     "level": "info"},
}

TITLE_PREFIX = "[PVEpanel]"

# Budget for one message, counted in UTF-8 BYTES - not characters. The
# smallest real limit among these services is WeCom's 2048 bytes, and a Chinese
# character costs three of them, so a 1500-character alert is 4500 bytes and
# gets rejected with a bare "invalid message content". Stay well under 2048.
MAX_TEXT_BYTES = 1800
MAX_TITLE_BYTES = 240

# Keys under which a settings blob carries the channel list. store.py calls it
# notify_channels; a channel-focused patch calls it channels. Both are handled by
# the same merge so a secret can never slip through whichever name is in use.
CHANNEL_LIST_KEYS = ("channels", "notify_channels")


# --------------------------------------------------------------------------
# bilingual helpers
# --------------------------------------------------------------------------
def _is_en(lang):
    return str(lang or "zh").strip().lower().startswith("en")


def _pick(entry, lang):
    """Take one language out of a {zh, en} entry; anything else passes through."""
    if isinstance(entry, dict):
        key = "en" if _is_en(lang) else "zh"
        return entry.get(key) or entry.get("zh") or entry.get("en") or ""
    return entry if entry is not None else ""


def label_for(ctype, field, lang="zh"):
    """Display name of one field of one channel, falling back to the key name."""
    meta = CHANNEL_TYPES.get(ctype) or {}
    return _pick((meta.get("labels") or {}).get(field), lang) or str(field)


def hint_for(ctype, field, lang="zh"):
    """Hint line under one field; empty when the type documents none."""
    meta = CHANNEL_TYPES.get(ctype) or {}
    return _pick((meta.get("hints") or {}).get(field), lang)


def type_meta(lang="zh"):
    """Form metadata for every channel, flattened for the current language.

    The panel renders its channel forms from this and nothing else, so adding a
    channel stays a one-file change. Both translations are included so the
    browser can switch language without another request.
    """
    out = {}
    for key, meta in CHANNEL_TYPES.items():
        fields = list(meta["fields"])
        out[key] = {
            "label_zh": meta["label_zh"],
            "label_en": meta["label_en"],
            "label": meta["label_zh"] if not _is_en(lang) else meta["label_en"],
            "fields": fields,
            "secrets": list(meta["secrets"]),
            "labels": {f: label_for(key, f, lang) for f in fields},
            "hints": {f: hint_for(key, f, lang) for f in fields},
            "labels_i18n": {f: dict((meta.get("labels") or {}).get(f) or {}) for f in fields},
            "hints_i18n": {f: dict((meta.get("hints") or {}).get(f) or {}) for f in fields},
        }
    return out


def type_list(lang="zh"):
    """Flat [{'type', 'label'}] list, for a dropdown."""
    return [{"type": key,
             "label": meta["label_zh"] if not _is_en(lang) else meta["label_en"]}
            for key, meta in CHANNEL_TYPES.items()]


def event_list(lang="zh"):
    """Flat [{'key', 'label', 'level'}] list for the event switches."""
    return [{"key": key,
             "label": _pick(meta, lang),
             "level": meta.get("level") or "info"}
            for key, meta in EVENTS.items()]


def event_label(event, lang="zh"):
    """Human name of one alert event; unknown keys are echoed back."""
    meta = EVENTS.get(str(event or "").strip())
    if not meta:
        return str(event or "")
    return _pick(meta, lang)


# --------------------------------------------------------------------------
# secret-preserving merge and the browser view
# --------------------------------------------------------------------------
def _as_dict(val):
    return val if isinstance(val, dict) else {}


def _keep_secret(old, new, present):
    """A secret is only replaced by a real value the user actually typed in."""
    if not present:
        return old if old else ""
    if new is None:
        return old if old else ""
    text = str(new).strip()
    if not text or text == SECRET_MARK:
        return old if old else ""
    return text


def _keep_plain(old, new, present):
    """A plain field: empty clears it, and the mark is never stored."""
    if not present:
        return old if old is not None else ""
    if new is None:
        return old if old is not None else ""
    text = str(new).strip()
    if text == SECRET_MARK:
        # 标记跑到非密钥字段里，说明调用方回传了整份打码配置。保留旧值总比把
        # 字面量写进磁盘强 —— 这条兜底挡掉过一次真实事故。
        return old if old is not None else ""
    return text


def _merge_channel(old, new, meta):
    """Merge one channel's config dict against the type's declared fields."""
    old = _as_dict(old)
    new = _as_dict(new)
    fields = list(meta.get("fields") or ())
    secrets = tuple(meta.get("secrets") or ())
    out = {}
    for key in fields:
        present = key in new
        val = new.get(key) if present else None
        if key in secrets:
            out[key] = _keep_secret(old.get(key), val, present)
        else:
            out[key] = _keep_plain(old.get(key), val, present)
    for key, val in new.items():
        if key not in fields:
            out[key] = _keep_plain(old.get(key), val, True)
    for key, val in old.items():
        if key not in out:
            out[key] = val
    return out


def _merge_list(old, new):
    """Lists merge by id; an explicit empty list clears the list."""
    old_list = old if isinstance(old, list) else []
    if not isinstance(new, list):
        return [dict(x) if isinstance(x, dict) else x for x in old_list]
    if not new:
        return []
    if not all(isinstance(x, dict) and x.get("id") for x in new):
        # 没有 id 的元素无法定位，整表替换比猜一个更安全
        return [_merge_dict({}, x) if isinstance(x, dict) else x for x in new]
    out = [dict(x) if isinstance(x, dict) else x for x in old_list]
    index = {}
    for i, item in enumerate(out):
        if isinstance(item, dict) and item.get("id"):
            index[str(item["id"])] = i
    for item in new:
        pos = index.get(str(item.get("id")))
        if pos is None:
            out.append(_merge_dict({}, item))
            index[str(item.get("id"))] = len(out) - 1
        else:
            out[pos] = _merge_dict(_as_dict(out[pos]), item)
    return out


def _merge_any(old, new):
    if isinstance(new, dict):
        return _merge_dict(_as_dict(old), new)
    if isinstance(new, list):
        return _merge_list(old, new)
    return new


def _merge_dict(old, new):
    """Recursive merge; a channel node switches its config branch to secret mode."""
    old = _as_dict(old)
    new = _as_dict(new)
    ctype = str(new.get("type") or old.get("type") or "").strip()
    meta = CHANNEL_TYPES.get(ctype)
    out = dict(old)
    for key, val in new.items():
        if key == "config" and meta is not None:
            out[key] = _merge_channel(old.get(key), val, meta)
        elif key in CHANNEL_LIST_KEYS and isinstance(val, list):
            out[key] = _merge_list(old.get(key), val)
        else:
            out[key] = _merge_any(old.get(key), val)
    return out


def merge_config(old, new):
    """Merge a notification patch onto the stored config.

    Returns a brand new dict; neither argument is mutated. Semantics:

      * keys missing from new keep their stored value;
      * secret fields keep the stored value when new carries SECRET_MARK, an
        empty string, or nothing at all - only a real new value replaces it;
      * SECRET_MARK is never written back to any field;
      * channels merges by id: unseen ids are appended, known ids are merged
        field by field, and ids nobody mentions are kept as they are;
      * channels: [] is an explicit clear.
    """
    return _merge_dict(_as_dict(old), _as_dict(new))


def channel_public(ch):
    """One channel in the shape the browser may see: secrets replaced by the mark."""
    ch = _as_dict(ch)
    ctype = str(ch.get("type") or "").strip()
    meta = CHANNEL_TYPES.get(ctype) or {"secrets": (), "fields": ()}
    cfg = _as_dict(ch.get("config"))
    out = {}
    for key in meta["fields"]:
        val = cfg.get(key)
        if key in meta["secrets"]:
            out[key] = SECRET_MARK if val else ""
        else:
            out[key] = val if val is not None else ""
    return {
        "id": str(ch.get("id") or ""),
        "name": str(ch.get("name") or ""),
        "type": ctype,
        "enabled": bool(ch.get("enabled", True)),
        "config": out,
        "fields": list(meta["fields"]),
        "secrets": list(meta["secrets"]),
    }


def mask(cfg):
    """Browser-safe copy of a whole config or of a single channel.

    Handles both shapes because the settings page saves the whole section while
    the channel editor only deals with one entry; one place deciding what is
    secret is what keeps the two from drifting apart.
    """
    if not isinstance(cfg, dict):
        return {}
    if str(cfg.get("type") or "").strip() in CHANNEL_TYPES:
        return channel_public(cfg)
    if any(isinstance(cfg.get(k), list) for k in CHANNEL_LIST_KEYS):
        out = dict(cfg)
        for key in CHANNEL_LIST_KEYS:
            if isinstance(cfg.get(key), list):
                out[key] = [channel_public(c) for c in cfg[key] if isinstance(c, dict)]
        return out
    if "config" in cfg or "type" in cfg:
        # 类型未知也要按渠道处理：它的字段表是空的，配置整体丢弃，绝不原样透传。
        # 透传等于把不认识渠道里的密钥直接交给浏览器。
        return channel_public(cfg)
    return dict(cfg)


# --------------------------------------------------------------------------
# message shaping (PVE flavoured, plain text - no markdown tables)
# --------------------------------------------------------------------------
def format_alert(event, node="", fields=None, ts=None, lang="zh"):
    """Build (title, body) for one alert.

    fields is an ordered mapping - or a list of pairs - of already formatted
    rows; the caller decides which numbers matter. Bodies stay plain multi-line
    text because most of these channels render markdown poorly or not at all.
    """
    label = event_label(event, lang)
    title = "%s %s" % (TITLE_PREFIX, label or ("Notification" if _is_en(lang) else "通知"))
    rows = []
    if node:
        rows.append(("Node" if _is_en(lang) else "节点", str(node)))
    if isinstance(fields, dict):
        rows.extend((str(k), str(v)) for k, v in fields.items())
    elif isinstance(fields, (list, tuple)):
        for item in fields:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                rows.append((str(item[0]), str(item[1])))
            elif isinstance(item, dict):
                rows.append((str(item.get("label") or item.get("name") or ""),
                             str(item.get("value") or item.get("detail") or "")))
    body = ["%s: %s" % (k, v) for k, v in rows if k]
    body.append(("Time: " if _is_en(lang) else "时间：") + str(ts or time.strftime("%Y-%m-%d %H:%M:%S")))
    return title, "\n".join(body)


def format_test(name="", lang="zh"):
    """The canned message a test delivery carries."""
    name = str(name or "").strip() or ("this" if _is_en(lang) else "这个")
    title = "%s %s" % (TITLE_PREFIX, "Notification test" if _is_en(lang) else "通知测试")
    if _is_en(lang):
        body = "\n".join([
            "If you can read this, the '%s' channel is configured correctly." % name,
            "Node offline, storage, disk temperature and backup events arrive here.",
            "Time: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        ])
    else:
        body = "\n".join([
            "收到这条消息说明「%s」渠道配置正确。" % name,
            "节点离线、存储告警、磁盘温度、备份失败等事件会推送到这里。",
            "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        ])
    return title, body


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------
def _is_loopback(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in ("localhost", "::1") or host.startswith("127.")


def _opener(url):
    # A webhook pointing at this very box must not be dragged through a system
    # proxy: on Windows the registry-based bypass does not always cover 127.0.0.1.
    if _is_loopback(url):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _post(url, data, headers, timeout):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with _opener(url).open(req, timeout=timeout) as resp:
        return resp.status, resp.read(8192).decode("utf-8", "replace")


def _post_json(url, payload, headers=None, timeout=8.0):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT}
    h.update(headers or {})
    return _post(url, data, h, timeout)


def _post_form(url, fields, timeout=8.0):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    return _post(url, data, {
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "User-Agent": USER_AGENT,
    }, timeout)


def _parse_headers(raw):
    """Parse the 'Name: value' lines of the custom-webhook headers field."""
    out = {}
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        if name.strip():
            out[name.strip()] = value.strip()
    return out


def _judge(status, text, code_field=None, expect=None):
    """Turn one response into (ok, detail).

    code_field names the JSON key carrying the real verdict for channels that
    answer 200 even when they failed (WeCom/DingTalk errcode, Telegram ok, the
    code family on the Chinese push services).
    """
    text = text or ""
    if status < 200 or status >= 300:
        return False, "HTTP %d %s" % (status, text[:200])
    if not code_field:
        return True, text[:120]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        # 服务端没给 JSON 就无法判定，按送达处理，把原文留给用户看
        return True, text[:120]
    if not isinstance(data, dict):
        return True, text[:120]
    code = data.get(code_field)
    if code is None:
        return True, text[:120]
    try:
        ok = int(code) == int(expect)
    except (TypeError, ValueError):
        ok = bool(code) if isinstance(expect, bool) else (str(code) == str(expect))
    if ok:
        return True, ""
    detail = (data.get("errmsg") or data.get("message") or data.get("msg")
              or data.get("description") or text[:160])
    return False, "服务返回 %s：%s" % (code, detail)


def _clip(text, max_bytes):
    """Cut a message to a UTF-8 byte budget, saying so instead of failing.

    WeCom rejects content over 2048 bytes; a "backup missing" alert that lists
    every guest can reach that. Truncating keeps the alert delivered instead of
    the channel silently dropping it. Counted in bytes because that is what the
    services actually limit - and CJK text is three bytes per character.
    """
    text = str(text or "")
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    suffix = "…(过长已截断)"
    keep = max(0, max_bytes - len(suffix.encode("utf-8")))
    # 按字节截断可能切在多字节字符中间，decode(errors="ignore") 丢掉残半个字
    return raw[:keep].decode("utf-8", "ignore") + suffix


def _compose(title, body):
    title = str(title or "").strip()
    body = str(body or "").strip()
    if title and body:
        return _clip("%s\n%s" % (title, body), MAX_TEXT_BYTES)
    return _clip(title or body, MAX_TEXT_BYTES)


def _dingtalk_url(url, secret):
    """Append timestamp+sign when the DingTalk bot has signing enabled.

    The algorithm is fixed by DingTalk: HMAC-SHA256 keyed with the secret over
    the millisecond timestamp, a newline, then the secret; base64 the digest and
    percent-encode it. Skipping this - or signing the wrong string - gets a 400
    back with no useful explanation, so the sign is verified in the tests.
    """
    secret = str(secret or "").strip()
    if not secret:
        return url
    stamp = str(int(time.time() * 1000))
    to_sign = "%s\n%s" % (stamp, secret)
    digest = hmac.new(secret.encode("utf-8"), to_sign.encode("utf-8"),
                      digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    joiner = "&" if "?" in url else "?"
    return "%s%stimestamp=%s&sign=%s" % (url, joiner, stamp, sign)


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------
def send(channel, title, body, timeout=8.0):
    """Deliver one message through one channel.

    Returns (ok, detail). Never raises: a broken channel reports a reason and
    the caller moves on to the next one.
    """
    ch = _as_dict(channel)
    ctype = str(ch.get("type") or "").strip()
    cfg = _as_dict(ch.get("config"))
    title = str(title or "")
    body = str(body or "")
    try:
        timeout = float(timeout or 8.0)
    except (TypeError, ValueError):
        timeout = 8.0
    try:
        if ctype == "bark":
            server = str(cfg.get("server") or "").strip() or BARK_DEFAULT_SERVER
            key = str(cfg.get("key") or "").strip()
            if not key:
                return False, "缺少设备 Key"
            url = "%s/%s" % (server.rstrip("/"), urllib.parse.quote(key, safe=""))
            status, text = _post_json(url, {
                "title": _clip(title, MAX_TITLE_BYTES),
                "body": _clip(body, MAX_TEXT_BYTES),
                "group": "PVEpanel",
                "icon": "",
            }, timeout=timeout)
            return _judge(status, text, "code", 200)

        if ctype == "serverchan":
            key = str(cfg.get("sendkey") or "").strip()
            if not key:
                return False, "缺少 SendKey"
            url = "%s/%s.send" % (SERVERCHAN_API.rstrip("/"), urllib.parse.quote(key, safe=""))
            status, text = _post_form(url, {"title": _clip(title, MAX_TITLE_BYTES),
                                             "desp": _clip(body, MAX_TEXT_BYTES)}, timeout=timeout)
            return _judge(status, text, "code", 0)

        if ctype == "pushplus":
            token = str(cfg.get("token") or "").strip()
            if not token:
                return False, "缺少 Token"
            status, text = _post_json(PUSHPLUS_API, {
                "token": token,
                "title": _clip(title, MAX_TITLE_BYTES),
                "content": _clip(body, MAX_TEXT_BYTES),
                # 正文是纯文本行，用 txt 渲染最稳：markdown 会把 "节点: x" 当成列表
                "template": "txt",
            }, timeout=timeout)
            return _judge(status, text, "code", 200)

        if ctype == "wxpusher":
            app_token = str(cfg.get("app_token") or "").strip()
            if not app_token:
                return False, "缺少 AppToken"
            uids = [x.strip() for x in str(cfg.get("uids") or "").split(",") if x.strip()]
            topics = []
            for item in str(cfg.get("topics") or "").split(","):
                item = item.strip()
                if item.isdigit():
                    topics.append(int(item))
            payload = {
                "appToken": app_token,
                "content": _compose(title, body),
                "summary": _clip(title or body, 99),
                "contentType": 1,
            }
            if uids:
                payload["uids"] = uids
            if topics:
                payload["topicIds"] = topics
            status, text = _post_json(WXPUSHER_API, payload, timeout=timeout)
            return _judge(status, text, "code", 1000)

        if ctype == "telegram":
            token = str(cfg.get("bot_token") or "").strip()
            chat_id = str(cfg.get("chat_id") or "").strip()
            if not token:
                return False, "缺少 Bot Token"
            if not chat_id:
                return False, "缺少 Chat ID"
            base = str(cfg.get("api_base") or "").strip() or TELEGRAM_API
            url = "%s/bot%s/sendMessage" % (base.rstrip("/"), token)
            status, text = _post_json(url, {
                "chat_id": chat_id,
                "text": _compose(title, body),
                "disable_web_page_preview": True,
            }, timeout=timeout)
            # HTTP 200 只说明请求送达，真正的结果在 ok 字段里
            return _judge(status, text, "ok", True)

        if ctype == "wecom":
            key = str(cfg.get("key") or "").strip()
            if not key:
                return False, "缺少机器人 Key"
            url = "%s?key=%s" % (WECOM_API, urllib.parse.quote(key, safe=""))
            status, text = _post_json(url, {
                "msgtype": "text",
                "text": {"content": _compose(title, body)},
            }, timeout=timeout)
            return _judge(status, text, "errcode", 0)

        if ctype == "dingtalk":
            url = str(cfg.get("url") or "").strip()
            if not url.startswith("http"):
                return False, "Webhook 地址无效"
            url = _dingtalk_url(url, cfg.get("secret") or "")
            status, text = _post_json(url, {
                "msgtype": "text",
                "text": {"content": _compose(title, body)},
            }, timeout=timeout)
            return _judge(status, text, "errcode", 0)

        if ctype == "slack":
            url = str(cfg.get("url") or "").strip()
            if not url.startswith("http"):
                return False, "Webhook 地址无效"
            # Slack 的 mrkdwn 里 *xx* 是加粗；没有标题时只发正文
            text_body = ("*%s*\n%s" % (title, body)).strip() if title else body
            status, resp = _post_json(url, {"text": _clip(text_body, MAX_TEXT_BYTES)}, timeout=timeout)
            return _judge(status, resp)

        if ctype == "webhook":
            url = str(cfg.get("url") or "").strip()
            if not url.startswith("http"):
                return False, "Webhook 地址无效"
            status, text = _post_json(url, {
                "title": _clip(title, MAX_TITLE_BYTES),
                "content": _clip(body, MAX_TEXT_BYTES),
                "source": "PVEpanel",
            }, _parse_headers(cfg.get("headers") or ""), timeout=timeout)
            return _judge(status, text)

        return False, "未知渠道类型：%s" % (ctype or "(空)")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        return False, "HTTP %d %s" % (exc.code, detail)
    except urllib.error.URLError as exc:
        return False, str(getattr(exc, "reason", exc))[:300]
    except Exception as exc:  # pragma: no cover - defensive
        return False, str(exc)[:300]


def test_channel(channel, timeout=8.0):
    """Send the canned test message. Returns the same (ok, detail) as send()."""
    ch = _as_dict(channel)
    name = str(ch.get("name") or "").strip() or str(ch.get("type") or "").strip()
    title, body = format_test(name)
    return send(ch, title, body, timeout=timeout)


def send_all(channels, title, body, timeout=8.0):
    """Push one message to every enabled channel.

    Returns a list of {'id', 'name', 'type', 'ok', 'detail'} records. One failing
    channel never stops the rest and nothing raises.
    """
    out = []
    for ch in (channels or []):
        ch = _as_dict(ch)
        if not ch or not ch.get("enabled", True):
            continue
        ok, detail = send(ch, title, body, timeout=timeout)
        out.append({
            "id": str(ch.get("id") or ""),
            "name": str(ch.get("name") or ""),
            "type": str(ch.get("type") or ""),
            "ok": bool(ok),
            "detail": detail,
        })
    return out