#!/usr/bin/env python3
"""test_maint.py - real tests for the maintenance/backup module.

Runs against a scratch directory under pve-panel/tmp/_unit/ (never
tempfile.mkdtemp: creating a fresh dir there and writing into it hits
PermissionError under the Windows sandbox, and the ACLs on such a dir can make
it undeletable).

    <py> tools/test_maint.py            # run everything
    <py> tools/test_maint.py -v         # per check names
    <py> tools/test_maint.py --keep     # leave the scratch dir for inspection

Every check prints what it observed, so a failure is diagnosable from the log
alone rather than from a bare assert line.
"""

import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJECT, "src", "fn-pvepanel", "app", "server"))

import maint  # noqa: E402

UNIT_ROOT = os.path.join(PROJECT, "tmp", "_unit")

PASSED = []
FAILED = []


def run(fn, *args, **kwargs):
    """Call one test, record the outcome, and never let it abort the suite.

    A test harness that stops at the first failure hides the other nine; this
    one runs everything and reports the whole picture at the end.
    """
    name = fn.__name__
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, exc))
        print("  FAIL  %s" % name)
        import traceback
        for line in traceback.format_exc().rstrip().splitlines():
            print("        " + line)
        return False
    PASSED.append(name)
    print("  ok    %s" % name)
    return True


def eq(got, want, what=""):
    if got != want:
        raise AssertionError("%s: got %r, want %r" % (what or "value", got, want))


def truthy(value, what):
    if not value:
        raise AssertionError("%s: expected truthy, got %r" % (what, value))


def fresh(name):
    """A clean scratch dir under tmp/_unit (never mkdtemp - see the docstring)."""
    path = os.path.join(UNIT_ROOT, name)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


SAMPLE_STATE = {
    "config": {
        "refresh": 10,
        "theme": "auto",
        "storage_warn": 85,
        "notify_channels": [
            {"id": "ch1", "type": "bark", "name": "bark",
             "config": {"key": "REAL-BARK-KEY", "server": "https://bark.example"}},
        ],
    },
    "servers": [
        {"id": "s1", "name": "Chin", "host": "192.168.122.50", "port": 8006,
         "token": "PVEAPIToken=monitor@pve!dash=SECRET-UUID", "verify_tls": False},
    ],
}


# ---------------------------------------------------------------- filename

def test_filename_shape():
    name = maint.backup_filename()
    truthy(name.startswith(maint.BACKUP_PREFIX), "prefix")
    truthy(name.endswith(maint.BACKUP_EXT), "ext")
    eq(len(name), len(maint.BACKUP_PREFIX) + len("20260101-000000") + len(maint.BACKUP_EXT),
       "length")
    print("        name = %s" % name)


def test_filename_from_epoch():
    # fixed epoch in LOCAL time, because the name is generated with localtime
    ts = int(time.mktime(time.strptime("2026-11-05 13:47:09", "%Y-%m-%d %H:%M:%S")))
    eq(maint.backup_filename(ts), "pvepanel-backup-20261105-134709.json", "epoch round trip")


def test_filename_from_stamp_string():
    eq(maint.backup_filename("20260101-010203"),
       "pvepanel-backup-20260101-010203.json", "string stamp")


# ---------------------------------------------------------------- make

def test_make_backup_keeps_secrets_by_default():
    doc = maint.make_backup(lambda: SAMPLE_STATE, "26.11.3")
    eq(doc["app"], "fn-pvepanel", "app")
    eq(doc["format"], 1, "format")
    eq(doc["kind"], "manual", "kind")
    eq(doc["contains_secrets"], True, "contains_secrets")
    eq(doc["version"], "26.11.3", "version")
    eq(doc["payload"]["servers"][0]["token"], "PVEAPIToken=monitor@pve!dash=SECRET-UUID",
       "token kept")
    eq(doc["payload"]["config"]["notify_channels"][0]["config"]["key"], "REAL-BARK-KEY",
       "channel key kept")


