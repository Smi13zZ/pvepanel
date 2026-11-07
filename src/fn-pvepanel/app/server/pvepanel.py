#!/usr/bin/env python3
"""pvepanel.py - the panel's local HTTP service.

Serves the static front-end and a small JSON API. The browser never talks to
PVE: every request is proxied here, so the cluster addresses and tokens stay on
the box and there is no CORS surface at all.

Binding defaults to 127.0.0.1 on purpose. The fnOS desktop entry reaches the
panel through index.cgi, and that path is gated by the fnOS login. The raw port
is not - anything that can route to it can read the whole cluster and start or
stop guests, so exposing it is only safe with a panel password.
"""

import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import collector  # noqa: E402
import pveclient  # noqa: E402
import store as store_mod  # noqa: E402

# Both of these are optional at import time: the panel has to keep working when
# a build ships without them, and a missing module must not take the whole
# service down.
try:
    import notify as notify_mod  # noqa: E402
except Exception:  # pragma: no cover
    notify_mod = None
try:
    import update as update_mod  # noqa: E402
except Exception:  # pragma: no cover
    update_mod = None
try:
    import notifier as notifier_mod  # noqa: E402
except Exception:  # pragma: no cover
    notifier_mod = None
try:
    import installer as installer_mod  # noqa: E402
except Exception:  # pragma: no cover
    installer_mod = None
try:
    import maint as maint_mod  # noqa: E402
except Exception:  # pragma: no cover
    maint_mod = None
try:
    import storage as storage_mod  # noqa: E402
except Exception:  # pragma: no cover
    storage_mod = None

APP_NAME = "fn-pvepanel"
VERSION = os.environ.get("PVEPANEL_VERSION") or "0.0.0"
START_TIME = time.time()


