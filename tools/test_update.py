#!/usr/bin/env python3
r"""test_update.py - exercise app/server/update.py against a local fake GitHub.

Nothing here touches the internet. A loopback HTTP server impersonates both
api.github.com and github.com, so every one of the three fallback layers can be
driven on purpose - including the two states that are easy to get wrong:

  * HTTP 403 from the REST API (60 anonymous calls/hour per egress address),
    which must fall through to the Atom feed rather than report a failure;
  * HTTP 404 for the whole repository, which today is the *normal* state of
    Smi13zZ/pvepanel and must come back as "no update", not an exception.

The cache tests move a fake clock instead of sleeping, so the 900 s success TTL
and the 60 s failure cooldown are both verified in milliseconds.

Run with the bundled interpreter:

  C:\Users\PVE\.dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\python\python.exe ^
      pve-panel\tools\test_update.py
"""

import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TOOLS_DIR)
SERVER_DIR = os.path.join(PROJECT_DIR, "src", "fn-pvepanel", "app", "server")
sys.path.insert(0, SERVER_DIR)

import update  # noqa: E402


def setup_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# --------------------------------------------------------------- fake github

ATOM_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Release notes from pvepanel</title>
  <entry>
    <id>tag:github.com,2008:Repository/1/%s</id>
    <updated>2026-11-05T12:00:00Z</updated>
    <link rel="alternate" type="text/html" href="%s/releases/tag/%s"/>
    <title>%s</title>
    <content type="html">&lt;p&gt;Atom notes for %s&lt;/p&gt;</content>
  </entry>
</feed>
"""

EMPTY_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Release notes from pvepanel</title>
</feed>
"""

ASSETS_HTML = """<div class="Box-body">
  <ul>
    <li><a href="/Smi13zZ/pvepanel/releases/download/v26.11.5/fn-pvepanel-26.11.5-fnos-all.fpk">x</a></li>
    <li><a href="/Smi13zZ/pvepanel/releases/download/v26.11.5/checksums.txt">y</a></li>
  </ul>
</div>
"""