def test_make_backup_redacted():
    doc = maint.make_backup(lambda: SAMPLE_STATE, "26.11.3", include_secrets=False)
    eq(doc["contains_secrets"], False, "contains_secrets")
    eq(doc["payload"]["servers"][0]["token"], "", "token blanked")
    eq(doc["payload"]["config"]["notify_channels"][0]["config"]["key"], "", "channel key blanked")
    # shape must survive so a restore still knows which servers existed
    eq(doc["payload"]["servers"][0]["host"], "192.168.122.50", "host kept")
    eq(len(doc["payload"]["servers"]), 1, "server count kept")
    eq(doc["payload"]["config"]["notify_channels"][0]["type"], "bark", "channel type kept")


def test_make_backup_detaches_from_live_state():
    """A backup must be a snapshot, not a live view of the store's dicts."""
    live = {"config": {"refresh": 10}, "servers": [{"id": "a", "token": "t"}]}
    doc = maint.make_backup(lambda: live, "v")
    live["servers"][0]["token"] = "MUTATED-AFTER-BACKUP"
    eq(doc["payload"]["servers"][0]["token"], "t", "snapshot independent of live dict")


def test_make_backup_tolerates_garbage_state():
    doc = maint.make_backup(lambda: None, "v")
    eq(doc["payload"]["servers"], [], "None state -> empty servers")
    eq(doc["payload"]["config"], {}, "None state -> empty config")
    doc2 = maint.make_backup(lambda: {"config": "notadict", "servers": "notalist"}, "v")
    eq(doc2["payload"]["servers"], [], "bad types coerced")


# ---------------------------------------------------------------- write/list

def test_write_and_list(scratch):
    var = os.path.join(scratch, "var1")
    os.makedirs(var, exist_ok=True)
    doc = maint.make_backup(lambda: SAMPLE_STATE, "26.11.3")
    path = maint.write_backup(var, doc)
    truthy(os.path.isfile(path), "file written")
    eq(os.path.basename(path)[:len(maint.BACKUP_PREFIX)], maint.BACKUP_PREFIX, "name prefix")
    items = maint.list_backups(var)
    eq(len(items), 1, "one backup listed")
    eq(items[0]["name"], os.path.basename(path), "listed name")
    truthy(items[0]["size"] > 0, "size > 0")
    truthy(items[0]["created_at"] > 0, "created_at parsed from the file name")
    print("        dir = %s" % maint.backup_dir(var))


def test_write_leaves_no_tmp(scratch):
    var = os.path.join(scratch, "var2")
    os.makedirs(var, exist_ok=True)
    maint.write_backup(var, maint.make_backup(lambda: SAMPLE_STATE, "v"))
    leftovers = [n for n in os.listdir(maint.backup_dir(var)) if n.endswith(".tmp")]
    eq(leftovers, [], "no .tmp left behind")


def test_write_backup_file_mode(scratch):
    var = os.path.join(scratch, "var3")
    os.makedirs(var, exist_ok=True)
    path = maint.write_backup(var, maint.make_backup(lambda: SAMPLE_STATE, "v"))
    mode = os.stat(path).st_mode & 0o777
    # Windows does not really carry these bits; assert on POSIX only
    if os.name == "posix":
        eq(mode, 0o600, "mode 600")
    else:
        print("        (skipped mode check on %s; mode=%o)" % (os.name, mode))


def test_same_second_does_not_clobber(scratch):
    """Two backups in the same second must both survive."""
    var = os.path.join(scratch, "var4")
    os.makedirs(var, exist_ok=True)
    fixed = maint.make_backup(lambda: SAMPLE_STATE, "v")
    fixed["created_at"] = 1770000000
    p1 = maint.write_backup(var, dict(fixed))
    p2 = maint.write_backup(var, dict(fixed))
    truthy(p1 != p2, "distinct paths")
    eq(len(maint.list_backups(var)), 2, "both listed")


def test_list_sorted_newest_first(scratch):
    var = os.path.join(scratch, "var5")
    os.makedirs(var, exist_ok=True)
    base = int(time.mktime(time.strptime("2026-01-01 00:00:00", "%Y-%m-%d %H:%M:%S")))
    for offset in (0, 7200, 3600):
        doc = maint.make_backup(lambda: SAMPLE_STATE, "v")
        doc["created_at"] = base + offset
        maint.write_backup(var, doc)
    items = maint.list_backups(var)
    eq(len(items), 3, "three listed")
    got = [b["created_at"] for b in items]
    eq(got, sorted(got, reverse=True), "newest first")


