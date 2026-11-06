#!/usr/bin/env python3
"""maint.py - configuration export/import, automatic backups, and retention.

A backup is a plain JSON document, nothing more: the point is that it stays
inspectable with an editor and portable across versions. Three things are worth
knowing before changing anything here.

  1. WHERE BACKUPS GO. Only <var_dir>/backups. The reference implementation
     prefers the app share (/vol1/@appshare/<app>) so users can grab backups
     over SMB, but a share is user visible and this panel's backups carry the
     PVE API tokens - that combination would quietly publish cluster
     credentials to every account that can read the share. This panel also
     declares no shares (config/resource -> data-share.shares == []), so there
     is nothing to write to anyway. var_dir is 770 root:root on fnOS and each
     file is chmod 600 on top of that.

  2. WHAT A BACKUP CONTAINS. Tokens, by default, because a backup that cannot
     restore a working panel is not a backup - it is a reminder to retype every
     token. So the document records "contains_secrets": true and the UI can warn
     before it is downloaded. For exports that leave the box, pass
     include_secrets=False: the secret bearing leaves are blanked, the shape is
     kept, and restoring such a file just requires re-entering them.

  3. RETENTION ONLY EVER DELETES AUTOMATIC BACKUPS. The reference version said
     the same in a comment while its code removed everything old, which is the
     kind of mismatch that loses the file a user was counting on. Here the kind
     is read back out of the document, and unreadable files are left alone.

Pure stdlib. Writes go to a temp file and are renamed into place, so a crash
mid-write cannot leave a truncated backup that fails to parse.
"""

import json
import os
import re
import time

APP_NAME = "fn-pvepanel"

BACKUP_PREFIX = "pvepanel-backup-"
BACKUP_EXT = ".json"
BACKUP_FORMAT = 1

RETENTION_MIN_DAYS = 1
RETENTION_MAX_DAYS = 3650
INTERVAL_MIN_HOURS = 1
INTERVAL_MAX_HOURS = 8760

DEFAULT_RETENTION_DAYS = 30
DEFAULT_INTERVAL_HOURS = 24

# Leaf key names whose values never belong in a redacted export. Compared
# case-insensitively; "key" catches the bark/devkey style channel secrets.
SECRET_KEY_NAMES = frozenset(("token", "pve_token", "secret", "password", "key"))

_NAME_RE = re.compile(
    r"^" + re.escape(BACKUP_PREFIX) + r"(\d{8}-\d{6})(?:-\d+)?"
    + re.escape(BACKUP_EXT) + r"$"
)


# ---------------------------------------------------------------- paths

def backup_dir(var_dir):
    """The single directory backups live in; created on demand.

    Deliberately not the app share - see the module docstring. The path is
    returned even when creation fails, so the caller gets a useful error out of
    the write instead of a confusing empty string.
    """
    d = os.path.join(var_dir, "backups")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _state_path(var_dir):
    return os.path.join(var_dir, "backup-state.json")


def _stamp(ts=None):
    when = time.time() if ts is None else ts
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(when))


def backup_filename(stamp=None):
    """Canonical file name for a backup taken at 'stamp' (epoch or YYYYMMDD-HHMMSS)."""
    if isinstance(stamp, str):
        return "%s%s%s" % (BACKUP_PREFIX, stamp, BACKUP_EXT)
    return "%s%s%s" % (BACKUP_PREFIX, _stamp(stamp), BACKUP_EXT)


