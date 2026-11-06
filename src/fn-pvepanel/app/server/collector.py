#!/usr/bin/env python3
"""collector.py - turns a pile of PVE endpoints into one snapshot per server.

Rules that shape this file, each paid for with a measurement:

1. **A dead token must cost one 3-second request, not sixty.** PVE answers every
   failed authentication with HTTP 401 after sitting on the response for a full
   three seconds, and 4xx replies always carry `Connection: close`. Eight
   endpoints in series is 24 seconds; a three-node cluster is 60. So the first
   401 aborts the whole batch, keeps the last good snapshot and flips the panel
   into an explicit "credentials are wrong" state.
2. **Nothing is polled faster than it changes.** pvestatd itself updates every
   10s, RRD is minute-grained and smartctl is expensive, so each family has its
   own TTL and a page hit reuses whatever is still fresh.
3. **Missing fields are hidden, never zeroed.** PVE 7/8 lack PSI and memhost,
   and the schema's `returns` is documentation rather than a whitelist (wearout
   and memhost are returned but undeclared). The UI decides what to draw by
   testing for the key, not by comparing a version number.
"""

import concurrent.futures
import re
import threading
import time

import pveclient

TTL = {
    "fingerprint": 5,
    "cluster": 8,
    "resources": 8,
    "guests": 8,
    "nodes": 8,
    "services": 60,
    "storages": 30,
    "rrd": 60,
    "tasks": 30,
    "subscription": 300,
    "disks": 300,
    "smart": 300,
}

DISK_WARN_TEMP = 55
DISK_CRIT_TEMP = 65