def test_list_ignores_foreign_files(scratch):
    var = os.path.join(scratch, "var6")
    os.makedirs(var, exist_ok=True)
    maint.write_backup(var, maint.make_backup(lambda: SAMPLE_STATE, "v"))
    folder = maint.backup_dir(var)
    for junk in ("notes.txt", "pvepanel-backup-x.json.tmp", "other-backup-1.json"):
        with open(os.path.join(folder, junk), "w", encoding="utf-8") as f:
            f.write("x")
    eq(len(maint.list_backups(var)), 1, "only real backups listed")


def test_list_missing_dir(scratch):
    var = os.path.join(scratch, "does-not-exist")
    eq(maint.list_backups(var), [], "missing dir -> empty list")


# ---------------------------------------------------------------- prune

def test_prune_removes_only_old_auto(scratch):
    var = os.path.join(scratch, "var7")
    os.makedirs(var, exist_ok=True)
    folder = maint.backup_dir(var)
    old = time.time() - 100 * 86400

    def put(kind, age_days, tag):
        doc = maint.make_backup(lambda: SAMPLE_STATE, "v", kind=kind)
        doc["created_at"] = int(time.time() - age_days * 86400)
        path = maint.write_backup(var, doc)
        # the age that matters for pruning is mtime, so force it
        when = time.time() - age_days * 86400
        os.utime(path, (when, when))
        return path

    old_auto = put("auto", 100, "old-auto")
    old_manual = put("manual", 100, "old-manual")
    new_auto = put("auto", 1, "new-auto")

    removed = maint.prune_backups(var, 30)
    eq(removed, 1, "exactly one removed")
    eq(os.path.exists(old_auto), False, "old AUTO deleted")
    eq(os.path.exists(old_manual), True, "old MANUAL kept")
    eq(os.path.exists(new_auto), True, "recent AUTO kept")
    print("        kept: %s" % sorted(os.path.basename(p) for p in
                                      (old_manual, new_auto)))


