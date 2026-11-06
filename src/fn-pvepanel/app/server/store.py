#!/usr/bin/env python3
"""store.py - settings, the server list, and every API token.

Three things live on disk and they are deliberately split:

  config.json   general settings - refresh cadence, thresholds, UI preferences.
                Safe to read; contains no secrets.
  servers.json  the list of PVE endpoints plus their tokens. Mode 600, root
                only. fnOS has no usable keyring (`keyring` and `secretstorage`
                are not installed and there is no `secret-tool`), so a root-only
                file is the honest answer - and on this box @appdata is already
                770 root:root, which is what every other application does.

Two invariants that cost real incidents to learn:

  * Writes go to a temp file and are renamed into place, so a crash mid-write
    cannot leave a half-written file that makes the panel look unconfigured.
  * A token equal to SECRET_MARK (or to an empty string) means "keep the one you
    already have". The browser only ever sees the mask, so echoing it back must
    never be read as a new value. Skipping this check once overwrote a live
    token with the literal string and could not be undone.
"""

import json
import os
import threading
import time
import uuid

SECRET_MARK = "__SET__"

DEFAULTS = {
    "refresh": 10,
    "timeframe": "hour",
    "storage_warn": 85,
    "temp_warn": 55,
    "temp_crit": 65,
    "theme": "auto",
    "lang": "zh",
    "active": "",
    "notify_enabled": False,
    "notify_channels": [],
    "notify_events": {
        "node_offline": True,
        "storage_full": True,
        "disk_temp": True,
        "disk_health": True,
        "backup_failed": True,
        "backup_missing": True,
        "guest_stopped": False,
        "load_high": True,
        "mem_high": True,
        "sub_due": True,
        "auth_failed": True,
        "update_available": True,
    },
    "load_warn": 0.85,
    "mem_warn": 90,
    "update_check": True,
    # Overridable so the three-layer release lookup can be pointed at a mirror
    # or at a test double; empty means "use the built-in defaults".
    "update_repo": "",
    "update_api_base": "",
    "update_web_base": "",
    # Maintenance: automatic configuration backups live entirely on this device.
    # The names match maint.auto_settings(), which owns the range clamping.
    "auto_backup_enabled": False,
    "auto_backup_interval_hours": 24,
    "auto_backup_retention_days": 7,
}

SERVER_DEFAULTS = {
    "id": "",
    "name": "",
    "host": "",
    "port": 8006,
    "token": "",
    "verify_tls": False,
    "ca_pem": "",
    "enabled": True,
}