class FakeGitHub(BaseHTTPRequestHandler):
    """Routes the four endpoints update.py knows about.

    mode controls what the API answers; the Atom feed and the asset fragment
    each have their own switch so the fallback chain can be cut at any link.
    """

    protocol_version = "HTTP/1.1"
    hits = []          # every (path, mode) served, for cache assertions
    mode = "new_api"
    atom = "ok"
    assets = "ok"

    def log_message(self, fmt, *args):   # keep the test output clean
        pass

    # -- helpers -----------------------------------------------------------
    def _send(self, code, body="", ctype="text/plain; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, code, doc):
        self._send(code, json.dumps(doc), "application/json; charset=utf-8")

    def _release(self, tag, name, notes, with_assets):
        assets = []
        if with_assets:
            assets = [
                {"name": "fn-pvepanel-" + tag.lstrip("v") + "-fnos-all.fpk",
                 "size": 207408,
                 "browser_download_url":
                     "https://example.invalid/download/" + tag + "/fn-pvepanel.fpk"},
                {"name": "checksums.txt", "size": 128,
                 "browser_download_url":
                     "https://example.invalid/download/" + tag + "/checksums.txt"},
                # an entry without a download URL must be skipped, not crash
                {"name": "build.log", "size": 4096},
            ]
        return {
            "tag_name": tag,
            "name": name,
            "body": notes,
            "published_at": "2026-11-05T12:00:00Z",
            "html_url": "https://github.com/Smi13zZ/pvepanel/releases/tag/" + tag,
            "assets": assets,
        }

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        self.hits.append((path, self.mode))
        base = "http://" + self.headers.get("Host", "127.0.0.1")

        if path.endswith("/releases/latest"):
            return self._api()

        if path.endswith("/releases.atom"):
            if self.atom == "404":
                return self._send(404, "Not Found")
            if self.atom == "empty":
                return self._send(200, EMPTY_ATOM, "application/atom+xml")
            return self._send(200, ATOM_TEMPLATE % (
                "v26.11.5", base + "/Smi13zZ/pvepanel", "v26.11.5",
                "PVE 面板 26.11.5", "v26.11.5"), "application/atom+xml")

        if "/releases/expanded_assets/" in path:
            if self.assets == "404":
                return self._send(404, "Not Found")
            return self._send(200, ASSETS_HTML, "text/html; charset=utf-8")

        return self._send(404, "Not Found")

    def _api(self):
        if self.mode == "new_api":
            return self._json(200, self._release(
                "v26.11.5", "PVE 面板 26.11.5",
                "## 26.11.5\n\n- 新增更新检查", True))
        if self.mode == "same_api":
            return self._json(200, self._release(
                "v26.11.0", "PVE 面板 26.11.0", "## 26.11.0\n\n首个版本", True))
        if self.mode == "rate_limited":
            return self._send(403, "API rate limit exceeded")
        if self.mode == "missing":
            return self._send(404, "Not Found")
        if self.mode == "broken_json":
            return self._send(200, "{not json", "application/json")
        if self.mode == "server_error":
            return self._send(500, "boom")
        return self._send(404, "Not Found")


class Server:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def set(self, mode="new_api", atom="ok", assets="ok"):
        FakeGitHub.mode = mode
        FakeGitHub.atom = atom
        FakeGitHub.assets = assets
        FakeGitHub.hits = []

    def hits(self):
        return len(FakeGitHub.hits)

    def paths(self):
        return [p for p, _m in FakeGitHub.hits]


# ------------------------------------------------------------------ harness

class Checker:
    def __init__(self):
        self.failed = 0
        self.passed = 0

    def ok(self, cond, label, detail=""):
        if cond:
            self.passed += 1
            print("  PASS  " + label)
            return True
        self.failed += 1
        print("  FAIL  " + label + (("  <- " + detail) if detail else ""))
        return False

    def eq(self, got, want, label):
        return self.ok(got == want, label, "got %r want %r" % (got, want))


def fresh(server, **kw):
    """Reset the module caches and point it at the fake server."""
    update.reset()
    return again(server, **kw)


def again(server, **kw):
    """Query the fake server *without* clearing the caches.

    The cache tests live off this: fresh() would reset the very state they
    are asserting on.
    """
    return update.check(kw.pop("current", "26.11.0"),
                        api_base=server.base, web_base=server.base, **kw)


def section(title):
    print("")
    print("== " + title)


# --------------------------------------------------------------------- cases

def test_version_compare(c):
    section("版本号比较（按数字分段，不是字符串）")
    c.eq(update.version_tuple("v26.11.10"), (26, 11, 10, 0), "26.11.10 解析为四段")
    c.eq(update.version_tuple("26.11.0"), (26, 11, 0, 0), "缺段补 0")
    c.eq(update.version_tuple(""), (0, 0, 0, 0), "空串退化为全 0")
    c.eq(update.version_tuple("garbage"), (0, 0, 0, 0), "无数字退化为全 0")
    c.eq(update.version_tuple("v26.11.5-beta1"), (26, 11, 5, 0),
         "预发布后缀不影响主版本")
    c.ok(update.is_newer("26.11.10", "26.11.9"),
         "26.11.10 > 26.11.9（字符串比较会说反）")
    c.ok(not update.is_newer("26.11.9", "26.11.10"), "反向不成立")
    c.ok(not update.is_newer("26.11.0", "26.11.0"), "相同版本不算更新")
    c.ok(update.is_newer("27.0.0", "26.11.99"), "跨年也能正确比较")
    c.ok(not update.is_newer("", "26.11.0"), "远端为空时不算更新")
    c.ok(not update.is_newer("26.11.5", ""), "当前版本未知时不算更新")


def test_new_version(c):
    section("情况一：有新版本（API 200 + 资产列表）")
    with Server() as s:
        s.set("new_api")
        info = fresh(s)
        c.ok(isinstance(info, dict), "返回的是 dict")
        c.eq(info.get("ok"), True, "ok=True")
        c.eq(info.get("has_update"), True, "has_update=True")
        c.eq(info.get("current"), "26.11.0", "current 原样回显")
        c.eq(info.get("latest"), "26.11.5", "latest 去掉 v 前缀")
        c.eq(info.get("name"), "PVE 面板 26.11.5", "发布名称解析")
        c.ok("新增更新检查" in info.get("notes", ""), "release notes 正常解析")
        c.eq(info.get("source"), "api", "来源是 api")
        c.eq(info.get("reason"), "", "成功时 reason 为空")
        c.eq(len(info.get("assets") or []), 2,
             "资产只保留带下载地址的两条（无 url 的被丢弃）")
        first = (info.get("assets") or [{}])[0]
        c.eq(first.get("size"), 207408, "资产大小解析为整数")
        c.ok(first.get("url", "").startswith("https://"),
             "资产下载地址保留")
        c.ok(info.get("checked_at", 0) > 0, "checked_at 有值")
        c.eq(info.get("cached"), False, "首次查询不是缓存")
        c.eq(info.get("checked"), True, "checked=True（前端用它在设置页出结论）")
        c.ok(info.get("html_url", "").endswith("/v26.11.5"),
             "html_url 是发布页地址")
        c.ok("/releases/latest" in " ".join(s.paths()),
             "确实走了 REST 接口")
        print("  返回值：" + json.dumps({
            "ok": info["ok"], "has_update": info["has_update"],
            "current": info["current"], "latest": info["latest"],
            "source": info["source"], "assets": len(info["assets"]),
        }, ensure_ascii=False))


def test_no_update(c):
    section("情况二：已是最新（API 200，版本相同）")
    with Server() as s:
        s.set("same_api")
        info = fresh(s)
        c.eq(info.get("ok"), True, "ok=True")
        c.eq(info.get("has_update"), False, "has_update=False")
        c.eq(info.get("latest"), "26.11.0", "latest 等于当前版本")
        c.eq(info.get("reason"), "", "没有问题时 reason 为空")
        c.eq(len(info.get("assets") or []), 2, "资产列表照常返回")

        # 版本更高一点点也必须判定为有更新
        s.set("same_api")
        FakeGitHub.mode = "new_api"
        info2 = fresh(s, current="26.11.4")
        c.eq(info2.get("has_update"), True, "当前 26.11.4 时 26.11.5 算更新")
        FakeGitHub.mode = "same_api"
        info3 = fresh(s, current="26.11.5")
        c.eq(info3.get("has_update"), False, "当前已是 26.11.5 时不算更新")


def test_rate_limited(c):
    section("情况三：403 限流 → 退到 Atom + expanded_assets")
    with Server() as s:
        s.set("rate_limited")
        info = fresh(s)
        c.eq(info.get("ok"), True, "ok=True（限流不算失败）")
        c.eq(info.get("source"), "atom", "来源退到 atom")
        c.eq(info.get("latest"), "26.11.5", "Atom 里解析出版本号")
        c.eq(info.get("has_update"), True, "仍然能判断有更新")
        c.ok("Atom notes" in info.get("notes", ""),
             "Atom 的 HTML 转义正文被压平")
        c.eq(len(info.get("assets") or []), 2,
             "下载列表从 expanded_assets HTML 补回")
        c.ok(all("example.invalid" not in a["url"] for a in info["assets"]),
             "补回的下载地址指向 web 域而不是 API 域")
        paths = s.paths()
        c.ok(any("/releases.atom" in p for p in paths), "调用过 Atom 订阅源")
        c.ok(any("/releases/expanded_assets/" in p for p in paths),
             "调用过 expanded_assets")
        c.eq(info.get("reason"), "", "成功时不带 reason")

        # 限流且资产页也挂：更新信息要保住，只是没有下载链接
        s.set("rate_limited", assets="404")
        info = fresh(s)
        c.eq(info.get("ok"), True, "资产页失败不影响更新结论")
        c.eq(info.get("has_update"), True, "依然报告有更新")
        c.eq(info.get("assets"), [], "资产列表为空但函数没抛异常")


def test_missing_repo(c):
    section("情况四：404 仓库不存在（当前的真实状态）")
    with Server() as s:
        s.set("missing", atom="404")
        info = fresh(s)
        c.ok(isinstance(info, dict), "没有抛异常，返回 dict")
        c.eq(info.get("ok"), False, "ok=False")
        c.eq(info.get("has_update"), False, "has_update=False")
        c.eq(info.get("reason"), "not_found", "reason 标明是仓库不存在")
        c.eq(info.get("latest"), "", "latest 为空")
        c.eq(info.get("assets"), [], "资产为空列表而不是 None")
        c.ok("404" in info.get("error", ""), "error 里带可读原因")
        c.eq(info.get("cached"), False, "首次失败不是缓存")
        c.eq(info.get("checked"), True, "失败也算查过（前端据此显示结论）")
        c.ok(bool(info.get("detail")), "detail 是给人看的一句话")
        print("  detail: " + info.get("detail", ""))

        # 仓库在但没发过版：也必须是安静的“无更新”
        s.set("missing", atom="empty")
        info = fresh(s)
        c.eq(info.get("ok"), False, "空 Atom 源 ok=False")
        c.eq(info.get("has_update"), False, "空 Atom 源 has_update=False")
        c.eq(info.get("reason"), "no_release", "reason 区分“没发过版”")
        c.ok(isinstance(info.get("error"), str) and info["error"],
             "error 是可读字符串")

        # API 返回坏 JSON：退到 Atom
        s.set("broken_json")
        info = fresh(s)
        c.eq(info.get("ok"), True, "坏 JSON 退到 Atom 后仍成功")
        c.eq(info.get("source"), "atom", "来源标记为 atom")

        # 5xx：退到 Atom
        s.set("server_error")
        info = fresh(s)
        c.eq(info.get("ok"), True, "500 也退到 Atom")
        c.eq(info.get("has_update"), True, "500 退路仍能判断更新")


def test_cache(c):
    section("缓存：900 s 成功缓存 + 60 s 失败冷却")
    clock = {"t": 1000.0}
    real_now = update._now
    update._now = lambda: clock["t"]
    try:
        with Server() as s:
            # ---- 成功缓存 ----
            s.set("new_api")
            a = fresh(s)
            first = s.hits()
            c.ok(first > 0, "首次查询真的打了网络")

            b = again(s)
            c.eq(b.get("cached"), True, "第二次直接命中成功缓存")
            c.eq(s.hits(), first, "命中缓存时没有再打网络")
            c.eq(b.get("latest"), a.get("latest"), "缓存内容与首次一致")

            clock["t"] += update.SUCCESS_TTL - 1
            again(s)
            c.eq(s.hits(), first, "TTL 未到仍走缓存")
            clock["t"] += 2
            expired = again(s)
            c.ok(s.hits() > first, "超过 900 s 后重新查询")
            c.eq(expired.get("cached"), False, "重新查询不是缓存")

            # ---- force 绕过 ----
            before = s.hits()
            forced = again(s, force=True)
            c.ok(s.hits() > before, "force=True 绕过成功缓存")
            c.eq(forced.get("cached"), False, "force 结果不标 cached")

            # ---- 失败冷却 ----
            s.set("missing", atom="404")
            f1 = fresh(s)
            hits1 = s.hits()
            c.eq(f1.get("ok"), False, "首次失败 ok=False")
            f2 = again(s)
            c.eq(f2.get("cached"), True, "失败在 60 s 内被缓存")
            c.eq(s.hits(), hits1, "冷却期内没有再打网络")
            clock["t"] += update.FAIL_TTL - 1
            again(s)
            c.eq(s.hits(), hits1, "冷却未到期仍不打网络")
            clock["t"] += 2
            f3 = again(s)
            c.eq(f3.get("cached"), False, "超过 60 s 后重试")
            c.ok(s.hits() > hits1, "重试真的打了网络")

            before = s.hits()
            again(s, force=True)
            c.ok(s.hits() > before, "force=True 绕过失败冷却")

            # ---- 时间戳只在查成功后写 ----
            update.reset()
            c.eq(update._CACHE["at"], 0.0, "reset 后成功时间戳为 0")
            s.set("missing", atom="404")
            fresh(s)
            c.eq(update._CACHE["at"], 0.0,
                 "失败不写成功时间戳（否则断网一次会吞掉后续补查）")
            c.ok(update._FAIL["at"] > 0, "失败时间戳才被写下")

            update.reset()
            s.set("new_api")
            fresh(s)
            c.ok(update._CACHE["at"] == clock["t"],
                 "成功时间戳取的是查询完成时刻")
            c.eq(update._FAIL["at"], 0.0, "成功会清掉失败冷却")

            # 失败不能延长上一次成功的缓存寿命
            update.reset()
            s.set("new_api")
            fresh(s)
            success_at = update._CACHE["at"]
            clock["t"] += 10
            s.set("missing", atom="404")
            info = again(s, force=True)
            c.eq(info.get("ok"), False, "force 查询失败")
            c.eq(update._CACHE["at"], success_at,
                 "失败不会刷新成功缓存的时间戳")
    finally:
        update._now = real_now
        update.reset()


def test_cache_isolation(c):
    section("缓存不能被调用方改坏（否则一次请求污染下一次）")
    with Server() as s:
        s.set("new_api")
        first = fresh(s)
        names = [a["name"] for a in first["assets"]]

        # 调用方就地改动返回的 assets
        first["assets"].append({"name": "POLLUTED", "size": 0, "url": ""})
        first["assets"][0]["name"] = "CLOBBERED"

        second = again(s)
        c.eq([a["name"] for a in second["assets"]], names,
             "命中缓存时拿到的 assets 与首次一致，没有被上一次调用污染")
        c.eq(second.get("cached"), True, "确认这次走的是缓存")

        # 未命中时的返回值也不能和缓存共用同一个 list 对象
        third = again(s, force=True)
        third["assets"].clear()
        fourth = again(s)
        c.eq(len(fourth["assets"]), 2, "force 查询后的返回值与缓存互不影响")


def test_poll_loop(c):
    section("poll_loop：后台线程的三种行为")
    with Server() as s:
        s.set("new_api")
        update.reset()
        stop = threading.Event()
        seen = []
        logs = []

        def get_settings():
            return {"current": "26.11.0", "update_check": True,
                    "update_repo": "Smi13zZ/pvepanel",
                    "update_api_base": s.base,
                    "update_web_base": s.base}

        t = threading.Thread(target=update.poll_loop, args=(
            get_settings, logs.append, stop, 0.05, 0.2), daemon=True)
        t.start()
        stop.wait(0.6)
        stop.set()
        t.join(3)
        c.ok(not t.is_alive(), "stop_event 能让线程退出")
        c.ok(s.hits() > 0, "线程确实查了版本")
        c.ok(any("发现新版本" in m for m in logs),
             "有新版本时写了日志（只报一次）", repr(logs))
        announce_count = len([m for m in logs if "发现新版本" in m])
        c.eq(announce_count, 1, "同一版本不会每小时重复播报")

        # 关闭开关后不再查询
        s.set("new_api")
        update.reset()
        stop2 = threading.Event()
        t2 = threading.Thread(target=update.poll_loop, args=(
            lambda: {"current": "26.11.0", "update_check": False,
                     "update_api_base": s.base, "update_web_base": s.base},
            logs.append, stop2, 0.02, 0.2), daemon=True)
        t2.start()
        stop2.wait(0.3)
        stop2.set()
        t2.join(3)
        c.eq(s.hits(), 0, "update_check=False 时一个请求都不发")

        # 版本来源：settings 没带版本时退回 PVEPANEL_VERSION 环境变量
        c.eq(update._current_from({}), "", "settings 与环境都没有版本时为空")
        update.set_version("26.11.3")
        try:
            c.eq(update._current_from({}), "26.11.3", "set_version 指定后能取到")
            c.eq(update._current_from({"current": "26.11.7"}), "26.11.7",
                 "settings 里的显式版本优先")
            os.environ["PVEPANEL_VERSION"] = "26.11.9"
            update.set_version("")
            c.eq(update._current_from({}), "26.11.9",
                 "没有显式版本时读 PVEPANEL_VERSION")
            os.environ["PVEPANEL_VERSION"] = "0.0.0"
            c.eq(update._current_from({}), "",
                 "服务自身占位值 0.0.0 不当作真实版本")
        finally:
            os.environ.pop("PVEPANEL_VERSION", None)
            update.set_version("")

        # get_settings 抛异常不能让线程死掉
        s.set("new_api")
        update.reset()
        stop3 = threading.Event()
        calls = {"n": 0}

        def broken_settings():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("配置读不了")
            return {"current": "26.11.0", "update_api_base": s.base,
                    "update_web_base": s.base}

        t3 = threading.Thread(target=update.poll_loop, args=(
            broken_settings, logs.append, stop3, 0.02, 0.1), daemon=True)
        t3.start()
        stop3.wait(0.5)
        stop3.set()
        t3.join(3)
        c.ok(calls["n"] >= 2, "get_settings 抛异常后线程继续跑下一轮")
        c.ok(s.hits() > 0, "异常之后的轮次照常查询")


def test_interface(c):
    section("对外接口形状")
    c.ok(callable(update.check), "check 可调用")
    c.ok(callable(update.poll_loop), "poll_loop 可调用")
    c.ok(callable(update.set_logger), "set_logger 可调用")
    c.eq(update.DEFAULT_REPO, "Smi13zZ/pvepanel", "默认仓库名正确")
    with Server() as s:
        s.set("new_api")
        info = fresh(s)
        required = ("ok", "current", "latest", "has_update", "notes",
                    "url", "html_url", "assets", "source", "reason",
                    "detail", "error", "cached", "checked", "checked_at")
        missing = [k for k in required if k not in info]
        c.eq(missing, [], "返回字段齐全")
        c.ok(isinstance(info["assets"], list), "assets 是 list")
        c.ok(isinstance(info["has_update"], bool), "has_update 是 bool")
        payload = json.dumps(info, ensure_ascii=False)
        c.ok(len(payload) > 0, "返回值可 JSON 序列化（要直接下发前端）")


# ---------------------------------------------------------------------- main

def main():
    setup_stdio()
    print("update.py 单元测试（本机假 HTTP 服务，不访问外网）")
    print("被测模块：" + update.__file__)
    c = Checker()
    for fn in (test_version_compare, test_new_version, test_no_update,
               test_rate_limited, test_missing_repo, test_cache,
               test_cache_isolation, test_poll_loop, test_interface):
        try:
            fn(c)
        except Exception:
            c.failed += 1
            print("  FAIL  %s 抛异常：\n%s" % (fn.__name__, traceback.format_exc()))
    print("")
    print("-" * 56)
    print("通过 %d，失败 %d" % (c.passed, c.failed))
    if c.failed:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
