#!/usr/bin/env python3
"""test_notify_log.py - delivery records and the bilingual push text.

Two things are tested here that are easy to get wrong and impossible to see
from the outside:

  * a record must be written for EVERY attempt, including the ones the channel
    rejected - a history that only shows successes cannot answer "did it fail
    because of the webhook or because nothing was sent";
  * the log must survive a corrupt file, a concurrent write and a clear, and it
    must never contain a channel secret.

Runs in a throwaway directory under tmp/ (tempfile.mkdtemp is unusable here:
the Windows sandbox leaves those ACL-locked).
"""
import json
import os
import shutil
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "fn-pvepanel", "app", "server")
sys.path.insert(0, SERVER)

import notifier  # noqa: E402

FAILS = []
CHECKS = [0]
SCRATCH = os.path.join(ROOT, "tmp", "_unit", "notifylog")


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


CHANNEL = {"id": "ch1", "type": "webhook", "name": "运维群",
           "url": "http://127.0.0.1:1/hook", "headers": "X-Token: super-secret"}


class FakeNotify(object):
    def __init__(self, result=(True, "ok")):
        self.result = result
        self.calls = 0

    def send(self, ch, title, body):
        self.calls += 1
        return self.result


class FakeStore(object):
    def __init__(self, var_dir, settings):
        self.var_dir = var_dir
        self._s = settings

    def settings(self):
        return self._s


def settings(**over):
    base = {
        "notify_enabled": True,
        "lang": "zh",
        "notify_channels": [dict(CHANNEL)],
        "notify_events": {"node_offline": True, "storage_full": True,
                          "auth_failed": True, "test": True},
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- records

def test_record_shape():
    var = fresh("shape")
    rec = notifier.record_delivery(var, CHANNEL, "标题", "正文", "storage_full",
                                   True, "ok")
    for key in ("id", "at", "event", "channel", "channel_id", "type", "ok",
                "detail", "title", "body"):
        check("record has %r" % key, key in rec, "keys %s" % sorted(rec))
    check("id is 12 hex chars", len(rec["id"]) == 12, rec["id"])
    check("event is kept", rec["event"] == "storage_full", rec["event"])
    check("channel name is kept", rec["channel"] == "运维群", rec["channel"])
    check("channel id is kept", rec["channel_id"] == "ch1", rec["channel_id"])
    check("type is kept", rec["type"] == "webhook", rec["type"])
    check("ok flag is boolean", rec["ok"] is True, rec["ok"])
    check("title is kept", rec["title"] == "标题", rec["title"])
    check("body is kept", rec["body"] == "正文", rec["body"])
    # The record holds the message, never the credential that signed it.
    raw = open(os.path.join(var, notifier.NOTIFY_LOG_NAME), encoding="utf-8").read()
    check("channel secret is not in the file", "super-secret" not in raw,
          raw[:200])


def test_newest_first_and_ring():
    var = fresh("ring")
    for i in range(5):
        notifier.record_delivery(var, CHANNEL, "t%d" % i, "b", "manual", True)
    recs = notifier.load_notify_log(var)
    check("five records stored", len(recs) == 5, len(recs))
    check("newest first", [r["title"] for r in recs] == ["t4", "t3", "t2", "t1", "t0"],
          [r["title"] for r in recs])

    # Ring buffer: the cap has to trim from the old end, not the new one.
    orig = notifier.NOTIFY_LOG_MAX
    notifier.NOTIFY_LOG_MAX = 3
    try:
        var2 = fresh("ring2")
        for i in range(5):
            notifier.record_delivery(var2, CHANNEL, "t%d" % i, "b", "manual", True)
        recs = notifier.load_notify_log(var2)
        check("ring keeps only the cap", len(recs) == 3, len(recs))
        check("ring drops the oldest", [r["title"] for r in recs] == ["t4", "t3", "t2"],
              [r["title"] for r in recs])
    finally:
        notifier.NOTIFY_LOG_MAX = orig


def test_corrupt_file_is_empty():
    var = fresh("corrupt")
    path = os.path.join(var, notifier.NOTIFY_LOG_NAME)
    for label, payload in (
            ("truncated json", '{"records": [{"id": "a"'),
            ("not an object", "[1, 2, 3]"),
            ("wrong type", '{"records": "nope"}'),
            ("empty file", ""),
    ):
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
        try:
            recs = notifier.load_notify_log(var)
            check("%s -> empty, no raise" % label, recs == [], recs)
        except Exception as e:
            check("%s -> empty, no raise" % label, False, repr(e))

    # a good file with one junk entry keeps the good ones
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"records": [{"id": "a", "at": 1}, "junk", 42]}, f)
    recs = notifier.load_notify_log(var)
    check("junk entries are filtered out", recs == [{"id": "a", "at": 1}], recs)

    # and a record written after corruption still works
    var2 = fresh("corrupt2")
    with open(os.path.join(var2, notifier.NOTIFY_LOG_NAME), "w", encoding="utf-8") as f:
        f.write("{{{ broken")
    rec = notifier.record_delivery(var2, CHANNEL, "after", "b", "manual", True)
    check("write after corruption succeeds", rec["title"] == "after", rec)
    check("the file is valid again",
          len(notifier.load_notify_log(var2)) == 1, notifier.load_notify_log(var2))


