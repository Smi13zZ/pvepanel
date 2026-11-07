#!/usr/bin/env python3
"""test_storage.py - the cleanup rules that are easy to get subtly wrong.

The interesting cases here are all destructive in production:

  * the package for the running version must survive a purge (rollback needs it)
  * rollback.fpk must survive too (deleting it disarms the rollback)
  * a .part must be removed when idle but kept while a download runs
  * a log is truncated in place, never unlinked (a held fd keeps the space)

Every case runs in a throwaway directory under tmp/, never tempfile.mkdtemp():
on Windows the sandbox leaves those ACL-locked and later writes fail.
"""
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "fn-pvepanel", "app", "server")
sys.path.insert(0, SERVER)

import storage  # noqa: E402

FAILS = []
CHECKS = [0]
SCRATCH = os.path.join(ROOT, "tmp", "_unit", "storage")


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % name)
        return True
    FAILS.append("%s %s" % (name, detail))
    print("  FAIL %s %s" % (name, detail))
    return False


def fresh(name):
    d = os.path.join(SCRATCH, name)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


def make(var, path, size=1024, mtime=None):
    full = os.path.join(var, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(b"x" * size)
    if mtime is not None:
        os.utime(full, (mtime, mtime))
    return full


def names(seq):
    return sorted(seq)


# ---------------------------------------------------------------- purge

def test_keeps_newest_two():
    var = fresh("purge_keep")
    upd = os.path.join(var, "update")
    os.makedirs(upd, exist_ok=True)
    # mtime decides "newest": distinct timestamps, oldest first
    for i, ver in enumerate(("26.11.1", "26.11.2", "26.11.3", "26.11.4")):
        make(var, "update/fn-pvepanel-%s-fnos-all.fpk" % ver, 100 + i,
             mtime=1000 + i)
    res = storage.purge_update_cache(var, keep=2, protect_version="26.11.4", busy=False)
    left = sorted(os.listdir(upd))
    check("keep=2 leaves exactly 2 packages", len(left) == 2, "left %s" % left)
    check("the running version survives", "fn-pvepanel-26.11.4-fnos-all.fpk" in left,
          "left %s" % left)
    check("the second newest survives", "fn-pvepanel-26.11.3-fnos-all.fpk" in left,
          "left %s" % left)
    check("the two oldest are gone", res["count"] == 2, "removed %s" % res["removed"])
    # sizes were 100, 101, 102, 103 with keep=2: the two oldest (100 + 101) go
    check("freed byte count is the sum of the removed files", res["freed"] == 201,
          "freed %r" % res["freed"])
    return res


def test_protected_survives_even_beyond_keep():
    var = fresh("purge_protect")
    os.makedirs(os.path.join(var, "update"), exist_ok=True)
    # The running build is the OLDEST one - it must still survive.
    make(var, "update/fn-pvepanel-26.10.1-fnos-all.fpk", 10, mtime=1000)
    for i, ver in enumerate(("26.11.2", "26.11.3", "26.11.4")):
        make(var, "update/fn-pvepanel-%s-fnos-all.fpk" % ver, 20, mtime=2000 + i)
    res = storage.purge_update_cache(var, keep=2, protect_version="v26.10.1", busy=False)
    left = sorted(os.listdir(os.path.join(var, "update")))
    check("a protected old version survives (leading v stripped)",
          "fn-pvepanel-26.10.1-fnos-all.fpk" in left, "left %s" % left)
    check("keep=2 still applies to the rest", len(left) == 3, "left %s" % left)
    return res


def test_rollback_fpk_never_deleted():
    var = fresh("purge_rollback")
    os.makedirs(os.path.join(var, "update"), exist_ok=True)
    make(var, "update/rollback.fpk", 500, mtime=1000)
    for i, ver in enumerate(("26.11.1", "26.11.2", "26.11.3")):
        make(var, "update/fn-pvepanel-%s-fnos-all.fpk" % ver, 30, mtime=2000 + i)
    storage.purge_update_cache(var, keep=1, protect_version="26.11.3", busy=False)
    left = sorted(os.listdir(os.path.join(var, "update")))
    check("rollback.fpk is never removed", "rollback.fpk" in left, "left %s" % left)


def test_rollback_does_not_take_a_keep_slot():
    """rollback.fpk is protected anyway; counting it in "keep 2" would leave 1."""
    var = fresh("purge_rollback_slot")
    os.makedirs(os.path.join(var, "update"), exist_ok=True)
    # rollback.fpk is the newest file here: if it took a keep slot, only one
    # real package would survive.
    make(var, "update/rollback.fpk", 500, mtime=9000)
    for i, ver in enumerate(("26.11.1", "26.11.2", "26.11.3")):
        make(var, "update/fn-pvepanel-%s-fnos-all.fpk" % ver, 30, mtime=1000 + i)
    storage.purge_update_cache(var, keep=2, protect_version="", busy=False)
    left = sorted(n for n in os.listdir(os.path.join(var, "update")) if n.endswith(".fpk"))
    check("keep=2 still keeps two real packages", len(left) == 3,
          "left %s (rollback + 2 packages)" % left)
    check("the two newest packages survive",
          "fn-pvepanel-26.11.3-fnos-all.fpk" in left and "fn-pvepanel-26.11.2-fnos-all.fpk" in left,
          "left %s" % left)
    check("rollback.fpk survives", "rollback.fpk" in left, "left %s" % left)


def test_partials_always_removed_when_idle():
    var = fresh("purge_partial")
    os.makedirs(os.path.join(var, "update"), exist_ok=True)
    make(var, "update/fn-pvepanel-26.11.4-fnos-all.fpk", 100, mtime=2000)
    make(var, "update/fn-pvepanel-26.11.5-fnos-all.fpk.part", 40, mtime=3000)
    make(var, "update/fn-pvepanel-26.11.6-fnos-all.fpk.tmp", 40, mtime=3001)
    res = storage.purge_update_cache(var, keep=2, protect_version="26.11.4", busy=False)
    left = sorted(os.listdir(os.path.join(var, "update")))
    check("both partial files removed", len(left) == 1, "left %s" % left)
    check("the real package stays", left == ["fn-pvepanel-26.11.4-fnos-all.fpk"], "left %s" % left)
    check("partials report as removed", res["count"] == 2, "removed %s" % res["removed"])


def test_busy_keeps_everything():
    var = fresh("purge_busy")
    os.makedirs(os.path.join(var, "update"), exist_ok=True)
    make(var, "update/fn-pvepanel-26.11.1-fnos-all.fpk", 10, mtime=1000)
    make(var, "update/fn-pvepanel-26.11.2-fnos-all.fpk", 10, mtime=2000)
    make(var, "update/fn-pvepanel-26.11.3-fnos-all.fpk.part", 10, mtime=3000)
    res = storage.purge_update_cache(var, keep=1, protect_version="", busy=True)
    left = sorted(os.listdir(os.path.join(var, "update")))
    check("busy: nothing is deleted", len(left) == 3, "left %s" % left)
    check("busy: skipped reason is reported", res["skipped"] == "busy", "skipped %r" % res["skipped"])


def test_idempotent():
    var = fresh("purge_idem")
    os.makedirs(os.path.join(var, "update"), exist_ok=True)
    for i, ver in enumerate(("26.11.1", "26.11.2", "26.11.3")):
        make(var, "update/fn-pvepanel-%s-fnos-all.fpk" % ver, 10, mtime=1000 + i)
    first = storage.purge_update_cache(var, keep=2, protect_version="26.11.3", busy=False)
    second = storage.purge_update_cache(var, keep=2, protect_version="26.11.3", busy=False)
    check("first run removes something", first["count"] == 1, "count %r" % first["count"])
    check("second run removes nothing", second["count"] == 0, "count %r" % second["count"])


def test_missing_dir_is_not_an_error():
    var = fresh("purge_nodir")
    res = storage.purge_update_cache(var, keep=2, protect_version="", busy=False)
    check("no update dir -> skipped, no exception", res["skipped"] == "no_update_dir",
          "skipped %r" % res["skipped"])
    check("no update dir -> nothing removed", res["count"] == 0, "count %r" % res["count"])


# ---------------------------------------------------------------- report

def test_report_shape():
    var = fresh("report")
    make(var, "update/fn-pvepanel-26.11.4-fnos-all.fpk", 2048, mtime=2000)
    make(var, "update/install.log", 100)
    make(var, "backups/pvepanel-backup-20260101-000000.json", 512)
    make(var, "pvepanel.log", 300)
    data = storage.report(var, "26.11.4")
    check("report has the contract keys",
          set(("items", "total", "var_dir", "updates", "updates_keep", "current_version"))
          <= set(data), "keys %s" % sorted(data))
    items = dict((it["key"], it) for it in data["items"])
    check("update dir size includes the installer log", items["updates"]["bytes"] >= 2148,
          "bytes %r" % items["updates"]["bytes"])
    check("nested installer log is flagged", items["update_log"]["nested"] is True)
    # total must equal the non-nested rows only, or the page over-reports
    expect = sum(it["bytes"] for it in data["items"] if not it["nested"])
    check("total excludes nested items", data["total"] == expect,
          "total %r expect %r" % (data["total"], expect))
    check("updates_keep is 2", data["updates_keep"] == 2)
    check("current_version is echoed", data["current_version"] == "26.11.4")
    check("updates list includes the package", len(data["updates"]) == 1,
          "updates %r" % data["updates"])


def test_report_unreadable_dir_does_not_fail():
    var = fresh("report_perm")
    make(var, "update/only.fpk", 10)
    # Point the panel at a var dir with an unreadable subfolder: the report must
    # still come back with the rows it could measure.
    os.makedirs(os.path.join(var, "backups"), exist_ok=True)
    data = storage.report(var, "")
    check("report survives an empty backups dir", isinstance(data["items"], list))
    check("report always returns every row", len(data["items"]) == len(storage.LAYOUT),
          "rows %r" % len(data["items"]))


# ---------------------------------------------------------------- logs

def test_logs_truncated_in_place():
    var = fresh("logs")
    main = make(var, "pvepanel.log", 500)
    rotated = make(var, "pvepanel.log.1", 400)
    ilog = make(var, "update/install.log", 200)
    before = os.stat(main).st_ino
    res = storage.clear_logs(var)
    check("panel log is truncated to 0", os.path.getsize(main) == 0,
          "size %r" % os.path.getsize(main))
    check("truncation keeps the inode (same file, not unlinked)",
          os.stat(main).st_ino == before, "inode changed")
    check("rotated copy is removed", not os.path.exists(rotated))
    check("installer log is truncated", os.path.getsize(ilog) == 0)
    check("freed covers all three", res["freed"] == 1100, "freed %r" % res["freed"])


def test_cleanup_targets():
    var = fresh("cleanup_targets")
    make(var, "update/fn-pvepanel-26.11.1-fnos-all.fpk", 10, mtime=1000)
    make(var, "update/fn-pvepanel-26.11.2-fnos-all.fpk", 10, mtime=2000)
    make(var, "update/fn-pvepanel-26.11.3-fnos-all.fpk", 10, mtime=3000)
    make(var, "pvepanel.log", 700)

    res = storage.cleanup(var, target="logs", log=None, busy=False)
    check("target=logs leaves packages alone",
          len(os.listdir(os.path.join(var, "update"))) == 3,
          "left %s" % sorted(os.listdir(os.path.join(var, "update"))))
    check("target=logs empties the panel log", os.path.getsize(os.path.join(var, "pvepanel.log")) == 0)

    res = storage.cleanup(var, target="updates", keep=2, protect_version="26.11.3", busy=False)
    left = sorted(os.listdir(os.path.join(var, "update")))
    check("target=updates trims to keep", len(left) == 2, "left %s" % left)

    res = storage.cleanup(var, target="all", keep=1, protect_version="26.11.3", busy=False)
    left = sorted(os.listdir(os.path.join(var, "update")))
    check("target=all also trims packages", left == ["fn-pvepanel-26.11.3-fnos-all.fpk"],
          "left %s" % left)


def test_cleanup_busy_is_refused():
    var = fresh("cleanup_busy")
    make(var, "update/fn-pvepanel-26.11.1-fnos-all.fpk", 10, mtime=1000)
    res = storage.cleanup(var, target="all", busy=True)
    check("busy cleanup is refused", res.get("ok") is False, "res %r" % res)
    check("busy cleanup sets the busy flag", res.get("busy") is True, "res %r" % res)
    check("busy cleanup deletes nothing",
          len(os.listdir(os.path.join(var, "update"))) == 1)


def test_cleanup_unknown_target():
    var = fresh("cleanup_badtarget")
    res = storage.cleanup(var, target="everything", busy=False)
    check("unknown target is refused", res.get("ok") is False, "res %r" % res)


def test_startup_purge_never_raises():
    # A var dir that does not exist at all: the automatic pass must be silent.
    var = os.path.join(SCRATCH, "startup_missing", "var")
    if os.path.isdir(os.path.dirname(var)):
        shutil.rmtree(os.path.dirname(var), ignore_errors=True)
    res = storage.startup_purge(var, "26.11.4")
    check("startup purge on a missing dir does not raise", isinstance(res, dict),
          "res %r" % res)


# ---------------------------------------------------------------- main

def main():
    if os.path.isdir(SCRATCH):
        shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(SCRATCH, exist_ok=True)
    print("scratch: %s" % SCRATCH)
    print("=" * 62)
    try:
        test_keeps_newest_two()
        test_protected_survives_even_beyond_keep()
        test_rollback_fpk_never_deleted()
        test_rollback_does_not_take_a_keep_slot()
        test_partials_always_removed_when_idle()
        test_busy_keeps_everything()
        test_idempotent()
        test_missing_dir_is_not_an_error()
        test_report_shape()
        test_report_unreadable_dir_does_not_fail()
        test_logs_truncated_in_place()
        test_cleanup_targets()
        test_cleanup_busy_is_refused()
        test_cleanup_unknown_target()
        test_startup_purge_never_raises()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    print("=" * 62)
    if FAILS:
        print("FAILED %d / %d" % (len(FAILS), CHECKS[0]))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("passed %d / %d" % (CHECKS[0], CHECKS[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