def test_prune_leaves_unreadable(scratch):
    var = os.path.join(scratch, "var8")
    os.makedirs(var, exist_ok=True)
    folder = maint.backup_dir(var)
    path = os.path.join(folder, "pvepanel-backup-20200101-000000.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    when = time.time() - 500 * 86400
    os.utime(path, (when, when))
    removed = maint.prune_backups(var, 1)
    eq(removed, 0, "nothing removed")
    eq(os.path.exists(path), True, "unparseable file left alone")


def test_prune_clamps_retention(scratch):
    var = os.path.join(scratch, "var9")
    os.makedirs(var, exist_ok=True)
    # 0 / negative / absurd values must not delete or explode
    for value in (0, -5, None, "abc", 999999):
        removed = maint.prune_backups(var, value)
        eq(removed, 0, "clamped retention %r removes nothing from an empty dir" % (value,))


def test_prune_boundary(scratch):
    """A backup just inside the window survives; just outside is removed."""
    var = os.path.join(scratch, "var10")
    os.makedirs(var, exist_ok=True)
    doc = maint.make_backup(lambda: SAMPLE_STATE, "v", kind="auto")
    doc["created_at"] = int(time.time())
    path = maint.write_backup(var, doc)
    inside = time.time() - 29 * 86400
    os.utime(path, (inside, inside))
    eq(maint.prune_backups(var, 30), 0, "29 days old survives a 30 day window")
    outside = time.time() - 31 * 86400
    os.utime(path, (outside, outside))
    eq(maint.prune_backups(var, 30), 1, "31 days old is removed")


# ---------------------------------------------------------------- parse

def test_parse_roundtrip():
    doc = maint.make_backup(lambda: SAMPLE_STATE, "26.11.3")
    raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    parsed = maint.parse_backup(raw)
    eq(parsed["app"], "fn-pvepanel", "parsed app")
    eq(len(parsed["payload"]["servers"]), 1, "parsed servers")


def test_parse_accepts_str():
    doc = maint.make_backup(lambda: SAMPLE_STATE, "v")
    parsed = maint.parse_backup(json.dumps(doc))
    eq(parsed["format"], 1, "str input accepted")


def test_parse_rejects_bad_input():
    cases = [
        (b"not json at all", "unreadable", "garbage bytes"),
        (b"", "unreadable", "empty bytes"),
        (b"[]", "unreadable", "json but not an object"),
        (b"\xff\xfe\x00", "unreadable", "not utf-8"),
        (json.dumps({"app": "qbpanel", "format": 1,
                     "payload": {"servers": []}}).encode(), "foreign", "other app"),
        (json.dumps({"app": "fn-pvepanel", "format": 1, "payload": {}}).encode(),
         "empty", "no servers key"),
        (json.dumps({"app": "fn-pvepanel", "format": 1,
                     "payload": {"servers": "nope"}}).encode(), "empty", "servers not a list"),
        (json.dumps({"app": "fn-pvepanel", "format": 99,
                     "payload": {"servers": []}}).encode(), "format", "future format"),
        (json.dumps({"app": "fn-pvepanel", "payload": {"servers": []}}).encode(),
         "format", "missing format"),
        (json.dumps({"app": "fn-pvepanel", "format": True,
                     "payload": {"servers": []}}).encode(), "format", "bool format"),
    ]
    for raw, want, label in cases:
        try:
            maint.parse_backup(raw)
        except ValueError as exc:
            eq(str(exc), want, "reason for %s" % label)
        else:
            raise AssertionError("%s: expected ValueError %r, none raised" % (label, want))
    print("        %d rejection cases all mapped" % len(cases))


def test_parse_accepts_empty_server_list():
    """A deliberately emptied panel is still a valid backup."""
    doc = maint.make_backup(lambda: {"config": {}, "servers": []}, "v")
    parsed = maint.parse_backup(json.dumps(doc).encode())
    eq(parsed["payload"]["servers"], [], "empty server list is valid")


# ---------------------------------------------------------------- settings/status

def test_auto_settings_clamps():
    eq(maint.auto_settings({})["interval_hours"], 24, "default interval")
    eq(maint.auto_settings({})["retention_days"], 30, "default retention")
    eq(maint.auto_settings({})["enabled"], False, "default disabled")
    eq(maint.auto_settings({"auto_backup_interval_hours": 0})["interval_hours"], 1, "min 1h")
    eq(maint.auto_settings({"auto_backup_interval_hours": 999999})["interval_hours"], 8760,
       "max 8760h")
    eq(maint.auto_settings({"auto_backup_retention_days": 0})["retention_days"], 1, "min 1d")
    eq(maint.auto_settings({"auto_backup_retention_days": 99999})["retention_days"], 3650,
       "max 3650d")
    eq(maint.auto_settings({"auto_backup_interval_hours": "abc"})["interval_hours"], 24,
       "garbage -> default")
    eq(maint.auto_settings({"auto_backup_enabled": 1})["enabled"], True, "truthy enabled")
    eq(maint.auto_settings(None)["interval_hours"], 24, "None settings tolerated")


def test_status_shape(scratch):
    var = os.path.join(scratch, "var11")
    os.makedirs(var, exist_ok=True)
    doc = maint.make_backup(lambda: SAMPLE_STATE, "26.11.3")
    maint.write_backup(var, doc)
    st = maint.status(var, {"auto_backup_enabled": True, "auto_backup_interval_hours": 6,
                            "auto_backup_retention_days": 14})
    eq(st["enabled"], True, "enabled")
    eq(st["interval_hours"], 6, "interval")
    eq(st["retention_days"], 14, "retention")
    eq(st["count"], 1, "count")
    eq(len(st["backups"]), 1, "backups list")
    eq(sorted(st["backups"][0].keys()), ["created_at", "name", "size"], "backup item shape")
    eq(st["prefix"], "pvepanel-backup-", "prefix exposed")
    truthy(st["directory"], "directory exposed")
    for key in ("interval_min", "interval_max", "retention_min", "retention_max",
                "last_success", "last_attempt", "last_error", "next_run", "format"):
        truthy(key in st, "status has %s" % key)
    print("        status keys = %s" % ", ".join(sorted(st.keys())))


def test_status_next_run():
    var = os.path.join(UNIT_ROOT, "var-status")
    os.makedirs(var, exist_ok=True)
    # disabled -> no next run even with a last_success
    maint.save_state(var, {"last_success": 1000})
    eq(maint.status(var, {"auto_backup_enabled": False})["next_run"], 0,
       "disabled -> next_run 0")
    st = maint.status(var, {"auto_backup_enabled": True, "auto_backup_interval_hours": 6})
    eq(st["next_run"], 1000 + 6 * 3600, "enabled -> last + interval")
    eq(st["last_success"], 1000, "last_success from state")


def test_status_never_leaks_token(scratch):
    """status() feeds the browser; it must not carry credentials."""
    var = os.path.join(scratch, "var12")
    os.makedirs(var, exist_ok=True)
    maint.write_backup(var, maint.make_backup(lambda: SAMPLE_STATE, "v"))
    blob = json.dumps(maint.status(var, {"auto_backup_enabled": True}), ensure_ascii=False)
    truthy("SECRET-UUID" not in blob, "no token in status payload")
    truthy("REAL-BARK-KEY" not in blob, "no channel key in status payload")


# ---------------------------------------------------------------- run_auto

def test_run_auto_disabled_skips(scratch):
    var = os.path.join(scratch, "var13")
    os.makedirs(var, exist_ok=True)
    res = maint.run_auto(var, {"auto_backup_enabled": False}, lambda: SAMPLE_STATE, None)
    eq(res, {"ok": True, "skipped": "disabled"}, "disabled skip")
    eq(maint.list_backups(var), [], "nothing written")


def test_run_auto_writes_when_enabled(scratch):
    var = os.path.join(scratch, "var14")
    os.makedirs(var, exist_ok=True)
    res = maint.run_auto(var, {"auto_backup_enabled": True}, lambda: SAMPLE_STATE, None,
                         version="26.11.3")
    eq(res["ok"], True, "ok")
    truthy(res.get("path"), "path returned")
    eq(os.path.basename(res["path"])[:len("pvepanel-backup-")], "pvepanel-backup-", "name")
    items = maint.list_backups(var)
    eq(len(items), 1, "one backup on disk")
    with open(items[0]["path"], encoding="utf-8") as f:
        doc = json.load(f)
    eq(doc["kind"], "auto", "kind auto")
    eq(doc["contains_secrets"], True, "auto backups keep secrets (they must be restorable)")
    eq(maint.load_state(var)["last_success"] > 0, True, "state recorded")


def test_run_auto_not_due(scratch):
    var = os.path.join(scratch, "var15")
    os.makedirs(var, exist_ok=True)
    maint.save_state(var, {"last_success": int(time.time())})
    res = maint.run_auto(var, {"auto_backup_enabled": True, "auto_backup_interval_hours": 24},
                         lambda: SAMPLE_STATE, None)
    eq(res.get("skipped"), "not_due", "skipped not_due")
    eq(maint.list_backups(var), [], "nothing written")


def test_run_auto_force_ignores_gate(scratch):
    var = os.path.join(scratch, "var16")
    os.makedirs(var, exist_ok=True)
    maint.save_state(var, {"last_success": int(time.time())})
    res = maint.run_auto(var, {"auto_backup_enabled": False}, lambda: SAMPLE_STATE, None,
                         force=True)
    eq(res["ok"], True, "forced ok")
    truthy(res.get("path"), "forced wrote a file")
    eq(len(maint.list_backups(var)), 1, "one backup despite disabled")


def test_run_auto_due_after_interval(scratch):
    var = os.path.join(scratch, "var17")
    os.makedirs(var, exist_ok=True)
    maint.save_state(var, {"last_success": int(time.time()) - 7 * 3600})
    res = maint.run_auto(var, {"auto_backup_enabled": True, "auto_backup_interval_hours": 6},
                         lambda: SAMPLE_STATE, None)
    truthy(res.get("path"), "due after 7h with a 6h interval")


def test_run_auto_prunes_during_run(scratch):
    var = os.path.join(scratch, "var18")
    os.makedirs(var, exist_ok=True)
    doc = maint.make_backup(lambda: SAMPLE_STATE, "v", kind="auto")
    doc["created_at"] = int(time.time() - 200 * 86400)
    old = maint.write_backup(var, doc)
    when = time.time() - 200 * 86400
    os.utime(old, (when, when))
    res = maint.run_auto(var, {"auto_backup_enabled": True, "auto_backup_retention_days": 30},
                         lambda: SAMPLE_STATE, None)
    eq(res["ok"], True, "ok")
    eq(res.get("removed"), 1, "old auto pruned during the run")
    eq(os.path.exists(old), False, "old file gone")


def test_run_auto_failure_is_recorded(scratch):
    """A broken get_state must not raise, and must leave a visible error."""
    var = os.path.join(scratch, "var19")
    os.makedirs(var, exist_ok=True)

    def boom():
        raise RuntimeError("state is on fire")

    res = maint.run_auto(var, {"auto_backup_enabled": True}, boom, None)
    eq(res["ok"], False, "reported as failure")
    truthy("state is on fire" in res["error"], "error text carried")
    state = maint.load_state(var)
    truthy("state is on fire" in state.get("last_error", ""), "error persisted")
    st = maint.status(var, {"auto_backup_enabled": True})
    truthy("state is on fire" in st["last_error"], "status surfaces the error")
    print("        persisted error = %s" % state.get("last_error"))


def test_run_auto_success_clears_error(scratch):
    var = os.path.join(scratch, "var20")
    os.makedirs(var, exist_ok=True)
    maint.save_state(var, {"last_error": "previous failure"})
    maint.run_auto(var, {"auto_backup_enabled": True}, lambda: SAMPLE_STATE, None)
    eq(maint.load_state(var)["last_error"], "", "error cleared on success")


# ---------------------------------------------------------------- auto_loop

def test_auto_loop_runs_and_stops(scratch):
    """Drive the real scheduler thread with a short tick."""
    import threading
    var = os.path.join(scratch, "var21")
    os.makedirs(var, exist_ok=True)
    stop = threading.Event()
    logs = []
    saved = []
    settings = {"auto_backup_enabled": True, "auto_backup_interval_hours": 1}

    th = threading.Thread(
        target=maint.auto_loop,
        args=(var, lambda: settings, lambda patch: saved.append(patch),
              lambda: SAMPLE_STATE, logs.append, stop),
        kwargs={"tick": 0.05, "version": "26.11.3", "initial_delay": 0},
        daemon=True,
    )
    th.start()
    deadline = time.time() + 6.0
    while time.time() < deadline and not maint.list_backups(var):
        time.sleep(0.05)
    stop.set()
    th.join(5.0)
    eq(th.is_alive(), False, "loop stopped promptly after stop.set()")
    items = maint.list_backups(var)
    eq(len(items), 1, "exactly one backup (interval gate held across ticks)")
    truthy(saved, "save_settings mirror was called")
    eq(saved[-1]["backup_last_error"], "", "mirrored success has no error")
    truthy(any("auto backup ->" in line for line in logs), "logged the backup")
    print("        loop backups=%d mirror_calls=%d log_lines=%d"
          % (len(items), len(saved), len(logs)))


def test_auto_loop_survives_broken_get_state(scratch):
    import threading
    var = os.path.join(scratch, "var22")
    os.makedirs(var, exist_ok=True)
    stop = threading.Event()
    logs = []

    def boom():
        raise RuntimeError("boom")

    th = threading.Thread(
        target=maint.auto_loop,
        args=(var, lambda: {"auto_backup_enabled": True}, lambda p: None,
              boom, logs.append, stop),
        kwargs={"tick": 0.05, "initial_delay": 0},
        daemon=True,
    )
    th.start()
    time.sleep(1.2)
    alive_after_errors = th.is_alive()
    stop.set()
    th.join(5.0)
    eq(alive_after_errors, True, "loop survived repeated exceptions")
    truthy(any("failed" in line for line in logs), "failure was logged")
    print("        log lines during failure = %d" % len(logs))


def test_auto_loop_initial_delay_stops_early(scratch):
    import threading
    var = os.path.join(scratch, "var23")
    os.makedirs(var, exist_ok=True)
    stop = threading.Event()
    th = threading.Thread(
        target=maint.auto_loop,
        args=(var, lambda: {"auto_backup_enabled": True}, lambda p: None,
              lambda: SAMPLE_STATE, None, stop),
        kwargs={"tick": 60, "initial_delay": 30},
        daemon=True,
    )
    t0 = time.time()
    th.start()
    time.sleep(0.2)
    stop.set()
    th.join(5.0)
    elapsed = time.time() - t0
    truthy(elapsed < 3.0, "stop.set() interrupts the initial delay (took %.2fs)" % elapsed)
    eq(maint.list_backups(var), [], "no backup taken during the delay")

def test_auto_loop_survives_broken_get_settings(scratch):
    """A failure in get_settings happens OUTSIDE run_auto's own try block.

    run_auto protects make/write/prune, so a broken get_state never reaches the
    loop's guard - it is swallowed there and reported as {"ok": False}. The
    loop-level except is only load-bearing when something outside run_auto
    throws, and get_settings is exactly that. Without this case the loop guard
    can be deleted and the whole suite still passes.
    """
    import threading
    var = os.path.join(scratch, "var24")
    os.makedirs(var, exist_ok=True)
    stop = threading.Event()
    logs = []

    def bad_settings():
        raise RuntimeError("settings store unreadable")

    th = threading.Thread(
        target=maint.auto_loop,
        args=(var, bad_settings, lambda p: None, lambda: SAMPLE_STATE, logs.append, stop),
        kwargs={"tick": 0.05, "initial_delay": 0},
        daemon=True,
    )
    th.start()
    time.sleep(1.2)
    alive = th.is_alive()
    ticks = len(logs)
    stop.set()
    th.join(5.0)
    eq(alive, True, "loop survived get_settings raising")
    truthy(ticks >= 3, "loop kept ticking after the failure (got %d)" % ticks)
    truthy(any("loop error" in line for line in logs), "the loop-level error was logged")
    print("        ticks while failing = %d" % ticks)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep tmp/_unit scratch dirs")
    args = ap.parse_args()

    os.makedirs(UNIT_ROOT, exist_ok=True)
    scratch = fresh("maint")
    print("scratch = %s" % scratch)
    print("python  = %s" % sys.version.split()[0])
    print("module  = %s" % maint.__file__)
    print("")

    print("[filename]")
    run(test_filename_shape)
    run(test_filename_from_epoch)
    run(test_filename_from_stamp_string)

    print("[make_backup]")
    run(test_make_backup_keeps_secrets_by_default)
    run(test_make_backup_redacted)
    run(test_make_backup_detaches_from_live_state)
    run(test_make_backup_tolerates_garbage_state)

    print("[write_backup / list_backups]")
    run(test_write_and_list, scratch)
    run(test_write_leaves_no_tmp, scratch)
    run(test_write_backup_file_mode, scratch)
    run(test_same_second_does_not_clobber, scratch)
    run(test_list_sorted_newest_first, scratch)
    run(test_list_ignores_foreign_files, scratch)
    run(test_list_missing_dir, scratch)

    print("[prune_backups]")
    run(test_prune_removes_only_old_auto, scratch)
    run(test_prune_leaves_unreadable, scratch)
    run(test_prune_clamps_retention, scratch)
    run(test_prune_boundary, scratch)

    print("[parse_backup]")
    run(test_parse_roundtrip)
    run(test_parse_accepts_str)
    run(test_parse_rejects_bad_input)
    run(test_parse_accepts_empty_server_list)

    print("[settings / status]")
    run(test_auto_settings_clamps)
    run(test_status_shape, scratch)
    run(test_status_next_run)
    run(test_status_never_leaks_token, scratch)

    print("[run_auto]")
    run(test_run_auto_disabled_skips, scratch)
    run(test_run_auto_writes_when_enabled, scratch)
    run(test_run_auto_not_due, scratch)
    run(test_run_auto_force_ignores_gate, scratch)
    run(test_run_auto_due_after_interval, scratch)
    run(test_run_auto_prunes_during_run, scratch)
    run(test_run_auto_failure_is_recorded, scratch)
    run(test_run_auto_success_clears_error, scratch)

    print("[auto_loop]")
    run(test_auto_loop_runs_and_stops, scratch)
    run(test_auto_loop_survives_broken_get_state, scratch)
    run(test_auto_loop_survives_broken_get_settings, scratch)
    run(test_auto_loop_initial_delay_stops_early, scratch)

    print("")
    total = len(PASSED) + len(FAILED)
    print("=" * 62)
    print("passed %d / %d" % (len(PASSED), total))
    if FAILED:
        print("FAILED:")
        for name, exc in FAILED:
            print("  - %s: %s" % (name, exc))
    print("=" * 62)

    if args.keep:
        print("kept scratch: %s" % scratch)
    else:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
