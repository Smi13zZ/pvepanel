#!/usr/bin/env python3
"""storage.py - disk usage of the panel's own folders, and one-shot cleanup.

The panel keeps four things on the device that grow on their own: downloaded
installation packages, configuration backups, the panel log and the installer's
log. None of them is large on day one and all of them are somebody's problem on
day four hundred, which is why this module works at two levels:

  * purge_update_cache() runs once in the background on every start, so an
    upgrade never leaves the package it just installed lying around, and a
    download interrupted months ago does not survive forever;
  * report() / cleanup() back the storage page, where a human can see what is
    actually using the space and reclaim it now.

Three rules shape the delete logic, each one a real hazard:

  1. THE PACKAGE FOR THE RUNNING VERSION IS NEVER REMOVED. It is the most likely
     thing to be needed again - a rollback has nothing else to install.
  2. rollback.fpk IS NEVER REMOVED either. That file is the previous build kept
     deliberately so a new version that fails to start can be replaced; deleting
     it during cleanup would quietly disarm the rollback.
  3. NOTHING IS DELETED WHILE A DOWNLOAD OR INSTALL IS IN FLIGHT. A .part file is
     the transfer happening right now, not a leftover, and the installer script
     writes into that same folder while it runs.

Every failure is swallowed and logged. A cleanup pass that cannot delete one file
must never take down startup or fail a whole page.

Pure standard library.
"""

import os
import time

APP_NAME = "fn-pvepanel"
UPDATES_KEEP = 2
UPDATES_KEEP_MAX = 20

#: Suffixes that mean "interrupted transfer". Removed unconditionally: a resumed
#: download starts over anyway, and an abandoned partial file is pure waste.
PARTIAL_SUFFIXES = (".part", ".tmp")

#: A real .fpk with a fixed name, so it needs an explicit exemption rather than
#: a version match.
ROLLBACK_FPK = "rollback.fpk"

_LOG = None


def set_logger(fn):
    """Install the service's log() so cleanup outcomes land somewhere visible."""
    global _LOG
    _LOG = fn if callable(fn) else None


def _emit(msg):
    fn = _LOG
    if callable(fn):
        try:
            fn(msg)
        except Exception:
            pass


# ---------------------------------------------------------------- paths

def update_dir(var_dir):
    return os.path.join(var_dir, "update") if var_dir else ""


def backup_dir(var_dir):
    return os.path.join(var_dir, "backups") if var_dir else ""


def panel_log(var_dir):
    return os.path.join(var_dir, "pvepanel.log") if var_dir else ""


def install_log(var_dir):
    d = update_dir(var_dir)
    return os.path.join(d, "install.log") if d else ""


# ---------------------------------------------------------------- measuring

def _dir_size(path):
    """(bytes, files) under path, recursively. Unreadable entries count as zero.

    A storage page is a diagnostic: failing the whole report because one nested
    directory cannot be read would hide the numbers for everything else.
    """
    total = 0
    files = 0
    if not path or not os.path.isdir(path):
        return 0, 0
    try:
        for root, _dirs, names in os.walk(path):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                    files += 1
                except OSError:
                    pass
    except OSError:
        pass
    return total, files


def _file_size(path):
    try:
        if os.path.isfile(path):
            return os.path.getsize(path), 1
    except OSError:
        pass
    return 0, 0


# ---------------------------------------------------------------- listing

def _is_partial(name):
    return str(name or "").lower().endswith(PARTIAL_SUFFIXES)


def _is_package(name):
    """A package or a partial package. Anything else in the folder is invisible."""
    low = str(name or "").lower()
    if low.endswith(".fpk"):
        return True
    for suffix in PARTIAL_SUFFIXES:
        if low.endswith(".fpk" + suffix):
            return True
    return False


