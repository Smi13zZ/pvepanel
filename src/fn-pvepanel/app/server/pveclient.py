#!/usr/bin/env python3
"""pveclient.py - minimal Proxmox VE REST client.

Pure standard library. Two behaviours here are not optional, both were measured
against a real PVE 9.2 box:

  * PVE answers *every* failed authentication with HTTP 401 after sitting on the
    response for a full 3 seconds (pve-http-server AnyEvent.pm pads to
    `3 - elapsed`). A doomed credential therefore costs 3s per request, which is
    how a monitoring panel turns into a 60-second white screen. So a 401 is
    raised as its own exception and the caller is expected to stop the whole
    batch rather than walk into the next trap.
  * 4xx replies carry `Connection: close` while 2xx keep the socket alive.
    Reusing a closed socket costs a TLS handshake (~3ms) and sometimes a failed
    read, so the pool drops every connection whose response status is >= 400.

Probing: never probe with an endpoint that needs auth. `GET /` and
`/api2/json/access/domains` are unauthenticated 200s that answer in ~4ms, and
the root page even leaks the PVE version and node name.
"""

import http.client
import json
import re
import socket
import ssl
import threading
import time


class PVEError(Exception):
    """Base class for everything this module raises."""


class PVEUnauthorized(PVEError):
    """HTTP 401 - token missing, wrong, expired, or the user is disabled."""


class PVEPermission(PVEError):
    """HTTP 403 - authenticated but the ACL does not allow this path."""


class PVEUnreachable(PVEError):
    """TCP/TLS/socket level failure."""


def _ssl_context(verify, ca_pem):
    if not verify:
        ctx = ssl._create_unverified_context()
        return ctx
    ctx = ssl.create_default_context()
    if ca_pem:
        ctx.load_verify_locations(cadata=ca_pem)
    return ctx


class _Conn:
    __slots__ = ("conn", "born")

    def __init__(self, conn):
        self.conn = conn
        self.born = time.time()