def _due_ts(value):
    """PVE reports nextduedate as "YYYY-MM-DD"; some versions hand back epoch
    seconds. Normalise both, or the expiry reminder silently never fires."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.isdigit():
        try:
            return float(text)
        except ValueError:
            return None
    try:
        import calendar
        import datetime
        return float(calendar.timegm(datetime.datetime.strptime(text, "%Y-%m-%d").timetuple()))
    except (ValueError, TypeError):
        return None


def _parse_temp(smart):
    """Disk temperature has no unified field and differs per drive family.

    ATA exposes attributes[] where id 194 (or a vendor id 190) carries the
    reading as the first token of `raw`. NVMe and SAS only give smartctl's raw
    text, and the column padding differs per firmware, so the number has to come
    out of it with a regex rather than a fixed-width window.
    """
    if not isinstance(smart, dict):
        return None
    if smart.get("type") == "ata":
        for a in smart.get("attributes") or []:
            if not isinstance(a, dict):
                continue
            name = a.get("name") or ""
            if a.get("id") in ("194", "190") or "Temperature" in name:
                raw = (a.get("raw") or "").strip()
                head = raw.split(" ", 1)[0] if raw else ""
                if head.isdigit():
                    return int(head)
    txt = smart.get("text") or ""
    for pat in (r"Temperature\s*:\s*(\d+)",
                r"Drive Temperature\s*:\s*(\d+)",
                r"Current Drive Temperature\s*:\s*(\d+)"):
        m = re.search(pat, txt, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


class ServerState:
    """Everything cached for one PVE endpoint."""

    def __init__(self, rec):
        self.id = rec.get("id")
        self.rec = dict(rec)
        self.cache = {}
        self.lock = threading.Lock()
        self.client = None
        self.client_key = None
        self.auth_failed = False
        self.auth_error = ""
        self.last_good = None
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    def client_for(self):
        rec = self.rec
        key = (rec.get("host"), rec.get("port"), rec.get("token"),
               bool(rec.get("verify_tls")), rec.get("ca_pem") or "")
        if self.client is not None and self.client_key == key:
            return self.client
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = pveclient.PVEClient(
            host=key[0], port=key[1], token=key[2],
            verify=key[3], ca_pem=key[4], timeout=10.0)
        self.client_key = key
        self.auth_failed = False
        self.auth_error = ""
        return self.client

    def reset_cache(self):
        """Drop cached data and the pooled sockets.

        Keeping the sockets after a failure means the next request reuses the
        same dead connections; closing them forces a fresh handshake.
        """
        with self.lock:
            self.cache = {}
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass


class Collector:
    def __init__(self, store, log):
        self.store = store
        self.log = log
        self._states = {}
        self._lock = threading.Lock()

    # ---------------- state plumbing ----------------

    def state(self, rec):
        with self._lock:
            st = self._states.get(rec.get("id"))
            key = (rec.get("host"), rec.get("port"), rec.get("token"),
                   bool(rec.get("verify_tls")), rec.get("ca_pem") or "")
            if st is None:
                st = ServerState(rec)
                self._states[rec["id"]] = st
            elif st.client_key is not None and st.client_key != key:
                st.rec = dict(rec)
                st.reset_cache()
                st.auth_failed = False
            else:
                st.rec = dict(rec)
            return st

    def invalidate(self, sid=None):
        if sid is None:
            with self._lock:
                self._states = {}
            return
        with self._lock:
            self._states.pop(sid, None)

    def auth_failed_since(self):
        """Which servers currently cannot authenticate - the notifier needs it."""
        with self._lock:
            return {sid: st.auth_error for sid, st in self._states.items() if st.auth_failed}

    # ---------------- endpoint fetchers ----------------

    def _cached(self, st, name, fn):
        ttl = TTL.get(name, 30)
        now = time.time()
        hit = st.cache.get(name)
        if hit and now - hit[0] < ttl:
            return hit[1]
        val = fn()
        st.cache[name] = (now, val)
        return val

    def _fetch_cluster(self, c):
        status = c.request("/api2/json/cluster/status") or []
        nodes, quorate, name = [], None, ""
        for item in status:
            t = item.get("type")
            if t == "cluster":
                quorate = item.get("quorate")
                name = item.get("name") or ""
            elif t == "node":
                nodes.append({
                    "node": item.get("name"),
                    "online": (item.get("online") or 0) == 1,
                    "level": item.get("level"),
                    "ip": item.get("ip"),
                })
        return {"nodes": nodes, "quorate": (quorate == 1) if quorate is not None else None,
                "name": name}

    def _fetch_node_status(self, c, node):
        s = c.request("/api2/json/nodes/%s/status" % node) or {}
        mem = s.get("memory") or {}
        swap = s.get("swap") or {}
        root = s.get("rootfs") or {}
        ci = s.get("cpuinfo") or {}
        ker = s.get("current-kernel") or {}
        boot = s.get("boot-info") or {}
        return {
            "node": node, "online": True,
            "cpu": s.get("cpu"),
            "maxcpu": ci.get("cpus"),
            "sockets": ci.get("sockets"),
            "cpumodel": ci.get("model") or "",
            "mem": mem.get("used"), "maxmem": mem.get("total"),
            "memfree": mem.get("free"), "memavailable": mem.get("available"),
            "swapused": swap.get("used"), "swaptotal": swap.get("total"),
            "rootused": root.get("used"), "roottotal": root.get("total"),
            "rootavail": root.get("avail"),
            "uptime": s.get("uptime"),
            "loadavg": s.get("loadavg") or [],
            "wait": s.get("wait"), "idle": s.get("idle"),
            "pveversion": s.get("pveversion"),
            # PVE 8+ gives a structured kernel object; PVE 7 only a bare string.
            "kernel": ker.get("release") or s.get("kversion") or "",
            "bootmode": boot.get("mode") or "",
            "secureboot": boot.get("secureboot"),
            "ksm": (s.get("ksm") or {}).get("shared"),
        }

    def _fetch_resources(self, c):
        """One request that describes every guest, storage and node at once.

        This is the only place live per-guest cpu/mem comes from without an
        N-nodes x M-guests request blowup, so it also is what the dashboard
        aggregate uses."""
        rows = c.request("/api2/json/cluster/resources") or []
        guests, storages = {}, {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            t = r.get("type")
            if t in ("qemu", "lxc"):
                guests[(r.get("node"), r.get("vmid"))] = {
                    "cpu": r.get("cpu"), "maxcpu": r.get("maxcpu"),
                    "mem": r.get("mem"), "maxmem": r.get("maxmem"),
                    "disk": r.get("disk"), "maxdisk": r.get("maxdisk"),
                    "netin": r.get("netin"), "netout": r.get("netout"),
                    "diskread": r.get("diskread"), "diskwrite": r.get("diskwrite"),
                    "uptime": r.get("uptime"), "status": r.get("status"),
                    "hastate": r.get("hastate"), "lock": r.get("lock"),
                    "tags": r.get("tags") or "", "template": bool(r.get("template")),
                    "name": r.get("name"),
                }
            elif t == "storage":
                storages[(r.get("node"), r.get("storage"))] = {
                    "total": r.get("maxdisk"), "used": r.get("disk"),
                    "shared": bool(r.get("shared")), "status": r.get("status"),
                }
        return {"guests": guests, "storages": storages}

    def _fetch_guests(self, c, node):
        out = []
        for kind in ("qemu", "lxc"):
            try:
                rows = c.request("/api2/json/nodes/%s/%s" % (node, kind)) or []
            except pveclient.PVEUnauthorized:
                raise
            except pveclient.PVEError:
                rows = []
            for r in rows:
                out.append({
                    "vmid": r.get("vmid"),
                    "name": r.get("name") or str(r.get("vmid")),
                    "type": kind, "node": node,
                    "status": r.get("status") or "unknown",
                    "uptime": r.get("uptime"),
                    "maxmem": r.get("maxmem"), "maxdisk": r.get("maxdisk"),
                    "maxcpu": r.get("cpus") or r.get("maxcpu"),
                    "tags": r.get("tags") or "",
                    "template": bool(r.get("template")),
                })
        return out

    def _fetch_storages(self, c, node):
        rows = c.request("/api2/json/nodes/%s/storage" % node) or []
        out = []
        for r in rows:
            total = r.get("total") or 0
            used = r.get("used") or 0
            out.append({
                "node": node, "storage": r.get("storage"),
                "type": r.get("type") or "", "content": r.get("content") or "",
                "active": bool(r.get("active")), "enabled": bool(r.get("enabled")),
                "shared": bool(r.get("shared")),
                "total": total, "used": used,
                "avail": r.get("avail") if r.get("avail") is not None else max(0, total - used),
                "frac": (float(used) / total) if total else 0.0,
            })
        return out

    def _fetch_disks(self, c, node):
        rows = c.request("/api2/json/nodes/%s/disks/list" % node) or []
        out = []
        for r in rows:
            out.append({
                "node": node, "devpath": r.get("devpath") or "",
                "model": (r.get("model") or r.get("vendor") or "").strip(),
                "size": r.get("size") or 0, "type": r.get("type") or "",
                "health": r.get("health") or "",
                # wearout is returned by PVE but absent from the declared schema
                "wearout": r.get("wearout"),
                "serial": r.get("serial") or "", "temp": None,
            })
        return out

    def _fetch_tasks(self, c, node):
        rows = c.request("/api2/json/nodes/%s/tasks?limit=40" % node) or []
        out = []
        for r in rows:
            out.append({
                "upid": r.get("upid") or "", "type": r.get("type") or "",
                "id": r.get("id") or "", "node": node,
                "user": r.get("user") or "",
                "starttime": r.get("starttime") or 0,
                "endtime": r.get("endtime") or 0,
                "status": r.get("status") or "",
            })
        return out

    def _fetch_services(self, c, node):
        try:
            rows = c.request("/api2/json/nodes/%s/services" % node) or []
        except pveclient.PVEUnauthorized:
            raise
        except pveclient.PVEError:
            return []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            name = r.get("service") or r.get("name") or ""
            if not name:
                continue
            state = r.get("state") or r.get("active-state") or ""
            out.append({"node": node, "service": name, "state": state,
                        "active": state in ("running", "active", "enabled")})
        return out

    def _rrd(self, st, c, node, timeframe):
        tf = timeframe if timeframe in ("hour", "day", "week", "month", "year", "decade") else "hour"
        key = "rrd:%s:%s" % (node, tf)
        hit = st.cache.get(key)
        if hit and time.time() - hit[0] < TTL["rrd"]:
            return hit[1]
        rows = c.request("/api2/json/nodes/%s/rrddata?timeframe=%s&cf=AVERAGE" % (node, tf)) or []
        pts = []
        for r in rows:
            if not isinstance(r, dict) or r.get("time") is None:
                continue
            cpu = r.get("cpu")
            memused, memtotal = r.get("memused"), r.get("memtotal")
            pts.append({
                "time": r.get("time"),
                "cpu": None if cpu is None else round(float(cpu) * 100, 2),
                "memused": (round(float(memused) / float(memtotal) * 100, 2)
                            if memused is not None and memtotal else None),
                "netin": r.get("netin"), "netout": r.get("netout"),
                "iowait": (round(float(r["iowait"]) * 100, 2)
                           if r.get("iowait") is not None else None),
            })
        st.cache[key] = (time.time(), pts)
        return pts

    # ---------------- snapshot ----------------

    def snapshot(self, rec, timeframe=None, force=False):
        """Collect one server's state.

        `force` means "ignore what is cached": the refresh button and every guest
        action use it, and without it a power action followed by an immediate
        refresh just replays the 8-second-old cache and looks like the action
        did nothing.
        """
        cfg = self.store.settings()
        tf = timeframe or cfg.get("timeframe") or "hour"
        snap = {
            "server": {"id": rec.get("id"), "name": rec.get("name") or rec.get("host"),
                       "host": rec.get("host"), "port": rec.get("port")},
            "timeframe": tf,
            "ok": False, "stale": False, "errors": [], "alerts": [],
            "meta": {"fetched_at": int(time.time()), "auth_failed": False, "configured": True},
        }
        if not rec.get("host"):
            snap["meta"] = {"fetched_at": int(time.time()), "configured": False,
                            "reason": "unconfigured", "auth_failed": False}
            return snap

        st = self.state(rec)
        if force:
            st.reset_cache()
        try:
            c = st.client_for()
        except Exception as e:
            snap["errors"].append(str(e))
            snap["meta"]["reason"] = "client"
            return snap

        # The fingerprint probe is decorative (version + node name) and hits the
        # unauthenticated root page. A transient failure there used to abort the
        # whole snapshot, which the UI renders as "everything vanished"; retry
        # once, and if it still fails keep going - the authenticated endpoints
        # below are the real test of reachability.
        try:
            snap["fingerprint"] = self._cached(st, "fingerprint", c.fingerprint)
        except pveclient.PVEError as e:
            try:
                snap["fingerprint"] = c.fingerprint()
                st.cache["fingerprint"] = (time.time(), snap["fingerprint"])
            except pveclient.PVEError:
                snap["fingerprint"] = {}
                snap["errors"].append("指纹探测：%s" % e)

        try:
            cluster = self._cached(st, "cluster", lambda: self._fetch_cluster(c))
            snap["cluster"] = cluster
        except pveclient.PVEUnauthorized as e:
            return self._stale_copy(st, snap, "auth", str(e))
        except pveclient.PVEError as e:
            snap["errors"].append("集群状态：%s" % e)
            # Reuse the last known node list rather than pretending the cluster
            # is empty; the per-node calls below will fail on their own if the
            # host really is gone, and that is what sets "unreachable".
            prev = st.cache.get("cluster")
            if prev:
                cluster = prev[1]
                snap["stale_nodes"] = True
            else:
                cluster = {"nodes": [], "quorate": None, "name": ""}
            snap["cluster"] = cluster

        node_names = [n.get("node") for n in cluster.get("nodes") or [] if n.get("node")]
        if not node_names:
            node_names = [cfg.get("pve_node") or "pve"]

        try:
            workers = {}
            for n in node_names:
                workers["n:" + n] = st.pool.submit(
                    lambda nn=n: self._fetch_node_status(c, nn))
                workers["g:" + n] = st.pool.submit(
                    lambda nn=n: self._fetch_guests(c, nn))
                workers["s:" + n] = st.pool.submit(
                    lambda nn=n: self._fetch_storages(c, nn))
                workers["t:" + n] = st.pool.submit(
                    lambda nn=n: self._fetch_tasks(c, nn))
                workers["v:" + n] = st.pool.submit(
                    lambda nn=n: self._fetch_services(c, nn))

            nodes, guests, storages, tasks, services = [], [], [], [], []
            for key, fut in workers.items():
                try:
                    val = self._cached(st, key, fut.result)
                except pveclient.PVEUnauthorized:
                    raise
                except pveclient.PVEPermission as e:
                    snap.setdefault("permission_errors", []).append("%s：%s" % (key, e))
                    continue
                except pveclient.PVEError as e:
                    snap["errors"].append("%s：%s" % (key, e))
                    continue
                if key.startswith("n:"):
                    nodes.append(val)
                elif key.startswith("g:"):
                    guests.extend(val)
                elif key.startswith("s:"):
                    storages.extend(val)
                elif key.startswith("t:"):
                    tasks.extend(val)
                elif key.startswith("v:"):
                    services.extend(val)

            snap["nodes"] = nodes
            if node_names and not nodes and not st.cache.get("nodes"):
                # Every node call failed on a fresh collection: the host is
                # down, not merely slow. Without this the response would claim
                # ok=true while carrying no data at all.
                snap["meta"]["reason"] = "unreachable"
                return self._stale_copy(st, snap, "unreachable",
                                        "所有节点查询失败，PVE 可能不可达")
            try:
                res = self._cached(st, "resources", lambda: self._fetch_resources(c))
            except pveclient.PVEUnauthorized:
                raise
            except pveclient.PVEError:
                res = {"guests": {}, "storages": {}}
            for g in guests:
                extra = (res.get("guests") or {}).get((g.get("node"), g.get("vmid")))
                if not extra:
                    continue
                for k, v in extra.items():
                    if v is not None:
                        g[k] = v
            snap["guests"] = sorted(guests, key=lambda g: (g.get("node") or "", g.get("vmid") or 0))
            for s in storages:
                extra = (res.get("storages") or {}).get((s.get("node"), s.get("storage")))
                if extra and not s.get("total") and extra.get("total"):
                    s["total"] = extra["total"]
                    s["used"] = extra.get("used") or 0
                    s["frac"] = float(s["used"]) / s["total"] if s["total"] else 0.0
            snap["storages"] = storages
            snap["tasks"] = sorted(tasks, key=lambda t: t.get("starttime") or 0, reverse=True)
            snap["services"] = services
        except pveclient.PVEUnauthorized as e:
            return self._stale_copy(st, snap, "auth", str(e))

        # Slower families, best-effort. A 403 here means the token lacks that
        # permission (typically Sys.Audit on /) - a configuration issue to
        # surface, not a reason to fail the whole snapshot.
        try:
            snap["rrd"] = self._rrd(st, c, node_names[0], tf)
        except pveclient.PVEPermission as e:
            snap["rrd"] = []
            snap.setdefault("permission_errors", []).append("曲线：%s" % e)
        except pveclient.PVEUnauthorized:
            raise
        except pveclient.PVEError as e:
            snap["rrd"] = []
            snap["errors"].append("曲线：%s" % e)

        for family in ("disks", "smart"):
            try:
                snap[family] = self._slow(st, c, node_names, family)
            except pveclient.PVEPermission as e:
                snap[family] = []
                snap.setdefault("permission_errors", []).append(str(e))
            except pveclient.PVEUnauthorized:
                raise
            except pveclient.PVEError as e:
                snap[family] = []
                snap["errors"].append("%s：%s" % (family, e))

        gaps = self._optional(st, c, "/api2/json/cluster/backup-info/not-backed-up", []) or []
        index = {g.get("vmid"): g for g in snap.get("guests") or []}
        snap["backup_gaps"] = [
            {"vmid": g.get("vmid"),
             "name": (index.get(g.get("vmid")) or {}).get("name") or "",
             "node": (index.get(g.get("vmid")) or {}).get("node") or "",
             "type": g.get("type") or ""}
            for g in gaps if isinstance(g, dict)
        ]

        ha = self._optional(st, c, "/api2/json/cluster/ha/status/current", []) or []
        snap["ha"] = {
            "resources": [x for x in ha if isinstance(x, dict) and x.get("type") == "service"],
            "quorate": next((bool(x.get("quorate")) for x in ha
                             if isinstance(x, dict) and x.get("type") == "quorum"), None),
        }
        snap["subs"] = {}
        for n in node_names:
            sub = self._optional(st, c, "/api2/json/nodes/%s/subscription" % n, {})
            if isinstance(sub, dict) and sub.get("status"):
                snap["subs"][n] = sub

        snap["alerts"] = self._alerts(snap, cfg)
        snap["ok"] = True
        snap["meta"]["fetched_at"] = int(time.time())
        with st.lock:
            st.last_good = snap
        if st.auth_failed:
            st.auth_failed = False
            st.auth_error = ""
        return snap

    def _optional(self, st, c, path, default):
        try:
            return self._cached(st, path, lambda: c.request(path))
        except pveclient.PVEError:
            return default

    def _slow(self, st, c, node_names, family):
        key = family
        hit = st.cache.get(key)
        if hit and time.time() - hit[0] < TTL.get(family, 300):
            return hit[1]
        out = []
        disks = []
        if family == "smart":
            dh = st.cache.get("disks")
            if dh:
                disks = dh[1]
            if not disks:
                # smart needs the device list; fetching it here keeps the two
                # families independent so a disks failure cannot blank SMART.
                for n in node_names:
                    try:
                        disks.extend(self._fetch_disks(c, n))
                    except pveclient.PVEUnauthorized:
                        raise
                    except pveclient.PVEError:
                        continue
        for n in node_names:
            try:
                if family == "disks":
                    out.extend(self._fetch_disks(c, n))
                else:
                    for d in disks:
                        if d.get("node") != n or not d.get("devpath"):
                            continue
                        try:
                            s = c.request("/api2/json/nodes/%s/disks/smart?disk=%s"
                                          % (n, d["devpath"])) or {}
                        except pveclient.PVEUnauthorized:
                            raise
                        except pveclient.PVEPermission:
                            raise
                        except pveclient.PVEError:
                            continue
                        out.append({"node": n, "devpath": d["devpath"],
                                    "health": s.get("health") or "",
                                    "temp": _parse_temp(s),
                                    "type": s.get("type") or "",
                                    "wearout": s.get("wearout")})
            except pveclient.PVEUnauthorized:
                raise
            except pveclient.PVEError:
                continue
        st.cache[key] = (time.time(), out)
        return out

    def _stale_copy(self, st, snap, reason, msg):
        """Return the previous good snapshot flagged as stale.

        A dead token must not wipe the screen: the operator still needs to see
        what the cluster looked like a second ago, with a loud "this is frozen"
        notice. What must never happen is reporting "everything is fine" from an
        empty payload.
        """
        st.auth_failed = True
        st.auth_error = msg
        self.log("[%s] auth failed: %s" % (st.id, msg))
        snap["meta"]["auth_failed"] = True
        snap["meta"]["reason"] = reason
        snap["errors"].append(msg)
        last = st.last_good
        if not last:
            snap["ok"] = False
            return snap
        out = dict(last)
        out["stale"] = True
        out["meta"] = dict(last.get("meta") or {})
        out["meta"]["auth_failed"] = True
        out["meta"]["reason"] = reason
        out["errors"] = list(last.get("errors") or []) + [msg]
        out["alerts"] = list(last.get("alerts") or [])
        return out

    # ---------------- alerts ----------------

    def _alerts(self, snap, cfg):
        """Build the alert list.

        Each entry carries a stable `key` plus the numbers behind it, so the
        browser can render the sentence in whichever language is selected. The
        Chinese title/detail stay as a fallback for log lines and for any client
        without an i18n table, which is why they are still filled in.
        """
        a = []

        def add(level, key, detail, params, title):
            a.append({"level": level, "key": key, "params": params,
                      "title": title, "detail": detail})

        if snap.get("permission_errors"):
            add("warn", "perm",
                "部分数据无法读取，请确认 PVEAuditor 授在 / 并开启 propagate",
                {}, "权限不足")
        thr = float(cfg.get("storage_warn") or 85)
        for n in snap.get("cluster", {}).get("nodes") or []:
            if not n.get("online"):
                add("err", "node_offline", "%s 不在线" % n.get("node"),
                    {"node": n.get("node")}, "节点离线")
        cluster = snap.get("cluster") or {}
        if cluster.get("quorate") is False:
            add("err", "no_quorum", "quorate = 0，写操作会被拒绝", {}, "集群失去仲裁")
        for st in snap.get("storages") or []:
            if st.get("frac") and st["frac"] * 100 >= thr:
                add("warn" if st["frac"] < 0.95 else "err", "storage_high",
                    "%s/%s 已用 %.0f%%" % (st.get("node"), st.get("storage"),
                                             st["frac"] * 100),
                    {"node": st.get("node"), "storage": st.get("storage"),
                     "pct": round(st["frac"] * 100)}, "存储水位")
        for n in snap.get("nodes") or []:
            load = n.get("loadavg") or []
            if load and n.get("maxcpu"):
                try:
                    ratio = float(load[0]) / float(n["maxcpu"])
                except (TypeError, ValueError, ZeroDivisionError):
                    ratio = 0
                if ratio >= float(cfg.get("load_warn") or 0.85):
                    add("warn", "load_high",
                        "%s 1 分钟负载 %s（%d 核）" % (n["node"], load[0], n["maxcpu"]),
                        {"node": n.get("node"), "load": load[0],
                         "cores": n.get("maxcpu")}, "负载偏高")
            if n.get("mem") and n.get("maxmem"):
                pctm = float(n["mem"]) / float(n["maxmem"]) * 100
                if pctm >= float(cfg.get("mem_warn") or 90):
                    add("warn", "mem_high",
                        "%s 已用 %.0f%%" % (n["node"], pctm),
                        {"node": n.get("node"), "pct": round(pctm)}, "内存紧张")
        gaps = snap.get("backup_gaps") or []
        if gaps:
            names = ", ".join((g.get("name") or str(g.get("vmid"))) for g in gaps[:5])
            add("warn", "backup_missing",
                "%d 台未被任何备份任务覆盖：%s" % (len(gaps), names),
                {"count": len(gaps), "names": names}, "虚拟机无备份")
        for t in (snap.get("tasks") or [])[:60]:
            if (t.get("type") == "vzdump" and t.get("status")
                    and not str(t["status"]).startswith("OK")):
                add("err", "backup_failed",
                    "%s 于节点 %s" % (t.get("id"), t.get("node")),
                    {"vmid": t.get("id"), "node": t.get("node")}, "备份任务失败")
                break
        warn_t = float(cfg.get("temp_warn") or DISK_WARN_TEMP)
        crit_t = float(cfg.get("temp_crit") or DISK_CRIT_TEMP)
        for d in snap.get("smart") or []:
            t = d.get("temp")
            if isinstance(t, int):
                if t >= crit_t:
                    add("err", "disk_temp", "%s 达 %d°C" % (d.get("devpath"), t),
                        {"dev": d.get("devpath"), "temp": t}, "磁盘温度过高")
                elif t >= warn_t:
                    add("warn", "disk_temp", "%s 达 %d°C" % (d.get("devpath"), t),
                        {"dev": d.get("devpath"), "temp": t}, "磁盘温度偏高")
            h = (d.get("health") or "").upper()
            if h and h not in ("PASSED", "OK", "UNKNOWN", "SMART DISABLED"):
                add("err", "disk_health", "%s 报告 %s" % (d.get("devpath"), h),
                    {"dev": d.get("devpath"), "health": d.get("health")},
                    "磁盘 SMART 异常")
        now = time.time()
        for node, sub in (snap.get("subs") or {}).items():
            due = _due_ts(sub.get("nextduedate"))
            if due:
                days = int((due - now) / 86400)
                if days <= 30:
                    add("warn" if days > 0 else "err", "sub_due",
                        ("%s 于 %d 天后到期" % (node, days)) if days > 0
                        else ("%s 订阅已过期" % node),
                        {"node": node, "days": days},
                        "订阅即将到期" if days > 0 else "订阅已过期")
        order = {"err": 0, "warn": 1, "info": 2}
        a.sort(key=lambda x: order.get(x.get("level"), 3))
        return a