def updates_list(var_dir):
    """Packages and partial downloads in the update folder, newest first.

    partial marks a half-transferred file: those are never usable, and the page
    shows them so the operator can see why cleanup frees what it does.
    """
    d = update_dir(var_dir)
    out = []
    if not d:
        return out
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        if not _is_package(name):
            continue
        path = os.path.join(d, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not os.path.isfile(path):
            continue
        out.append({
            "name": name,
            "bytes": int(st.st_size),
            "t": int(st.st_mtime),
            "partial": _is_partial(name),
        })
    out.sort(key=lambda item: item["t"], reverse=True)
    return out


# ---------------------------------------------------------------- report

#: (key, default label, parts under var_dir, kind, nested).
#:
#: nested=True marks an item that lives inside another listed directory: the
#: installer log sits in update/, so adding it to the total again would report
#: more disk usage than the folder actually occupies.
LAYOUT = (
    ("updates", "更新包缓存", ("update",), "dir", False),
    ("backups", "配置备份", ("backups",), "dir", False),
    ("panel_log", "面板日志", ("pvepanel.log",), "file", False),
    ("panel_log1", "面板日志（上一份）", ("pvepanel.log.1",), "file", False),
    ("update_log", "更新安装日志", ("update", "install.log"), "file", True),
)


def report(var_dir, current_version=""):
    """Directory usage plus the package cache, in the shape the page expects."""
    var_dir = var_dir or ""
    rows = []
    total = 0
    for key, label, parts, kind, nested in LAYOUT:
        path = os.path.join(var_dir, *parts) if var_dir else ""
        if kind == "dir":
            size, files = _dir_size(path)
        else:
            size, files = _file_size(path)
        rows.append({
            "key": key,
            "label": label,
            "bytes": int(size),
            "files": int(files),
            "path": path,
            "kind": kind,
            "nested": bool(nested),
        })
        if not nested:
            total += int(size)
    return {
        "items": rows,
        "total": int(total),
        "var_dir": var_dir,
        "updates": updates_list(var_dir),
        "updates_keep": UPDATES_KEEP,
        "current_version": str(current_version or ""),
    }


# ---------------------------------------------------------------- safety

def installer_busy():
    """True while a download or install is running.

    Imported lazily so this module stays usable from tests and from a build that
    ships without the installer; an import failure simply means "not busy".
    """
    try:
        import installer
        state = installer.status() or {}
    except Exception:
        return False
    return str(state.get("phase") or "") in ("downloading", "installing")


def _within(path, root):
    """True when path really sits inside root after resolving symlinks."""
    try:
        return os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)
    except OSError:
        return False


# ---------------------------------------------------------------- purge

def purge_update_cache(var_dir, keep=UPDATES_KEEP, protect_version="",
                       busy=None, log=None):
    """Trim the update folder to the newest keep packages.

    Removes every partial transfer and every .fpk beyond the newest keep, while
    never touching the running version's package or rollback.fpk. Returns
    {"removed": [...], "kept": [...], "count": n, "freed": bytes, "skipped": str}.

    Idempotent by construction: a second run with the same arguments finds
    nothing left to do. Never raises.
    """
    if log is not None:
        set_logger(log)
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        keep = UPDATES_KEEP
    keep = max(0, min(UPDATES_KEEP_MAX, keep))
    protect = str(protect_version or "").strip().lstrip("vV")
    d = update_dir(var_dir)
    out = {"removed": [], "kept": [], "count": 0, "freed": 0, "skipped": ""}
    if not d or not os.path.isdir(d):
        out["skipped"] = "no_update_dir"
        return out

    running = installer_busy() if busy is None else bool(busy)
    items = updates_list(var_dir)
    # "Newest keep" counts real packages only. A partial is not a package, and
    # rollback.fpk is kept unconditionally anyway - letting it take a slot would
    # quietly halve how many packages survive.
    full = [item for item in items
            if not item["partial"] and item["name"].lower() != ROLLBACK_FPK]
    keep_names = set(item["name"] for item in full[:keep])

    for item in items:
        name = item["name"]
        if name.lower() == ROLLBACK_FPK:
            out["kept"].append(name)
            continue
        if protect and protect in name:
            out["kept"].append(name)
            continue
        if not item["partial"] and name in keep_names:
            out["kept"].append(name)
            continue
        if running:
            # A .part here may be the transfer in progress, and the installer is
            # writing into this directory right now. Decide once, up front.
            continue
        path = os.path.join(d, name)
        if not _within(path, d):
            continue
        try:
            os.remove(path)
        except OSError as exc:
            _emit("storage: cannot remove %s: %r" % (name, exc))
            continue
        out["removed"].append(name)
        out["freed"] += int(item["bytes"])
    if running:
        out["skipped"] = "busy"
        _emit("storage: update in progress, package cleanup skipped")
        return out
    out["count"] = len(out["removed"])
    if out["removed"]:
        _emit("storage: removed %d cached package(s), freed %.1f MB"
              % (out["count"], out["freed"] / 1024.0 / 1024.0))
    return out