def test_clear():
    var = fresh("clear")
    notifier.record_delivery(var, CHANNEL, "a", "b", "manual", True)
    notifier.record_delivery(var, CHANNEL, "b", "b", "manual", True)
    check("two records before clear", len(notifier.load_notify_log(var)) == 2)
    ok = notifier.clear_notify_log(var)
    check("clear reports success", ok is True, ok)
    check("clear empties the log", notifier.load_notify_log(var) == [],
          notifier.load_notify_log(var))
    check("clear is idempotent", notifier.clear_notify_log(var) is True)


def test_concurrent_writes():
    """Losing a record to a race is exactly the failure this log exists to catch."""
    var = fresh("concurrent")
    errors = []

    def worker(n):
        try:
            for i in range(10):
                notifier.record_delivery(var, CHANNEL, "w%d-%d" % (n, i), "b",
                                         "manual", True)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("no worker raised", not errors, errors)
    recs = notifier.load_notify_log(var)
    check("all 40 records survived", len(recs) == 40, len(recs))


# ---------------------------------------------------------------- push()

def test_push_records_every_attempt():
    var = fresh("push_ok")
    mock = FakeNotify((True, "ok"))
    nt = notifier.Notifier(FakeStore(var, settings()), mock, lambda *_a: None)
    nt.push("storage_full", "标题", "正文")
    recs = notifier.load_notify_log(var)
    check("a successful push is recorded", len(recs) == 1, len(recs))
    if recs:
        check("recorded as ok", recs[0]["ok"] is True, recs[0]["ok"])
        check("event key recorded", recs[0]["event"] == "storage_full", recs[0]["event"])
        check("channel name recorded", recs[0]["channel"] == "运维群", recs[0]["channel"])

    # A rejected channel must also leave a row, with the reason.
    var2 = fresh("push_fail")
    mock2 = FakeNotify((False, "HTTP 400 bad token"))
    nt2 = notifier.Notifier(FakeStore(var2, settings()), mock2, lambda *_a: None)
    nt2.push("storage_full", "标题", "正文")
    recs2 = notifier.load_notify_log(var2)
    check("a rejected push is recorded", len(recs2) == 1, len(recs2))
    if recs2:
        check("recorded as failure", recs2[0]["ok"] is False, recs2[0]["ok"])
        check("failure reason recorded", "HTTP 400" in recs2[0]["detail"],
              recs2[0]["detail"])


def test_push_records_exception():
    """A channel that raises is a failure too - it must still show up."""
    var = fresh("push_raise")

    class Boom(object):
        def send(self, ch, title, body):
            raise RuntimeError("kaboom")

    nt = notifier.Notifier(FakeStore(var, settings()), Boom(), lambda *_a: None)
    nt.push("node_offline", "标题", "正文")
    recs = notifier.load_notify_log(var)
    check("a raising channel is recorded", len(recs) == 1, len(recs))
    if recs:
        check("recorded as failure", recs[0]["ok"] is False, recs[0]["ok"])
        check("exception text in detail", "kaboom" in recs[0]["detail"], recs[0]["detail"])


def test_push_no_channels_writes_nothing():
    var = fresh("push_none")
    nt = notifier.Notifier(FakeStore(var, settings(notify_channels=[])),
                           FakeNotify(), lambda *_a: None)
    nt.push("storage_full", "t", "b")
    check("no channel -> no record", notifier.load_notify_log(var) == [],
          notifier.load_notify_log(var))


def test_observe_records_with_event_key():
    var = fresh("observe")
    mock = FakeNotify((True, "ok"))
    nt = notifier.Notifier(FakeStore(var, settings()), mock, lambda *_a: None)
    nt.observe({"server": {"name": "pve1"}, "meta": {},
                "alerts": [{"title": "节点离线", "detail": "节点: pve1"}]}, "pve1")
    recs = notifier.load_notify_log(var)
    check("observe writes a record", len(recs) == 1, len(recs))
    if recs:
        check("event key is the push key, not the collector title",
              recs[0]["event"] == "node_offline", recs[0]["event"])
        check("title carries both languages",
              "节点离线" in recs[0]["title"] and "Node offline" in recs[0]["title"],
              recs[0]["title"])


# ---------------------------------------------------------------- bilingual

def test_bilingual_text():
    check("bilingual joins with a slash",
          notifier.bilingual("甲", "A") == "甲 / A", notifier.bilingual("甲", "A"))
    body = notifier.bilingual_body(["中"], ["EN"])
    check("body has a blank line between the halves", body == "中\n\nEN", repr(body))
    check("empty english half adds no separator",
          notifier.bilingual_body(["中"], []) == "中",
          repr(notifier.bilingual_body(["中"], [])))

    class Store(object):
        def __init__(self):
            self._s = settings()

        def settings(self):
            return self._s

    mock = FakeNotify()
    nt = notifier.Notifier(Store(), mock, lambda *_a: None)
    nt.observe({"server": {"name": "n1"}, "meta": {"auth_failed": True},
                "alerts": []}, "n1")
    check("auth_failed pushes once", mock.calls == 1, mock.calls)
    check("auth_failed headline is bilingual",
          notifier.HEADLINE["auth_failed"] == ("Token 失效", "Token invalid"),
          notifier.HEADLINE["auth_failed"])


# ---------------------------------------------------------------- main

def main():
    if os.path.isdir(SCRATCH):
        shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(SCRATCH, exist_ok=True)
    print("scratch: %s" % SCRATCH)
    print("=" * 62)
    try:
        test_record_shape()
        test_newest_first_and_ring()
        test_corrupt_file_is_empty()
        test_clear()
        test_concurrent_writes()
        test_push_records_every_attempt()
        test_push_records_exception()
        test_push_no_channels_writes_nothing()
        test_observe_records_with_event_key()
        test_bilingual_text()
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