class PVEClient:
    """Thread-safe connection pool + JSON transport for one PVE endpoint."""

    MAX_IDLE = 6
    IDLE_TTL = 60.0

    def __init__(self, host, token="", port=8006, verify=False, ca_pem="", timeout=10.0):
        self.host = (host or "").strip()
        self.port = int(port or 8006)
        self.token = (token or "").strip()
        self.timeout = float(timeout)
        self._ctx = _ssl_context(verify, ca_pem)
        self._idle = []
        self._lock = threading.Lock()

    # ---------------- transport ----------------

    def _checkout(self):
        """Return (conn, reused).

        `reused` matters: a pooled socket may have been closed by the server
        between requests (pveproxy drops idle keep-alive connections). The
        failure then looks like "Remote end closed connection without response"
        on the *next* request, which is exactly the shape that looks like a
        flaky PVE and is really a stale socket here.
        """
        with self._lock:
            while self._idle:
                c = self._idle.pop()
                if time.time() - c.born < self.IDLE_TTL:
                    return c, True
                try:
                    c.conn.close()
                except Exception:
                    pass
        return _Conn(http.client.HTTPSConnection(
            self.host, self.port, timeout=self.timeout, context=self._ctx)), False

    def _checkin(self, c, reusable):
        if not reusable:
            try:
                c.conn.close()
            except Exception:
                pass
            return
        with self._lock:
            if len(self._idle) < self.MAX_IDLE:
                self._idle.append(c)
                return
        try:
            c.conn.close()
        except Exception:
            pass

    def _headers(self):
        h = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self.token:
            # The documented form is PVEAPIToken=USER@REALM!TOKENID=UUID.
            # Accepting a bare UUID is a convenience so the settings page can
            # take just the secret and fill the rest in for the user.
            tok = self.token
            if "=" not in tok:
                tok = "PVEAPIToken=" + tok
            elif not tok.lower().startswith("pveapitoken"):
                tok = "PVEAPIToken=" + tok
            h["Authorization"] = tok
        return h

    def request(self, path, timeout=None):
        """GET `path`, return the decoded `data` member."""
        return self.request_method("GET", path, timeout=timeout)

    def request_method(self, method, path, body=None, timeout=None):
        """Issue any method. Only guest power actions use this today, and they
        are the one place the panel writes to PVE - so the 401 handling here is
        identical to the read path rather than a second, subtly different one."""
        url = path if path.startswith("/") else "/" + path
        c, reused = self._checkout()
        old_timeout = c.conn.timeout
        if timeout:
            c.conn.timeout = float(timeout)
        reusable = False
        try:
            headers = self._headers()
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            try:
                c.conn.request(method, url, body=payload, headers=headers)
                resp = c.conn.getresponse()
            except (http.client.RemoteDisconnected, http.client.BadStatusLine,
                    ConnectionResetError, BrokenPipeError):
                # A pooled socket the server already closed. Only worth retrying
                # when the request came from the pool; a fresh connection failing
                # this way means something else is wrong and must surface.
                if not reused:
                    raise
                try:
                    c.conn.close()
                except Exception:
                    pass
                c = _Conn(http.client.HTTPSConnection(
                    self.host, self.port, timeout=self.timeout, context=self._ctx))
                reused = False
                c.conn.request(method, url, body=payload, headers=headers)
                resp = c.conn.getresponse()
            body = resp.read()
            status = resp.status
            reusable = status < 400
            if status == 401:
                raise PVEUnauthorized("PVE 返回 401：Token 无效或已过期")
            if status == 403:
                raise PVEPermission("PVE 返回 403：权限不足，请检查 ACL 是否授在 / 且勾选「继承」")
            if status >= 400:
                text = body[:200].decode("utf-8", "replace")
                raise PVEError("HTTP %d %s" % (status, text))
            try:
                doc = json.loads(body.decode("utf-8"))
            except ValueError:
                raise PVEError("响应不是合法 JSON")
            if isinstance(doc, dict) and "data" in doc:
                return doc["data"]
            return doc
        except (socket.timeout, TimeoutError):
            raise PVEUnreachable("连接 %s:%d 超时" % (self.host, self.port))
        except ssl.SSLError as e:
            raise PVEUnreachable("TLS 握手失败：%s" % e)
        except (http.client.HTTPException, OSError) as e:
            raise PVEUnreachable("无法连接 %s:%d：%s" % (self.host, self.port, e))
        finally:
            if timeout:
                c.conn.timeout = old_timeout
            self._checkin(c, reusable)

    # ---------------- fingerprint (unauthenticated) ----------------

    # The root page carries several ver= query strings: ExtJS reports its own
    # (7.0.0), and proxmoxlib.js carries the actual PVE release. Taking the
    # first match reports 7.0.0 on PVE 8, so the proxmoxlib line wins and the
    # rest is only a fallback.
    _VER_LIB = re.compile(r"proxmoxlib\.js\?ver=(\d+\.\d+\.\d+)")
    _VER_ANY = re.compile(r"[?&]ver=(\d+\.\d+\.\d+)")
    _NODE_RE = re.compile(r"NodeName\s*:\s*'([^']+)'")

    def fingerprint(self):
        """Read / (unauthenticated, ~5ms) for version + node name.

        This is what lets the first paint say something useful before a token
        is configured, instead of showing a blank page.
        """
        out = {"reachable": False, "version": "", "node": "", "latency_ms": 0}
        c, _reused = self._checkout()
        reusable = False
        t0 = time.time()
        try:
            c.conn.request("GET", "/", headers={"Accept": "text/html"})
            resp = c.conn.getresponse()
            body = resp.read(200000).decode("utf-8", "replace")
            reusable = resp.status < 400
            out["latency_ms"] = int((time.time() - t0) * 1000)
            out["reachable"] = True
            m = self._VER_LIB.search(body)
            if not m:
                hits = self._VER_ANY.findall(body)
                # drop the ExtJS release, which is never the PVE version
                hits = [h for h in hits if not h.startswith("7.")]
                m = None
                if hits:
                    class _M:
                        def __init__(self, v):
                            self._v = v

                        def group(self, _n):
                            return self._v

                    m = _M(hits[-1])
            if m:
                out["version"] = m.group(1)
            m = self._NODE_RE.search(body)
            if m:
                out["node"] = m.group(1)
        except (socket.timeout, TimeoutError):
            raise PVEUnreachable("连接 %s:%d 超时" % (self.host, self.port))
        except (http.client.HTTPException, OSError, ssl.SSLError) as e:
            raise PVEUnreachable("无法连接 %s:%d：%s" % (self.host, self.port, e))
        finally:
            self._checkin(c, reusable)
        return out

    def probe(self):
        """Cheap liveness+auth check.

        /api2/json/access/domains is unauthenticated and 200, so it answers in
        ~4ms and costs nothing when the credentials are dead. Testing with
        /api2/json/version instead would burn the full 3-second 401 penalty on
        every poll.
        """
        t0 = time.time()
        domains = self.request("/api2/json/access/domains")
        return {
            "latency_ms": int((time.time() - t0) * 1000),
            "domains": [d.get("name") if isinstance(d, dict) else str(d) for d in (domains or [])],
        }

    def close(self):
        with self._lock:
            for c in self._idle:
                try:
                    c.conn.close()
                except Exception:
                    pass
            self._idle = []