def _created_at_from_name(name):
    m = _NAME_RE.match(name)
    if not m:
        return 0
    try:
        return int(time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S")))
    except (ValueError, OverflowError):
        return 0


# ---------------------------------------------------------------- building

def _snapshot(get_state):
    """Take a detached copy of the caller's live state.

    The json round trip is the point: it severs the backup from dicts the
    server keeps mutating while we serialise, so a backup is one consistent
    moment rather than whatever the store looked like as we walked it.
    """
    state = get_state() or {}
    if not isinstance(state, dict):
        raise TypeError("get_state() must return a dict")
    cfg = state.get("config")
    servers = state.get("servers")
    payload = {
        "config": cfg if isinstance(cfg, dict) else {},
        "servers": servers if isinstance(servers, list) else [],
    }
    return json.loads(json.dumps(payload, ensure_ascii=False))


def _redact(node):
    """Blank every secret bearing leaf in place, keeping the shape intact.

    Structure is preserved on purpose: a restore must still see the servers and
    channels, just with empty credentials. Keys are never removed, so the result
    is still a valid backup document.
    """
    if isinstance(node, dict):
        for key in list(node.keys()):
            value = node[key]
            if isinstance(value, str) and str(key).lower() in SECRET_KEY_NAMES:
                node[key] = ""
            else:
                _redact(value)
    elif isinstance(node, list):
        for item in node:
            _redact(item)
    return node


def make_backup(get_state, version, kind="manual", include_secrets=True):
    """Build the backup document. Does not write it."""
    payload = _snapshot(get_state)
    contains_secrets = True
    if not include_secrets:
        _redact(payload)
        contains_secrets = False
    return {
        "app": APP_NAME,
        "format": BACKUP_FORMAT,
        "version": str(version or ""),
        "kind": kind if kind in ("manual", "auto") else "manual",
        "created_at": int(time.time()),
        "contains_secrets": contains_secrets,
        "payload": payload,
    }


# ---------------------------------------------------------------- writing

def _unique_path(folder, base_name):
    """Avoid clobbering a backup written in the same second.

    Two saves inside one second would otherwise collide on the file name and
    silently replace the earlier backup; a counter suffix keeps both.
    """
    path = os.path.join(folder, base_name)
    if not os.path.exists(path):
        return path
    stem = base_name[:-len(BACKUP_EXT)] if base_name.endswith(BACKUP_EXT) else base_name
    n = 1
    while True:
        cand = os.path.join(folder, "%s-%d%s" % (stem, n, BACKUP_EXT))
        if not os.path.exists(cand):
            return cand
        n += 1


def write_backup(var_dir, data, folder=None):
    """Serialise a backup document to disk; returns the path it landed on."""
    folder = folder or backup_dir(var_dir)
    os.makedirs(folder, exist_ok=True)
    created = data.get("created_at")
    if not isinstance(created, (int, float)) or created <= 0:
        created = int(time.time())
    path = _unique_path(folder, backup_filename(created))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------- listing

def list_backups(var_dir):
    """Every backup in the folder, newest first.

    created_at comes from the file name (the authoritative creation time) and
    falls back to mtime only for names that do not match the pattern - which in
    practice means a file somebody renamed by hand.
    """
    folder = backup_dir(var_dir)
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        if not name.startswith(BACKUP_PREFIX) or not name.endswith(BACKUP_EXT):
            continue
        path = os.path.join(folder, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not os.path.isfile(path):
            continue
        created = _created_at_from_name(name) or int(st.st_mtime)
        out.append({
            "name": name,
            "size": int(st.st_size),
            "created_at": int(created),
            "mtime": int(st.st_mtime),
            "path": path,
        })
    out.sort(key=lambda b: (b["created_at"], b["name"]), reverse=True)
    return out


def _read_kind(path):
    """Kind recorded inside a backup document, or None if it cannot be read."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    kind = doc.get("kind")
    return kind if kind in ("manual", "auto") else None


def prune_backups(var_dir, retention_days):
    """Drop automatic backups older than retention_days; return the count removed.

    Manual exports are never pruned, and a file whose kind cannot be read is
    left in place: an unparseable backup is more likely damaged than disposable,
    and it stays visible in the list for the user to decide about.
    """
    try:
        days = int(retention_days or 0) or DEFAULT_RETENTION_DAYS
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    days = max(RETENTION_MIN_DAYS, min(RETENTION_MAX_DAYS, days))
    cutoff = time.time() - days * 86400.0
    removed = 0
    for item in list_backups(var_dir):
        if item["mtime"] >= cutoff:
            continue
        if _read_kind(item["path"]) != "auto":
            continue
        try:
            os.remove(item["path"])
            removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------- parsing

def parse_backup(raw):
    """Validate raw bytes as a backup document for THIS app.

    Raises ValueError carrying a short machine readable reason so the HTTP layer
    can map it to a message without leaking internals:
      "unreadable" - not utf-8, not JSON, or not an object
      "foreign"    - a backup belonging to another app
      "format"     - a format this build does not know (including newer ones)
      "empty"      - no usable payload (no server list to restore)
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("unreadable")
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except Exception:
        raise ValueError("unreadable")
    if not isinstance(data, dict):
        raise ValueError("unreadable")
    if data.get("app") != APP_NAME:
        raise ValueError("foreign")
    fmt = data.get("format")
    if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt < 1:
        raise ValueError("format")
    if fmt > BACKUP_FORMAT:
        raise ValueError("format")
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("empty")
    if not isinstance(payload.get("servers"), list):
        raise ValueError("empty")
    cfg = payload.get("config")
    if cfg is not None and not isinstance(cfg, dict):
        raise ValueError("empty")
    return data


# ---------------------------------------------------------------- state file

def load_state(var_dir):
    try:
        with open(_state_path(var_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(var_dir, state):
    path = _state_path(var_dir)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- settings

def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def auto_settings(settings):
    """Normalise the auto backup slice of the settings dict, clamping ranges."""
    st = settings if isinstance(settings, dict) else {}
    interval = _int(st.get("auto_backup_interval_hours"), DEFAULT_INTERVAL_HOURS)
    retention = _int(st.get("auto_backup_retention_days"), DEFAULT_RETENTION_DAYS)
    return {
        "enabled": bool(st.get("auto_backup_enabled")),
        "interval_hours": max(INTERVAL_MIN_HOURS, min(INTERVAL_MAX_HOURS, interval)),
        "retention_days": max(RETENTION_MIN_DAYS, min(RETENTION_MAX_DAYS, retention)),
    }


def status(var_dir, settings):
    """Everything the maintenance page needs to render the backup card."""
    cfg = auto_settings(settings)
    state = load_state(var_dir)
    items = list_backups(var_dir)
    last = _int(state.get("last_success"), 0)
    if last <= 0:
        # survives a var_dir whose backup-state.json was lost; the settings
        # mirror is written by auto_loop for exactly this case
        last = _int((settings or {}).get("backup_last_success"), 0)
    next_run = (last + cfg["interval_hours"] * 3600) if (cfg["enabled"] and last > 0) else 0
    last_error = str(state.get("last_error") or (settings or {}).get("backup_last_error") or "")
    return {
        "enabled": cfg["enabled"],
        "interval_hours": cfg["interval_hours"],
        "retention_days": cfg["retention_days"],
        "interval_min": INTERVAL_MIN_HOURS,
        "interval_max": INTERVAL_MAX_HOURS,
        "retention_min": RETENTION_MIN_DAYS,
        "retention_max": RETENTION_MAX_DAYS,
        "prefix": BACKUP_PREFIX,
        "format": BACKUP_FORMAT,
        "directory": backup_dir(var_dir),
        "count": len(items),
        "last_success": last,
        "last_attempt": _int(state.get("last_attempt"), 0),
        "last_error": last_error,
        "next_run": next_run,
        "backups": [
            {"name": b["name"], "size": b["size"], "created_at": b["created_at"]}
            for b in items
        ],
    }


# ---------------------------------------------------------------- running

def run_auto(var_dir, settings, get_state, log, force=False, version=""):
    """Take an automatic backup if one is due. Never raises.

    Returns {"ok": True, "skipped": <reason>} when nothing was written, or
    {"ok": True, "path": ..., "removed": n} when it was. A failure comes back as
    {"ok": False, "error": ...} AND is recorded in the state file, so a panel
    that has failed every night says so on the maintenance page instead of only
    in a log nobody opens.
    """
    log = log or (lambda *_a, **_k: None)
    cfg = auto_settings(settings)
    if not cfg["enabled"] and not force:
        return {"ok": True, "skipped": "disabled"}

    state = load_state(var_dir)
    now = int(time.time())
    if not force:
        last = _int(state.get("last_success"), 0)
        if last <= 0:
            last = _int((settings or {}).get("backup_last_success"), 0)
        if last > 0 and now < last + cfg["interval_hours"] * 3600:
            return {"ok": True, "skipped": "not_due"}

    try:
        data = make_backup(get_state, version, kind="auto")
        path = write_backup(var_dir, data)
        removed = prune_backups(var_dir, cfg["retention_days"])
    except Exception as exc:  # noqa: BLE001 - a timer thread must never die
        state.update({"last_attempt": now, "last_error": repr(exc)})
        save_state(var_dir, state)
        log("auto backup failed: %r" % (exc,))
        return {"ok": False, "error": repr(exc)}

    state.update({
        "last_success": now,
        "last_attempt": now,
        "last_path": path,
        "last_error": "",
        "removed": removed,
    })
    save_state(var_dir, state)
    log("auto backup -> %s (pruned %d)" % (path, removed))
    return {"ok": True, "path": path, "removed": removed, "created_at": now}


def _stop_requested(stop):
    if stop is None:
        return False
    try:
        return bool(stop.is_set())
    except AttributeError:
        pass
    try:
        return bool(stop())
    except Exception:  # noqa: BLE001
        return False


def _wait(stop, seconds):
    """Sleep, but wake at once when stop is set so shutdown is not slow."""
    if stop is None:
        time.sleep(seconds)
        return
    try:
        stop.wait(seconds)
        return
    except AttributeError:
        pass
    deadline = time.time() + seconds
    while time.time() < deadline and not _stop_requested(stop):
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


def auto_loop(var_dir, get_settings, save_settings, get_state, log, stop,
              tick=60, version="", initial_delay=60):
    """Background scheduler for automatic backups.

    Deliberately does NOT force a backup on start. A panel that restarts often -
    or that the user restarts while fiddling with settings - would otherwise
    accumulate near identical snapshots on every boot. The interval check inside
    run_auto is the only thing that decides when a backup happens.

    save_settings is used to mirror the last result into the settings file, so
    the maintenance page can show "last backup" right after a restart, before
    the first tick has had a chance to run. Its exact signature is not fixed
    from here, so every call is best effort.
    """
    log = log or (lambda *_a, **_k: None)
    if initial_delay:
        _wait(stop, initial_delay)
    while not _stop_requested(stop):
        try:
            settings = get_settings() if callable(get_settings) else {}
            result = run_auto(var_dir, settings, get_state, log, force=False, version=version)
            if callable(save_settings):
                try:
                    if result.get("ok") and result.get("path"):
                        save_settings({
                            "backup_last_success": result.get("created_at"),
                            "backup_last_error": "",
                        })
                    elif not result.get("ok"):
                        save_settings({"backup_last_error": str(result.get("error") or "")})
                except Exception as exc:  # noqa: BLE001
                    log("auto backup: could not persist settings: %r" % (exc,))
        except Exception as exc:  # noqa: BLE001 - the loop outlives any failure
            log("auto backup loop error: %r" % (exc,))
        _wait(stop, tick)