def var_dir():
    d = os.environ.get("TRIM_PKGVAR")
    if not d:
        d = os.path.abspath(os.path.join(HERE, "..", "var"))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def web_dir():
    return os.path.abspath(os.path.join(HERE, "..", "web"))


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        path = os.path.join(var_dir(), "pvepanel.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


STORE = store_mod.Store(var_dir(), log)
COLLECT = collector.Collector(STORE, log)
NOTIFIER = (notifier_mod.Notifier(STORE, notify_mod, log, var_dir=var_dir())
            if notifier_mod else None)
SNAP_LOCK = threading.Lock()


def get_snapshot(force=False, timeframe=None, server_id=None):
    """Collect one server's snapshot. Only one collection runs at a time; a
    concurrent page load waits for the in-flight one instead of piling on."""
    rec = STORE.server(server_id) if server_id else STORE.active_server()
    if rec is None:
        return {"ok": False, "meta": {"reason": "unconfigured", "configured": False},
                "errors": [], "alerts": [], "timeframe": timeframe or "hour"}
    if not SNAP_LOCK.acquire(timeout=30):
        return {"ok": False, "meta": {"reason": "busy"},
                "errors": ["上一次刷新还没结束，请稍候"], "alerts": []}
    try:
        snap = COLLECT.snapshot(rec, timeframe=timeframe, force=force)
    except Exception:
        log("snapshot failed:\n" + traceback.format_exc())
        return {"ok": False, "meta": {"reason": "internal"},
                "errors": [traceback.format_exc().splitlines()[-1]], "alerts": []}
    finally:
        SNAP_LOCK.release()
    # Push off the request path: a slow webhook must never delay a page load,
    # and a failing one must never break it. The notifier owns its own cooldown
    # so repeated snapshots do not repeat the same page.
    if NOTIFIER is not None:
        try:
            threading.Thread(
                target=NOTIFIER.observe,
                args=(snap, (snap.get("server") or {}).get("name") or ""),
                daemon=True).start()
        except Exception:
            pass
    return snap


def backup_state():
    """Everything a configuration backup has to carry.

    The server records include their tokens on purpose: a restore that needed
    every token typed again would not be a backup. The file this feeds is
    written 0600 into a 0700 directory, which is the same protection the live
    servers.json already relies on.
    """
    return {"config": STORE.settings(), "servers": STORE.servers()}


MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "pvepanel/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # the collector already logs what matters

    # ---------------- helpers ----------------

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # The browser navigated away or a poll was abandoned. Not an
                # error worth a stack trace: the client is gone by definition.
                pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    # The request body is a stream: reading it twice blocks forever on the
    # second call, because the bytes are already gone and the socket is still
    # open waiting for more. do_POST reads it exactly once and both accessors
    # serve that copy.
    BODY_LIMIT = 8 << 20      # a configuration backup is small; 8 MB is roomy

    def _read_raw(self):
        if getattr(self, "_raw_cache", None) is not None:
            return self._raw_cache
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > self.BODY_LIMIT:
            self._raw_cache = b""
            return self._raw_cache
        try:
            self._raw_cache = self.rfile.read(n)
        except Exception:
            self._raw_cache = b""
        return self._raw_cache

    def _raw_body(self):
        """The request body as bytes, for endpoints that take a file rather
        than a JSON object."""
        return self._read_raw()

    def _body(self):
        raw = self._read_raw()
        if not raw:
            return {}
        try:
            val = json.loads(raw.decode("utf-8"))
            return val if isinstance(val, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---------------- routes ----------------

    def do_HEAD(self):
        self.do_GET()

    def do_DELETE(self):
        """Only the delivery log is deletable; everything else is POST.

        Kept as a real DELETE so the front-end reads as "clear this resource"
        rather than "send an empty POST that happens to clear something".
        """
        parsed = urlparse(self.path)
        path = parsed.path
        self._raw_cache = None
        try:
            self._read_raw()
            if path == "/api/notify/log":
                return self._json(self._notify_log_clear())
            return self._json({"ok": False, "error": "not found"}, 404)
        except Exception:
            log("DELETE %s failed:\n%s" % (path, traceback.format_exc()))
            try:
                self._json({"ok": False, "error": "internal error"}, 500)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/"):
                return self._api(path, parse_qs(parsed.query))
            return self._static(path)
        except Exception:
            log("GET %s failed:\n%s" % (path, traceback.format_exc()))
            try:
                self._json({"ok": False, "error": "internal error"}, 500)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        self._raw_cache = None
        try:
            # Read the request body once, here, before any route can look at it.
            self._read_raw()
            body = self._body()
            if path == "/api/settings":
                SETTINGS_LOCK.acquire()
                try:
                    out = STORE.update(body)
                finally:
                    SETTINGS_LOCK.release()
                if "refresh" in body or "timeframe" in body:
                    COLLECT.invalidate()
                return self._json({"ok": True, "settings": out, "servers": STORE.public_servers()})
            if path == "/api/server":
                rec = STORE.upsert_server(body)
                COLLECT.invalidate(rec.get("id") if rec else None)
                return self._json({"ok": bool(rec), "server": rec,
                                   "servers": STORE.public_servers(),
                                   "settings": STORE.settings()})
            if path == "/api/server/delete":
                ok = STORE.delete_server(str(body.get("id") or ""))
                COLLECT.invalidate(str(body.get("id") or ""))
                return self._json({"ok": ok, "servers": STORE.public_servers(),
                                   "settings": STORE.settings()})
            if path == "/api/active":
                STORE.update({"active": str(body.get("id") or "")})
                return self._json({"ok": True, "settings": STORE.settings()})
            if path == "/api/refresh":
                tf = (parse_qs(parsed.query).get("tf") or [None])[0]
                sid = (parse_qs(parsed.query).get("server") or [None])[0]
                return self._json({"ok": True,
                                   "snapshot": get_snapshot(force=True, timeframe=tf,
                                                            server_id=sid)})
            if path == "/api/guest/action":
                return self._json(self._guest_action(body))
            if path == "/api/notify/channel":
                if body.get("_delete"):
                    return self._json(self._notify_delete(body))
                return self._json(self._notify_channel(body))
            if path == "/api/notify/test":
                return self._json(self._notify_test(body))
            if path == "/api/update/check":
                return self._json(self._update_check(bool(body.get("force"))))
            if path == "/api/update/install":
                return self._json(self._update_install())
            if path == "/api/server/test":
                return self._json(self._server_test(body))
            if path == "/api/backup/import":
                return self._json(self._backup_import(self._raw_body()))
            if path == "/api/backup/to-folder":
                return self._json(self._backup_to_folder())
            if path == "/api/backup/run":
                return self._json(self._backup_run())
            if path == "/api/backup/restore":
                return self._json(self._backup_restore(body))
            if path == "/api/backup/delete":
                return self._json(self._backup_delete(body))
            if path == "/api/storage/cleanup":
                return self._json(self._storage_cleanup(body))
            return self._json({"ok": False, "error": "not found"}, 404)
        except Exception:
            log("POST %s failed:\n%s" % (path, traceback.format_exc()))
            try:
                self._json({"ok": False, "error": "internal error"}, 500)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

    # ---------------- API ----------------

    def _api(self, path, query):
        if path == "/api/health":
            return self._json({
                "ok": True, "app": APP_NAME, "version": VERSION,
                "python": "%d.%d.%d" % sys.version_info[:3],
                "var_dir": var_dir(), "uptime": int(time.time() - START_TIME),
            })
        if path == "/api/bootstrap":
            force = (query.get("force") or ["0"])[0] in ("1", "true")
            tf = (query.get("tf") or [None])[0]
            sid = (query.get("server") or [None])[0]
            return self._json({
                "ok": True,
                "app": {"name": APP_NAME, "version": VERSION,
                        "uptime": int(time.time() - START_TIME)},
                "settings": STORE.settings(),
                "servers": STORE.public_servers(),
                "notify": self._notify_meta(),
                # never blocks: see _update_cached()
                "update": self._update_cached(),
                "snapshot": get_snapshot(force=force, timeframe=tf, server_id=sid),
            })
        if path == "/api/snapshot":
            force = (query.get("force") or ["0"])[0] in ("1", "true")
            tf = (query.get("tf") or [None])[0]
            sid = (query.get("server") or [None])[0]
            return self._json({"ok": True, "snapshot": get_snapshot(force=force, timeframe=tf, server_id=sid)})
        if path == "/api/settings":
            return self._json({"ok": True, "settings": STORE.settings(),
                               "servers": STORE.public_servers()})
        if path == "/api/notify":
            return self._json({"ok": True, "notify": self._notify_meta()})
        if path == "/api/notify/log":
            limit = 0
            try:
                limit = int((query.get("limit") or ["0"])[0])
            except (TypeError, ValueError):
                limit = 0
            return self._json(self._notify_log(limit))
        if path == "/api/diagnose":
            sid = (query.get("server") or [None])[0]
            return self._json({"ok": True, "result": self._diagnose(sid)})
        if path == "/api/log":
            n = int((query.get("lines") or ["200"])[0])
            return self._json({"ok": True, "lines": STORE.tail_log(n)})
        if path == "/api/update/check":
            force = (query.get("force") or ["0"])[0] in ("1", "true")
            return self._json(self._update_check(force))
        if path == "/api/update/status":
            return self._json(self._update_status())
        if path == "/api/storage":
            return self._json(self._storage_report())
        if path == "/api/backup/status":
            return self._json(self._backup_status())
        if path == "/api/backup/export":
            return self._backup_export()
        return self._json({"ok": False, "error": "not found"}, 404)

    # ---------------- feature helpers ----------------

    def _notify_meta(self):
        """Everything the notification section needs, in the shape notify.py
        already defines.

        The module stores a channel as {id, name, type, enabled, config:{...}}
        and knows which of those config fields are secret. Reusing its own
        type_meta/event list here means adding a channel or an event stays a
        one-file change in notify.py.
        """
        cfg = STORE.settings()
        events_cfg = cfg.get("notify_events") or {}
        if notify_mod is None:
            return {"available": False, "types": {}, "channels": [],
                    "events": events_cfg, "event_list": []}
        channels = [notify_mod.channel_public(c)
                    for c in (cfg.get("notify_channels") or []) if isinstance(c, dict)]
        return {
            "available": True,
            "types": notify_mod.type_meta(STORE.settings().get("lang") or "zh"),
            "channels": channels,
            "events": events_cfg,
            "event_list": notify_mod.event_list(STORE.settings().get("lang") or "zh"),
        }

    def _notify_channel(self, body):
        """Create or update one channel.

        The browser only ever holds a masked copy, so a blank or masked secret
        must leave the stored value alone. notify.merge_config() owns that rule;
        the panel just feeds it the stored list and the incoming entry keyed by
        id, which is also what keeps a partial edit from wiping other channels.
        """
        if notify_mod is None:
            return {"ok": False, "error": "通知模块不可用"}
        cfg = STORE.settings()
        stored = [c for c in (cfg.get("notify_channels") or []) if isinstance(c, dict)]
        one = dict(body or {})
        if not str(one.get("id") or "").strip():
            one["id"] = "ch%d" % int(time.time() * 1000)
        patch = {"notify_channels": [one]}
        merged = notify_mod.merge_config({"notify_channels": stored}, patch)
        channels = merged.get("notify_channels") or []
        STORE.update_raw("notify_channels", channels)
        return {"ok": True, "notify": self._notify_meta()}

    def _notify_delete(self, body):
        cid = str(body.get("id") or "").strip()
        cfg = STORE.settings()
        channels = [c for c in (cfg.get("notify_channels") or [])
                    if str((c or {}).get("id")) != cid]
        STORE.update_raw("notify_channels", channels)
        return {"ok": True, "notify": self._notify_meta()}

    # ---------------- delivery records ----------------

    def _notify_log(self, limit=0):
        """Recent push attempts, newest first.

        Returns {ok, records, total, max}: total is how many are stored, records
        is the (optionally limited) page the browser asked for.
        """
        empty = {"ok": True, "records": [], "total": 0, "max": 0}
        if notifier_mod is None:
            empty["error"] = "通知模块不可用"
            return empty
        try:
            recs = notifier_mod.load_notify_log(var_dir())
        except Exception as e:
            log("notify log read failed: %r" % (e,))
            empty["error"] = str(e)
            return empty
        total = len(recs)
        if limit and limit > 0:
            recs = recs[:limit]
        return {"ok": True, "records": recs, "total": total,
                "max": notifier_mod.NOTIFY_LOG_MAX}

    def _notify_log_clear(self):
        if notifier_mod is None:
            return {"ok": False, "error": "通知模块不可用"}
        try:
            ok = notifier_mod.clear_notify_log(var_dir())
        except Exception as e:
            log("notify log clear failed: %r" % (e,))
            return {"ok": False, "error": str(e)}
        return {"ok": bool(ok), "removed": True}

    def _notify_test(self, body):
        """Send the canned test message through one channel.

        The stored record is preferred over whatever the browser sent, because
        the browser only has the mask for secret fields.
        """
        if notify_mod is None:
            return {"ok": False, "error": "通知模块不可用"}
        cfg = STORE.settings()
        lang = cfg.get("lang") or "zh"
        cid = str(body.get("id") or "").strip()
        chosen = None
        for ch in cfg.get("notify_channels") or []:
            if isinstance(ch, dict) and str(ch.get("id")) == cid:
                chosen = ch
                break
        if chosen is None:
            if not isinstance(body, dict) or not body.get("type"):
                return {"ok": False, "error": "找不到这个通知渠道"}
            chosen = body
        title, text = notify_mod.format_test(chosen.get("name") or "", lang)
        ok, detail = notify_mod.send(chosen, title, text)
        # A test is a delivery like any other: it belongs in the same history, or
        # "the test worked but the alert never arrived" stays unexplainable.
        if notifier_mod is not None:
            try:
                notifier_mod.record_delivery(var_dir(), chosen, title, text,
                                             "test", bool(ok), detail)
            except Exception as e:  # noqa: BLE001
                log("test delivery record failed: %r" % (e,))
        return {"ok": bool(ok), "detail": detail}
    def _update_cached(self):
        """The last update answer, WITHOUT ever hitting the network.

        /api/bootstrap runs on every page load, and update.check() synchronously
        tries three layers with an 8s timeout each. Measured on a box whose
        outbound internet is blocked: the very first paint blocks for 16.5s, and
        it blocks again every time the 60s failure cooldown expires - i.e. every
        minute, forever. A monitor must never stall its own UI on a question as
        unimportant as "is there a newer build".

        So bootstrap only ever reports what is already cached, and the background
        poller (and the explicit "check now" button) is what warms it.
        """
        if update_mod is None:
            return {"available": False, "has_update": False, "current": VERSION,
                    "reason": "更新检查模块不可用"}
        try:
            peek = getattr(update_mod, "cached", None)
            if callable(peek):
                res = peek()
                if isinstance(res, dict) and res:
                    if "available" not in res:
                        res["available"] = True
                    return res
        except Exception:
            pass
        # Nothing cached yet: say so honestly rather than making the caller wait.
        return {"available": True, "ok": False, "has_update": False,
                "current": VERSION, "latest": "", "checked": False,
                "reason": "pending", "detail": "还没有检查过，稍后会自动检查",
                "cached": True, "checked_at": 0}

    def _update_check(self, force):
        if update_mod is None:
            return {"available": False, "has_update": False, "current": VERSION,
                    "reason": "更新检查模块不可用"}
        try:
            cfg = STORE.settings()
            res = update_mod.check(
                VERSION, force=force,
                repo=cfg.get("update_repo") or update_mod.DEFAULT_REPO,
                api_base=cfg.get("update_api_base") or "",
                web_base=cfg.get("update_web_base") or "",
                log=log)
            if isinstance(res, dict) and "available" not in res:
                res["available"] = True
            return res
        except Exception as e:
            return {"available": False, "has_update": False, "current": VERSION,
                    "reason": str(e)}

    def _guest_action(self, body):
        """Start / stop / shutdown / reboot one guest.

        These are the only write operations the panel performs, and they exist
        because "is it up" and "can I bounce it" are the same question when you
        are away from a desk. Each is a single POST to the matching PVE
        endpoint; the caller gets the task UPID back so it can be watched.
        """
        action = str(body.get("action") or "").strip().lower()
        vmid = body.get("vmid")
        node = str(body.get("node") or "").strip()
        kind = str(body.get("type") or "qemu").strip().lower()
        sid = body.get("server")
        if action not in ("start", "stop", "shutdown", "reboot"):
            return {"ok": False, "error": "不支持的操作：%s" % action}
        if not node or not vmid or kind not in ("qemu", "lxc"):
            return {"ok": False, "error": "缺少 node / vmid / type"}
        rec = STORE.server(sid) if sid else STORE.active_server()
        if rec is None:
            return {"ok": False, "error": "未配置服务器"}
        st = COLLECT.state(rec)
        try:
            c = st.client_for()
            path = "/api2/json/nodes/%s/%s/%s/status/%s" % (node, kind, vmid, action)
            upid = c.request_method("POST", path)
        except pveclient.PVEUnauthorized as e:
            COLLECT.invalidate(rec.get("id"))
            return {"ok": False, "error": str(e)}
        except pveclient.PVEPermission as e:
            return {"ok": False, "error": "权限不足：%s" % e}
        except pveclient.PVEError as e:
            return {"ok": False, "error": str(e)}
        log("[%s] guest %s/%s %s -> %s" % (rec.get("id"), kind, vmid, action, upid))
        # Drop the cache so the automatic refresh after the action reads PVE
        # rather than replaying what was cached a second earlier.
        st.reset_cache()
        return {"ok": True, "upid": upid, "action": action}

    def _diagnose(self, sid):
        """Split "can I reach it", "is it really PVE", "is the token good" and
        "are the ACLs right" into separate answers - 401 and 403 need different
        fixes and one generic failure message helps nobody."""
        out = {"steps": []}
        rec = STORE.server(sid) if sid else STORE.active_server()
        if rec is None:
            out["steps"].append({"name": "服务器", "ok": False, "detail": "尚未配置任何 PVE 服务器"})
            return out
        out["steps"].append({"name": "服务器", "ok": True,
                             "detail": "%s（%s:%s）" % (rec.get("name") or "-",
                                                        rec.get("host"), rec.get("port"))})
        client = pveclient.PVEClient(
            host=rec.get("host"), port=rec.get("port"), token=rec.get("token") or "",
            verify=bool(rec.get("verify_tls")), ca_pem=rec.get("ca_pem") or "", timeout=10.0)
        try:
            fp = client.fingerprint()
            out["steps"].append({
                "name": "连通性", "ok": True,
                "detail": "%s:%s 可达，%d ms，服务器报 PVE %s（节点 %s）" % (
                    rec.get("host"), rec.get("port"), fp["latency_ms"],
                    fp["version"] or "未知", fp["node"] or "未知"),
            })
        except pveclient.PVEError as e:
            out["steps"].append({"name": "连通性", "ok": False, "detail": str(e)})
            return out
        try:
            pr = client.probe()
            out["steps"].append({"name": "认证域", "ok": True,
                                 "detail": "可访问认证域：%s（%d ms）" % (
                                     ", ".join(pr["domains"]) or "无", pr["latency_ms"])})
        except pveclient.PVEError as e:
            out["steps"].append({"name": "认证域", "ok": False, "detail": str(e)})
        if not rec.get("token"):
            out["steps"].append({"name": "Token", "ok": False, "detail": "尚未填写 Token"})
            return out
        try:
            v = client.request("/api2/json/version")
            out["steps"].append({"name": "Token", "ok": True,
                                 "detail": "认证通过，PVE %s（release %s）" % (
                                     (v or {}).get("version"), (v or {}).get("release"))})
        except pveclient.PVEUnauthorized as e:
            out["steps"].append({"name": "Token", "ok": False, "detail": str(e)})
            try:
                client.close()
            except Exception:
                pass
            return out
        except pveclient.PVEError as e:
            out["steps"].append({"name": "Token", "ok": False, "detail": str(e)})
            try:
                client.close()
            except Exception:
                pass
            return out
        for name, path in (("集群审计", "/api2/json/cluster/status"),
                           ("节点审计", "/api2/json/nodes"),
                           ("虚拟机列表", "/api2/json/cluster/resources")):
            try:
                client.request(path)
                out["steps"].append({"name": name, "ok": True, "detail": "通过"})
            except pveclient.PVEPermission as e:
                out["steps"].append({"name": name, "ok": False, "detail": str(e),
                                     "hint": "PVEAuditor 必须授在 / 并勾选「继承」"})
            except pveclient.PVEError as e:
                out["steps"].append({"name": name, "ok": False, "detail": str(e)})
        try:
            client.close()
        except Exception:
            pass
        return out


    # ---------------- update: one-click install ----------------

    def _update_status(self):
        if installer_mod is None:
            return {"ok": False, "phase": "idle", "current": VERSION,
                    "error": "更新模块不可用"}
        try:
            st = installer_mod.status()
        except Exception as e:
            return {"ok": False, "phase": "idle", "current": VERSION, "error": str(e)}
        st = dict(st or {})
        st["ok"] = True
        st["current"] = VERSION
        return st

    def _update_install(self):
        """Download and install the newest release.

        Everything that can fail before the service is touched (no update, no
        fpk asset, bad package) is answered synchronously so the browser gets a
        real error. The install itself is detached on purpose: it stops the
        very service that would be waiting for it.
        """
        if installer_mod is None:
            return {"ok": False, "error": "更新模块不可用"}
        info = self._update_check(True)
        if not info.get("ok"):
            return {"ok": False, "error": info.get("detail") or info.get("error")
                    or "无法获取版本信息"}
        if not info.get("has_update"):
            return {"ok": False, "error": "当前已是最新版本", "latest": info.get("latest")}
        try:
            return installer_mod.start(info, VERSION, pick_port(), var_dir(), log)
        except Exception as e:
            log("one-click update failed to start: %r" % (e,))
            return {"ok": False, "error": str(e)}

    # ---------------- server test ----------------

    def _server_test(self, body):
        """Probe one stored endpoint without making it the active one.

        The settings page has to answer "is this host reachable and is that
        token right" for a server you are not currently viewing; switching to it
        just to find out would change what every other page shows.
        """
        sid = str((body or {}).get("id") or "").strip()
        rec = STORE.server(sid) if sid else None
        if rec is None:
            return {"ok": False, "error": "找不到这台服务器"}
        client = pveclient.PVEClient(
            host=rec.get("host"), port=rec.get("port"), token=rec.get("token") or "",
            verify=bool(rec.get("verify_tls")), ca_pem=rec.get("ca_pem") or "", timeout=10.0)
        try:
            fp = client.fingerprint()
            if not rec.get("token"):
                return {"ok": False, "error": "尚未填写 Token",
                        "detail": "可达，PVE %s" % (fp.get("version") or "?")}
            client.request("/api2/json/version")
            return {"ok": True, "detail": "PVE %s" % (fp.get("version") or "?"),
                    "latency_ms": fp.get("latency_ms")}
        except pveclient.PVEUnauthorized as e:
            return {"ok": False, "error": "Token 无效：%s" % e}
        except pveclient.PVEPermission as e:
            return {"ok": False, "error": "权限不足：%s" % e}
        except pveclient.PVEError as e:
            return {"ok": False, "error": str(e)}
        finally:
            try:
                client.close()
            except Exception:
                pass

    # ---------------- maintenance: configuration backup ----------------

    def _backup_status(self):
        if maint_mod is None:
            return {"ok": False, "error": "维护模块不可用", "backups": [],
                    "directory": "", "enabled": False}
        try:
            st = maint_mod.status(var_dir(), STORE.settings())
        except Exception as e:
            log("backup status failed: %r" % (e,))
            return {"ok": False, "error": str(e), "backups": [], "directory": "",
                    "enabled": False}
        st = dict(st or {})
        st["ok"] = True
        return st

    # ---------------- storage ----------------

    def _storage_report(self):
        """Disk usage of the panel's own folders, plus the package cache.

        Reports only - the numbers are a diagnostic, so an unreadable directory
        costs its own row, never the page.
        """
        if storage_mod is None:
            return {"ok": False, "error": "存储模块不可用", "items": [],
                    "total": 0, "var_dir": var_dir(), "updates": [],
                    "updates_keep": 2, "current_version": VERSION}
        try:
            data = storage_mod.report(var_dir(), VERSION)
        except Exception as e:
            log("storage report failed: %r" % (e,))
            return {"ok": False, "error": str(e), "items": [], "total": 0,
                    "var_dir": var_dir(), "updates": [], "updates_keep": 2,
                    "current_version": VERSION}
        out = dict(data or {})
        out["ok"] = True
        return out

    def _storage_cleanup(self, body):
        """One cleanup pass over the package cache and/or the logs.

        The keep count comes from the report so the page and the automatic pass
        cannot disagree about how many packages are worth keeping. Answers 409
        while an update is downloading or installing: the .part file in that
        folder is the transfer in progress, not a leftover.
        """
        if storage_mod is None:
            return {"ok": False, "error": "存储模块不可用"}
        body = body if isinstance(body, dict) else {}
        target = str(body.get("target") or "updates").strip().lower()
        if target not in ("updates", "logs", "all"):
            return {"ok": False, "error": "不支持的清理目标：%s" % (target or "(空)")}
        try:
            keep = int(body.get("keep") or storage_mod.UPDATES_KEEP)
        except (TypeError, ValueError):
            keep = storage_mod.UPDATES_KEEP
        try:
            res = storage_mod.cleanup(var_dir(), target=target, keep=keep,
                                      protect_version=VERSION, log=log)
        except Exception as e:
            log("storage cleanup failed: %r" % (e,))
            return {"ok": False, "error": str(e)}
        if not res.get("ok"):
            if res.get("busy"):
                # 409: the request was valid, the state is not. Retrying later works.
                return self._json({"ok": False, "busy": True,
                                   "error": res.get("error") or "更新正在进行"}, 409)
            return {"ok": False, "error": res.get("error") or "清理失败"}
        res["ok"] = True
        res["report"] = self._storage_report()
        return res

    def _backup_export(self):
        """Stream the configuration as a downloadable file.

        The download leaves this device, so it is exported **redacted**: the
        tokens stay behind. The on-device automatic backups are the ones that
        keep them, because a restore that needed every token retyped would not
        be a backup.

        octet-stream, not json: the Android WebView behind the fnOS phone app
        consults the MIME type before deciding a response is a download, and a
        json body gets rendered instead of saved even with the attachment
        disposition.
        """
        if maint_mod is None:
            return self._json({"ok": False, "error": "维护模块不可用"}, 500)
        try:
            data = maint_mod.make_backup(backup_state, VERSION, kind="manual",
                                         include_secrets=False)
            body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            name = maint_mod.backup_filename(data.get("created_at"))
        except Exception as e:
            log("backup export failed: %r" % (e,))
            return self._json({"ok": False, "error": str(e)}, 500)
        return self._send(200, body, "application/octet-stream", {
            "Content-Disposition": 'attachment; filename="%s"' % name,
        })

    def _backup_to_folder(self):
        if maint_mod is None:
            return {"ok": False, "error": "维护模块不可用"}
        try:
            data = maint_mod.make_backup(backup_state, VERSION, kind="manual")
            path = maint_mod.write_backup(var_dir(), data)
            return {"ok": True, "path": path}
        except Exception as e:
            log("backup to folder failed: %r" % (e,))
            return {"ok": False, "error": str(e)}

    def _backup_run(self):
        if maint_mod is None:
            return {"ok": False, "error": "维护模块不可用"}
        try:
            res = maint_mod.run_auto(var_dir(), STORE.settings(), backup_state,
                                     log, force=True)
            return dict(res or {})
        except Exception as e:
            log("manual backup failed: %r" % (e,))
            return {"ok": False, "error": str(e)}

    # Machine-readable reasons from maint.parse_backup, mapped to sentences the
    # settings page can show as-is.
    BACKUP_REASON = {
        "unreadable": "文件不是有效的备份（无法解析）",
        "foreign": "这不是 PVEpanel 的备份文件",
        "format": "备份格式版本不受支持",
        "empty": "备份里没有可恢复的服务器列表",
    }

    def _restore_blob(self, raw):
        """Apply a parsed backup. Shared by import and restore-from-folder."""
        data = maint_mod.parse_backup(raw)
        payload = data.get("payload") or {}
        config = payload.get("config") or {}
        servers = payload.get("servers") or []
        n = STORE.replace_all(config, servers)
        COLLECT.invalidate()
        # notify.py has no module-level cache to clear (it reads the channel
        # record passed to send() every time), so there is nothing to reset here.
        # The old code called notify_mod.reset_cache() inside a bare try/except,
        # which meant a non-existent method silently did nothing - dead code that
        # looked like a safeguard.
        log("configuration restored from backup: %d servers" % n)
        return {"ok": True, "restored": {"servers": n,
                                         "masked": not data.get("contains_secrets", True)}}

    def _backup_import(self, raw):
        if maint_mod is None:
            return {"ok": False, "error": "维护模块不可用"}
        if not raw:
            return {"ok": False, "error": "备份内容为空"}
        try:
            return self._restore_blob(raw)
        except ValueError as e:
            return {"ok": False, "error": self.BACKUP_REASON.get(str(e), str(e))}
        except Exception as e:
            log("backup import failed: %r" % (e,))
            return {"ok": False, "error": str(e)}

    def _backup_restore(self, body):
        if maint_mod is None:
            return {"ok": False, "error": "维护模块不可用"}
        name = os.path.basename(str((body or {}).get("name") or ""))
        if not name:
            return {"ok": False, "error": "缺少备份文件名"}
        try:
            folder = maint_mod.backup_dir(var_dir())
            target = os.path.join(folder, name)
            if not os.path.isfile(target):
                return {"ok": False, "error": "找不到这个备份文件"}
            with open(target, "rb") as f:
                raw = f.read()
            return self._restore_blob(raw)
        except ValueError as e:
            return {"ok": False, "error": self.BACKUP_REASON.get(str(e), str(e))}
        except Exception as e:
            log("backup restore failed: %r" % (e,))
            return {"ok": False, "error": str(e)}

    def _backup_delete(self, body):
        if maint_mod is None:
            return {"ok": False, "error": "维护模块不可用"}
        name = os.path.basename(str((body or {}).get("name") or ""))
        if not name:
            return {"ok": False, "error": "缺少备份文件名"}
        try:
            folder = maint_mod.backup_dir(var_dir())
            target = os.path.join(folder, name)
            if not os.path.isfile(target):
                return {"ok": False, "error": "找不到这个备份文件"}
            os.remove(target)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        root = web_dir()
        target = os.path.abspath(os.path.join(root, path.lstrip("/")))
        if not target.startswith(root + os.sep) and target != root:
            return self._send(400, "bad path", "text/plain; charset=utf-8")
        if not os.path.isfile(target):
            target = os.path.join(root, "index.html")
            if not os.path.isfile(target):
                return self._send(404, "not found", "text/plain; charset=utf-8")
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as f:
            data = f.read()
        self._send(200, data, MIME.get(ext, "application/octet-stream"))


SETTINGS_LOCK = threading.Lock()


def pick_port():
    """Which port to listen on.

    cmd/main decides the port (a fresh random one on first start, remembered in
    @appdata so it never moves) and passes it in PVEPANEL_PORT. The other names
    are accepted so the service still works when started by hand.

    There is deliberately no 8899 default: that port is commonly taken, and
    silently listening somewhere unexpected is worse than failing to start.
    """
    for key in ("PVEPANEL_PORT", "BACKEND_PORT", "PORT"):
        v = os.environ.get(key)
        if v and v.strip().isdigit():
            port = int(v.strip())
            if 1 <= port <= 65535:
                return port
    return int(os.environ.get("PVEPANEL_DEFAULT_PORT") or 0) or _free_port()


def _free_port():
    """Pick an unused local port when nothing told us which one to use."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def pick_host():
    v = os.environ.get("PVEPANEL_LISTEN")
    return v.strip() if v else "127.0.0.1"


def start_background_threads():
    """Background work that has to happen whether or not a browser is open.

    Two independent loops: the update check (a NAS panel should notice a new
    release on its own) and the automatic configuration backup. Each owns its
    own stop event and its loop body swallows its own exceptions - a thread that
    dies on a transient network error is a feature that silently stops working.
    """
    if installer_mod is not None:
        # status() is polled straight from an HTTP handler, so it needs to know
        # where the update directory is without being told on every call.
        try:
            installer_mod.set_context(var_dir=var_dir(), current=VERSION)
        except Exception:
            pass

    if update_mod is not None:
        stop_u = threading.Event()

        def loop_update():
            update_mod.poll_loop(STORE.settings, log, stop_u,
                                 initial_delay=90, interval=3600)

        threading.Thread(target=loop_update, name="update-poll", daemon=True).start()

    if maint_mod is not None:
        stop_m = threading.Event()

        def loop_backup():
            maint_mod.auto_loop(var_dir(), STORE.settings, STORE.update,
                                backup_state, log, stop_m, tick=60)

        threading.Thread(target=loop_backup, name="auto-backup", daemon=True).start()

    if storage_mod is not None:
        # The package cache is trimmed once per start, in the background.
        #
        # Every start is the right moment, and after an upgrade it is the only
        # moment: the update flow is uninstall -> install -> start, so the
        # process that would do the deleting is replaced by the very install it
        # is waiting for. Whoever runs afterwards is the first code that can
        # safely touch that folder. A plain restart gets the same treatment for
        # free and clears whatever accumulated in the meantime.
        def purge_once():
            try:
                storage_mod.set_logger(log)
                res = storage_mod.startup_purge(var_dir(), VERSION)
                if res.get("skipped"):
                    log("storage: startup cleanup skipped (%s)" % res.get("skipped"))
            except Exception as exc:  # noqa: BLE001 - a cleanup must never block a start
                log("storage: startup cleanup failed: %r" % (exc,))

        threading.Thread(target=purge_once, name="storage-purge", daemon=True).start()


def main():
    port = pick_port()
    host = pick_host()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.allow_reuse_address = True
    log("pvepanel %s listening on %s:%d (web=%s var=%s)" % (VERSION, host, port, web_dir(), var_dir()))
    start_background_threads()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()