# ---------------------------------------------------------------- cleanup

def _truncate(path):
    """Empty a live log file without unlinking it.

    A log another process still holds open must keep its inode: unlinking it
    frees only the directory entry and leaves the space held by the old file
    descriptor, which is exactly the "I deleted it and nothing shrank" report.
    Opening for writing truncates in place and is correct either way.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    try:
        with open(path, "wb"):
            pass
    except OSError:
        return 0
    return int(size)


def _remove(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    try:
        os.remove(path)
    except OSError:
        return 0
    return int(size)


def clear_logs(var_dir):
    """Empty the panel log and the installer log, remove the rotated copy."""
    freed = 0
    removed = []
    main = panel_log(var_dir)
    if main and os.path.isfile(main):
        got = _truncate(main)
        if got:
            freed += got
            removed.append(os.path.basename(main))
    rotated = (main + ".1") if main else ""
    if rotated and os.path.isfile(rotated):
        got = _remove(rotated)
        if got:
            freed += got
            removed.append(os.path.basename(rotated))
    ilog = install_log(var_dir)
    if ilog and os.path.isfile(ilog):
        got = _truncate(ilog)
        if got:
            freed += got
            removed.append(os.path.basename(ilog))
    return {"freed": freed, "removed": removed}


def cleanup(var_dir, target="updates", keep=UPDATES_KEEP, protect_version="",
            busy=None, log=None):
    """Run one cleanup pass. Returns the freed byte count plus what was removed.

    target is "updates", "logs" or "all". A transfer in flight answers
    {"ok": False, "busy": True} so the caller can turn it into a 409 instead of
    deleting the download that is being written right now.
    """
    if log is not None:
        set_logger(log)
    target = str(target or "updates").strip().lower()
    if target not in ("updates", "logs", "all"):
        return {"ok": False, "error": "unknown target", "busy": False,
                "freed": 0, "removed": []}
    running = installer_busy() if busy is None else bool(busy)
    if running:
        return {"ok": False, "busy": True,
                "error": "更新正在进行，稍后再试",
                "freed": 0, "removed": []}

    freed = 0
    removed = []
    if target in ("updates", "all"):
        res = purge_update_cache(var_dir, keep=keep, protect_version=protect_version,
                                 busy=False, log=log)
        freed += int(res.get("freed") or 0)
        removed.extend(res.get("removed") or [])
    if target in ("logs", "all"):
        res = clear_logs(var_dir)
        freed += int(res.get("freed") or 0)
        removed.extend(res.get("removed") or [])
    _emit("storage: cleanup(%s) freed %.1f MB, %d file(s)"
          % (target, freed / 1024.0 / 1024.0, len(removed)))
    return {"ok": True, "busy": False, "target": target, "freed": int(freed),
            "removed": removed, "warnings": []}


def startup_purge(var_dir, current_version="", keep=UPDATES_KEEP, log=None):
    """The automatic pass: trim the package cache, then log the outcome.

    Called from a background thread on every start. A new build starting for the
    first time is exactly the moment the package that installed it became
    redundant, and a plain restart is a free chance to clear what accumulated.
    Never raises, never blocks startup.
    """
    try:
        return purge_update_cache(var_dir, keep=keep, protect_version=current_version,
                                  log=log)
    except Exception as exc:  # noqa: BLE001 - startup must survive anything
        _emit("storage: startup purge failed: %r" % (exc,))
        return {"ok": False, "error": repr(exc), "removed": [], "count": 0,
                "freed": 0, "kept": []}