class Store:
    def __init__(self, var_dir, log):
        self.var_dir = var_dir
        self.log = log
        self._lock = threading.RLock()
        try:
            os.makedirs(var_dir, exist_ok=True)
        except OSError:
            pass
        self._cfg_path = os.path.join(var_dir, "config.json")
        self._srv_path = os.path.join(var_dir, "servers.json")
        self._cfg = self._read_json(self._cfg_path, {})
        raw = self._read_json(self._srv_path, {})
        self._servers = raw.get("servers") if isinstance(raw.get("servers"), list) else []
        self._servers = [s for s in self._servers if isinstance(s, dict) and s.get("id")]
        self._migrate_legacy()

    def _migrate_legacy(self):
        """Carry a pre-multi-server config over instead of dropping it.

        Older builds kept one endpoint in config.json (pve_host/pve_port) and the
        token in token.json. Anyone upgrading would otherwise find the panel
        blank and have to type the token again.
        """
        if self._servers:
            return
        host = (self._cfg.get("pve_host") or "").strip()
        if not host:
            return
        tok = ""
        legacy = self._read_json(os.path.join(self.var_dir, "token.json"), {})
        if isinstance(legacy, dict):
            tok = (legacy.get("pve_token") or "").strip()
        rec = dict(SERVER_DEFAULTS)
        rec.update({
            "id": "srv-legacy",
            "name": host,
            "host": host,
            "port": int(self._cfg.get("pve_port") or 8006),
            "token": tok,
            "verify_tls": bool(self._cfg.get("verify_tls")),
            "ca_pem": self._cfg.get("ca_pem") or "",
        })
        self._servers = [rec]
        self._save_servers()
        self._cfg["active"] = rec["id"]
        self._write_json(self._cfg_path, self._cfg, 0o600)
        self.log("migrated legacy single-server config to %s" % host)

    # ---------------- io ----------------

    def _read_json(self, path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                val = json.load(f)
            return val if isinstance(val, dict) else default
        except (OSError, ValueError):
            return default

    def _write_json(self, path, data, mode=0o600):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass

    # ---------------- settings ----------------

    def settings(self):
        with self._lock:
            out = dict(DEFAULTS)
            out.update({k: v for k, v in (self._cfg or {}).items() if k in DEFAULTS})
            # nested dicts need the defaults merged under the stored values
            for key in ("notify_events",):
                merged = dict(DEFAULTS[key])
                merged.update(self._cfg.get(key) or {})
                out[key] = merged
            return out

    def update(self, patch):
        """Merge a partial settings patch; SECRET_MARK means "leave it alone"."""
        if not isinstance(patch, dict):
            return self.settings()
        with self._lock:
            for k, v in patch.items():
                if k not in DEFAULTS or k == "notify_channels":
                    continue
                if k == "notify_events":
                    if isinstance(v, dict):
                        merged = dict(self._cfg.get("notify_events") or {})
                        for ek, ev in v.items():
                            if ek in DEFAULTS["notify_events"]:
                                merged[ek] = bool(ev)
                        self._cfg["notify_events"] = merged
                    continue
                if k in ("refresh", "timeframe"):
                    if k == "refresh":
                        try:
                            v = max(5, int(v))
                        except (TypeError, ValueError):
                            continue
                    else:
                        if v not in ("hour", "day", "week", "month", "year", "decade"):
                            continue
                elif k in ("storage_warn", "temp_warn", "temp_crit", "load_warn", "mem_warn"):
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        continue
                elif k in ("notify_enabled", "update_check", "auto_backup_enabled"):
                    v = bool(v)
                elif k in ("auto_backup_interval_hours", "auto_backup_retention_days"):
                    try:
                        v = max(1, int(v))
                    except (TypeError, ValueError):
                        continue
                elif k in ("theme", "lang", "active", "update_repo",
                           "update_api_base", "update_web_base"):
                    v = str(v or "").strip()
                    v = str(v)
                self._cfg[k] = v
            self._cfg["updated_at"] = int(time.time())
            self._write_json(self._cfg_path, self._cfg, 0o600)
        return self.settings()

    def update_raw(self, key, value):
        """Set one non-scalar setting (the channel list) without going through
        the scalar validation path in update()."""
        with self._lock:
            self._cfg[key] = value
            self._cfg["updated_at"] = int(time.time())
            self._write_json(self._cfg_path, self._cfg, 0o600)
        return self.settings()

    # ---------------- servers ----------------

    def _save_servers(self):
        self._write_json(self._srv_path, {"servers": self._servers}, 0o600)

    def servers(self):
        """Full records, tokens included. Never hand this to the browser."""
        with self._lock:
            return [dict(s) for s in self._servers]

    def public_servers(self):
        out = []
        for s in self.servers():
            item = dict(SERVER_DEFAULTS)
            item.update({k: s.get(k) for k in SERVER_DEFAULTS if k in s})
            item["token"] = SECRET_MARK if s.get("token") else ""
            item["token_set"] = bool(s.get("token"))
            item["id"] = s.get("id")
            out.append(item)
        return out

    def server(self, sid):
        for s in self.servers():
            if s.get("id") == sid:
                return s
        return None

    def active_server(self):
        cfg = self.settings()
        sid = cfg.get("active")
        srv = self.server(sid) if sid else None
        if srv:
            return srv
        all_ = self.servers()
        return all_[0] if all_ else None

    def upsert_server(self, patch):
        """Create or update one server. Returns the masked record.

        A patch without an id appends; one with a known id updates in place. The
        token field follows the same rule as the panel token used to: an empty
        value or the mask keeps whatever is already stored.
        """
        if not isinstance(patch, dict):
            return None
        with self._lock:
            sid = str(patch.get("id") or "").strip()
            rec = None
            if sid:
                for s in self._servers:
                    if s.get("id") == sid:
                        rec = s
                        break
            if rec is None:
                if not str(patch.get("host") or "").strip():
                    return None
                rec = dict(SERVER_DEFAULTS)
                rec["id"] = uuid.uuid4().hex[:8]
                self._servers.append(rec)

            for key in ("name", "host", "ca_pem"):
                if key in patch:
                    rec[key] = str(patch.get(key) or "").strip()
            if "port" in patch:
                try:
                    rec["port"] = int(patch.get("port") or 8006)
                except (TypeError, ValueError):
                    pass
            if "verify_tls" in patch:
                rec["verify_tls"] = bool(patch.get("verify_tls"))
            if "enabled" in patch:
                rec["enabled"] = bool(patch.get("enabled"))
            tok = patch.get("token")
            if tok is not None:
                tok = str(tok).strip()
                if tok and tok != SECRET_MARK:
                    rec["token"] = tok
            if not rec.get("name"):
                rec["name"] = rec.get("host") or "PVE"
            self._save_servers()
            if not self.settings().get("active"):
                self._cfg["active"] = rec["id"]
                self._write_json(self._cfg_path, self._cfg, 0o600)
        self.log("server %s saved (%s)" % (rec.get("id"), rec.get("host")))
        return next((s for s in self.public_servers() if s["id"] == rec["id"]), None)

    def replace_all(self, config, servers):
        """Overwrite the whole configuration from a backup.

        Restore is a wholesale replacement, not a merge: a backup that only
        merges would leave servers added after the snapshot behind, which is
        exactly what someone restoring is trying to undo. The server list is
        sanitised first so a hand-edited file cannot inject a record without an
        id or a host.
        """
        clean = []
        for s in (servers or []):
            if not isinstance(s, dict) or not str(s.get("host") or "").strip():
                continue
            rec = dict(SERVER_DEFAULTS)
            rec.update({k: s.get(k) for k in SERVER_DEFAULTS if k in s})
            if not rec.get("id"):
                rec["id"] = uuid.uuid4().hex[:8]
            try:
                rec["port"] = int(rec.get("port") or 8006)
            except (TypeError, ValueError):
                rec["port"] = 8006
            rec["name"] = rec.get("name") or rec.get("host")
            clean.append(rec)
        with self._lock:
            self._servers = clean
            self._save_servers()
            new_cfg = dict(self._cfg)
            for k, v in (config or {}).items():
                if k in DEFAULTS and k != "notify_channels":
                    new_cfg[k] = v
            if isinstance((config or {}).get("notify_channels"), list):
                new_cfg["notify_channels"] = config["notify_channels"]
            active = str(new_cfg.get("active") or "")
            if not any(s.get("id") == active for s in clean):
                new_cfg["active"] = clean[0]["id"] if clean else ""
            new_cfg["updated_at"] = int(time.time())
            self._cfg = new_cfg
            self._write_json(self._cfg_path, self._cfg, 0o600)
        self.log("configuration restored (%d servers)" % len(clean))
        return len(clean)

    def delete_server(self, sid):
        with self._lock:
            before = len(self._servers)
            self._servers = [s for s in self._servers if s.get("id") != sid]
            if len(self._servers) == before:
                return False
            self._save_servers()
            if self.settings().get("active") == sid:
                self._cfg["active"] = self._servers[0]["id"] if self._servers else ""
                self._write_json(self._cfg_path, self._cfg, 0o600)
        self.log("server %s deleted" % sid)
        return True

    # ---------------- logs ----------------

    def tail_log(self, lines=200):
        path = os.path.join(self.var_dir, "pvepanel.log")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = f.readlines()
            return [ln.rstrip("\n") for ln in data[-lines:]]
        except OSError:
            return []