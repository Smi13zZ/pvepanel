#!/usr/bin/env python3
r"""test_notify.py - end to end check of app/server/notify.py against a sink.

Nothing here touches the internet. A local HTTP sink stands in for every vendor
endpoint: it records the exact request (method, path, query, headers, body) and
replies with a scripted response, so what gets verified is the real wire format
each channel produces - URL shape, encoding, signing, and how the verdict in the
response body is interpreted.

Two suites run:

  [E2E]  every channel is sent through the real send() to the sink, with a
         success body and a failure body; the handler's default endpoints are
         monkeypatched to the sink so no vendor host is ever contacted.
  [UNIT] merge_config / mask / _judge / _dingtalk_url semantics, including the
         incident that must never repeat: a masked secret echoed back as the
         whole config must not overwrite the stored key.

Run with the bundled interpreter, from anywhere:

  C:\Users\PVE\.dsh\dsh-runtimes\...\python.exe tools\test_notify.py

Exit code is 0 only when every check passes. Secrets in this file are fabricated
placeholders (FAKE_*), never real credentials.
"""

import base64
import hashlib
import hmac
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(HERE)
SERVER_DIR = os.path.join(PROJECT_DIR, "src", "fn-pvepanel", "app", "server")
sys.path.insert(0, SERVER_DIR)

import notify  # noqa: E402

# 假值：任何一个都不对应真实账号，也不允许换成真的。
FAKE_BARK_KEY = "FAKEbarkKey123456"
FAKE_SENDKEY = "SCTfakeSendKey98765"
FAKE_PUSHPLUS_TOKEN = "FAKEpushplusToken01"
FAKE_WXPUSHER_TOKEN = "AT_FAKEwxpusherToken"
FAKE_WXPUSHER_UID = "UID_FAKE0001"
FAKE_WXPUSHER_TOPIC = "12345"
FAKE_TG_TOKEN = "123456:FAKEtelegramToken"
FAKE_TG_CHAT = "-1001234567890"
FAKE_WECOM_KEY = "FAKEwecomKey-0001"
FAKE_DINGTALK_SECRET = "SECfakeDingtalkSign0001"
FAKE_SLACK_PATH = "/services/FAKE/FAKE/slackHook"
FAKE_WEBHOOK_HEADER = "FAKE-Header-Value"


# ---------------------------------------------------------------- sink server
class Sink:
    """Local HTTP server that records requests and replays scripted responses."""

    def __init__(self):
        self.records = []
        self.route_bodies = {}
        self.response = (200, {"Content-Type": "application/json"}, b"{}")
        self.lock = threading.Lock()
        self.httpd = None
        self.port = None

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def _handle(self, method):
                length = 0
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b""
                parts = urllib.parse.urlsplit(self.path)
                rec = {
                    "method": method,
                    "path": parts.path,
                    "raw_path": self.path,
                    "query": urllib.parse.parse_qs(parts.query, keep_blank_values=True),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": raw.decode("utf-8", "replace"),
                    "at": time.time(),
                }
                with outer.lock:
                    outer.records.append(rec)
                    status, headers, payload = outer.response
                    # 路径级覆盖：同一个 sink 上同时验证成功与失败两种响应
                    for prefix, resp in outer.route_bodies.items():
                        if parts.path.startswith(prefix):
                            status, headers, payload = resp
                            break
                self.send_response(status)
                for key, val in (headers or {}).items():
                    self.send_header(key, val)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                self._handle("POST")

            def do_GET(self):
                self._handle("GET")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t.start()
        return self

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
            try:
                self.httpd.server_close()
            except Exception:
                pass

    # -- helpers -----------------------------------------------------------
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def reset(self, status=200, body="{}", ctype="application/json"):
        with self.lock:
            self.records = []
            self.route_bodies = {}
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            self.response = (status, {"Content-Type": ctype},
                             body.encode("utf-8") if isinstance(body, str) else body)

    def set_route(self, prefix, status=200, body="{}", ctype="application/json"):
        with self.lock:
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            self.route_bodies[prefix] = (status, {"Content-Type": ctype},
                                         body.encode("utf-8") if isinstance(body, str) else body)

    def only(self):
        with self.lock:
            return self.records[0] if len(self.records) == 1 else None

    def count(self):
        with self.lock:
            return len(self.records)


# ------------------------------------------------------------------ harness
class Report:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, name, cond, extra=""):
        if cond:
            self.passed += 1
            print("  ok   %s" % name)
            return True
        self.failed.append(name)
        print("  FAIL %s%s" % (name, (" | " + str(extra)) if extra else ""))
        return False

    def eq(self, name, got, want):
        return self.check(name, got == want, "got=%r want=%r" % (got, want))

    def section(self, title):
        print("")
        print("-- %s" % title)


def patch_endpoints(sink):
    """Point every vendor endpoint at the sink; returns the previous values."""
    base = sink.base()
    saved = {
        "BARK_DEFAULT_SERVER": notify.BARK_DEFAULT_SERVER,
        "SERVERCHAN_API": notify.SERVERCHAN_API,
        "PUSHPLUS_API": notify.PUSHPLUS_API,
        "WXPUSHER_API": notify.WXPUSHER_API,
        "TELEGRAM_API": notify.TELEGRAM_API,
        "WECOM_API": notify.WECOM_API,
    }
    notify.BARK_DEFAULT_SERVER = base + "/bark"
    notify.SERVERCHAN_API = base + "/serverchan"
    notify.PUSHPLUS_API = base + "/pushplus/send"
    notify.WXPUSHER_API = base + "/wxpusher/send"
    notify.TELEGRAM_API = base + "/telegram"
    notify.WECOM_API = base + "/wecom/webhook/send"
    return saved


def restore_endpoints(saved):
    for key, val in saved.items():
        setattr(notify, key, val)


def channel(ctype, cfg, name="", cid="c1"):
    return {"id": cid, "name": name or ctype, "type": ctype, "enabled": True, "config": cfg}


# ------------------------------------------------------------------- E2E
def e2e_bark(rep, sink):
    rep.section("Bark")
    sink.reset(200, {"code": 200, "message": "success"})
    ch = channel("bark", {"server": sink.base() + "/bark", "key": FAKE_BARK_KEY})
    ok, detail = notify.send(ch, "[PVEpanel] 存储水位告警", "节点: pve1\n用量: 91%")
    rec = sink.only()
    rep.check("请求已到达 sink", rec is not None, sink.count())
    if rec:
        rep.eq("请求方法是 POST", rec["method"], "POST")
        rep.eq("设备 key 拼在路径里", rec["path"], "/bark/" + FAKE_BARK_KEY)
        body = json.loads(rec["body"])
        rep.eq("title 原样传出", body.get("title"), "[PVEpanel] 存储水位告警")
        rep.check("body 是多行纯文本", "\n" in body.get("body", ""), body.get("body"))
        rep.eq("分组固定 PVEpanel", body.get("group"), "PVEpanel")
        rep.check("Content-Type 是 json", "application/json" in rec["headers"].get("content-type", ""))
    rep.check("code=200 判成功", ok is True, detail)

    # 服务端用 200 之外的 code 拒绝，HTTP 仍是 200
    sink.reset(200, {"code": 400, "message": "failed to get device token"})
    ok, detail = notify.send(ch, "t", "b")
    rep.check("code!=200 判失败（HTTP 200）", ok is False, detail)
    rep.check("失败原因带上游 message", "device token" in detail, detail)

    sink.reset(200, {"code": 200})
    ok, _ = notify.send(channel("bark", {"key": ""}), "t", "b")
    rep.check("缺 key 直接失败不发请求", ok is False and sink.count() == 0)


def e2e_serverchan(rep, sink):
    rep.section("Server 酱")
    sink.reset(200, {"code": 0, "message": ""})
    ch = channel("serverchan", {"sendkey": FAKE_SENDKEY})
    ok, detail = notify.send(ch, "[PVEpanel] 节点离线", "节点: pve2\n状态: 不在线")
    rec = sink.only()
    rep.check("请求已到达 sink", rec is not None, sink.count())
    if rec:
        rep.eq("路径含 sendkey", rec["path"], "/serverchan/" + FAKE_SENDKEY + ".send")
        rep.check("表单编码而非 JSON",
                  "application/x-www-form-urlencoded" in rec["headers"].get("content-type", ""),
                  rec["headers"].get("content-type"))
        form = urllib.parse.parse_qs(rec["body"])
        rep.eq("title 字段", form.get("title", [""])[0], "[PVEpanel] 节点离线")
        rep.check("desp 是多行正文", "\n" in form.get("desp", [""])[0], form.get("desp"))
    rep.check("code=0 判成功", ok is True, detail)

    sink.reset(200, {"code": 40001, "message": "bad sendkey"})
    ok, detail = notify.send(ch, "t", "b")
    rep.check("code!=0 判失败", ok is False and "40001" in detail, detail)


def e2e_pushplus(rep, sink):
    rep.section("PushPlus")
    sink.reset(200, {"code": 200, "msg": "ok"})
    ch = channel("pushplus", {"token": FAKE_PUSHPLUS_TOKEN})
    ok, detail = notify.send(ch, "[PVEpanel] 磁盘温度告警", "设备: /dev/sda\n温度: 58°C")
    rec = sink.only()
    rep.check("请求已到达 sink", rec is not None, sink.count())
    if rec:
        rep.eq("路径", rec["path"], "/pushplus/send")
        body = json.loads(rec["body"])
        rep.eq("token 在 body 里", body.get("token"), FAKE_PUSHPLUS_TOKEN)
        rep.eq("模板用 txt（正文是纯文本行）", body.get("template"), "txt")
        rep.eq("content 是正文", body.get("content"), "设备: /dev/sda\n温度: 58°C")
    rep.check("code=200 判成功", ok is True, detail)

    sink.reset(200, {"code": 999, "msg": "token 无效"})
    ok, detail = notify.send(ch, "t", "b")
    rep.check("code!=200 判失败", ok is False, detail)


def e2e_wxpusher(rep, sink):
    rep.section("WxPusher")
    sink.reset(200, {"code": 1000, "msg": "处理成功"})
    ch = channel("wxpusher", {"app_token": FAKE_WXPUSHER_TOKEN,
                              "uids": FAKE_WXPUSHER_UID + ", UID_FAKE0002",
                              "topics": FAKE_WXPUSHER_TOPIC + ",abc"})
    ok, detail = notify.send(ch, "[PVEpanel] 备份任务失败", "任务: vzdump-101\n状态: ERROR")
    rec = sink.only()
    rep.check("请求已到达 sink", rec is not None, sink.count())
    if rec:
        rep.eq("路径", rec["path"], "/wxpusher/send")
        body = json.loads(rec["body"])
        rep.eq("appToken", body.get("appToken"), FAKE_WXPUSHER_TOKEN)
        rep.eq("uids 按逗号拆开并去空格", body.get("uids"), [FAKE_WXPUSHER_UID, "UID_FAKE0002"])
        rep.eq("topics 只收数字并转 int", body.get("topicIds"), [int(FAKE_WXPUSHER_TOPIC)])
        rep.eq("summary 截到 99 字符内", len(body.get("summary") or ""), len("[PVEpanel] 备份任务失败"))
        rep.check("标题并进正文（这个渠道没有独立标题）",
                  body.get("content", "").startswith("[PVEpanel] 备份任务失败"),
                  body.get("content", "")[:40])
    rep.check("code=1000 判成功", ok is True, detail)

    sink.reset(200, {"code": 1001, "msg": "appToken 错误"})
    ok, detail = notify.send(ch, "t", "b")
    rep.check("code!=1000 判失败", ok is False, detail)


def e2e_telegram(rep, sink):
    rep.section("Telegram")
    sink.reset(200, {"ok": True, "result": {"message_id": 1}})
    ch = channel("telegram", {"bot_token": FAKE_TG_TOKEN, "chat_id": FAKE_TG_CHAT,
                              "api_base": sink.base() + "/telegram"})
    ok, detail = notify.send(ch, "[PVEpanel] 内存吃紧", "节点: pve1\n内存: 93%")
    rec = sink.only()
    rep.check("请求已到达 sink", rec is not None, sink.count())
    if rec:
        rep.eq("token 在路径里", rec["path"], "/telegram/bot%s/sendMessage" % FAKE_TG_TOKEN)
        body = json.loads(rec["body"])
        rep.eq("chat_id 原样传字符串", body.get("chat_id"), FAKE_TG_CHAT)
        rep.check("标题与正文用换行拼成一条",
                  body.get("text", "").startswith("[PVEpanel] 内存吃紧\n节点: pve1"),
                  body.get("text"))
        rep.check("关掉链接预览", body.get("disable_web_page_preview") is True)
    rep.check("ok=true 判成功", ok is True, detail)

    # 关键点：HTTP 200 但 ok=false，这就是"只看状态码"会漏掉的那类失败
    sink.reset(200, {"ok": False, "description": "Bad Request: chat not found"})
    ok, detail = notify.send(ch, "t", "b")
    rep.check("HTTP 200 + ok=false 判失败", ok is False, detail)
    rep.check("失败原因取 description", "chat not found" in detail, detail)

    sink.reset(401, {"ok": False, "description": "Unauthorized"})
    ok, detail = notify.send(ch, "t", "b")
    rep.check("HTTP 401 判失败", ok is False and "401" in detail, detail)


def e2e_wecom(rep, sink):
    rep.section("企业微信机器人")
    sink.reset(200, {"errcode": 0, "errmsg": "ok"})
    ch = channel("wecom", {"key": FAKE_WECOM_KEY})
    ok, detail = notify.send(ch, "[PVEpanel] 节点离线", "节点: pve3\n状态: 不在线")
    rec = sink.only()
    rep.check("请求已到达 sink", rec is not None, sink.count())
    if rec:
        rep.eq("路径", rec["path"], "/wecom/webhook/send")
        rep.eq("key 在查询串里", rec["query"].get("key"), [FAKE_WECOM_KEY])
        body = json.loads(rec["body"])
        rep.eq("msgtype 是 text", body.get("msgtype"), "text")
        rep.eq("标题并进 content",
               body.get("text", {}).get("content"), "[PVEpanel] 节点离线\n节点: pve3\n状态: 不在线")
    rep.check("errcode=0 判成功", ok is True, detail)

    sink.reset(200, {"errcode": 93000, "errmsg": "invalid webhook url"})
    ok, detail = notify.send(ch, "t", "b")
    rep.check("HTTP 200 + errcode!=0 判失败", ok is False, detail)
    rep.check("失败原因取 errmsg", "invalid webhook url" in detail, detail)


def e2e_dingtalk(rep, sink):
    rep.section("钉钉机器人（含加签落点）")
    sink.reset(200, {"errcode": 0, "errmsg": "ok"})
    ch = channel("dingtalk", {"url": sink.base() + "/dingtalk/robot/send?access_token=FAKEtoken",
                              "secret": FAKE_DINGTALK_SECRET})
    before_ms = int(time.time() * 1000)
    ok, detail = notify.send(ch, "[PVEpanel] 存储水位告警", "存储: local-lvm\n用量: 96%")
    after_ms = int(time.time() * 1000)
    rec = sink.only()
    rep.check("请求已到达 sink", rec is not None, sink.count())
    if rec:
        rep.eq("路径", rec["path"], "/dingtalk/robot/send")
        rep.eq("原有 access_token 保留", rec["query"].get("access_token"), ["FAKEtoken"])
        stamps = rec["query"].get("timestamp") or []
        signs = rec["query"].get("sign") or []
        rep.check("timestamp 已附加", len(stamps) == 1, stamps)
        rep.check("sign 已附加", len(signs) == 1, signs)
        if stamps and signs:
            stamp = stamps[0]
            rep.check("timestamp 是毫秒时间戳且在本次调用区间内",
                      stamp.isdigit() and before_ms - 2000 <= int(stamp) <= after_ms + 2000, stamp)
            # 用同样的算法在测试侧独立算一遍，验的是"签名落点"而不是"签名字段非空"
            expect = base64.b64encode(hmac.new(
                FAKE_DINGTALK_SECRET.encode("utf-8"),
                ("%s\n%s" % (stamp, FAKE_DINGTALK_SECRET)).encode("utf-8"),
                digestmod=hashlib.sha256).digest()).decode("ascii")
            rep.eq("sign 与独立计算的 HMAC-SHA256(base64) 一致", signs[0], expect)
            raw_sign = rec["raw_path"].split("sign=")[-1]
            rep.check("sign 写进 URL 时是百分号编码（%2B/%2F/%3D，不是裸 base64）",
                      "%" in raw_sign and "+" not in raw_sign and "/" not in raw_sign,
                      raw_sign)
            rep.eq("解码后正是独立计算的签名", urllib.parse.unquote(raw_sign), expect)
        body = json.loads(rec["body"])
        rep.eq("msgtype 是 text", body.get("msgtype"), "text")
        rep.check("标题并进 content", body.get("text", {}).get("content", "").startswith("[PVEpanel]"))
    rep.check("errcode=0 判成功", ok is True, detail)

    # 未开加签：不能凭空塞 timestamp/sign，否则钉钉报 400
    sink.reset(200, {"errcode": 0})
    notify.send(channel("dingtalk", {"url": sink.base() + "/dingtalk/plain", "secret": ""}), "t", "b")
    rec = sink.only()
    if rec:
        rep.check("未配密钥时不加 timestamp/sign",
                  "timestamp" not in rec["query"] and "sign" not in rec["query"], rec["raw_path"])

    sink.reset(200, {"errcode": 310000, "errmsg": "sign not match"})
    ok, detail = notify.send(ch, "t", "b")
    rep.check("HTTP 200 + errcode!=0 判失败", ok is False, detail)
    rep.check("失败原因取 errmsg", "sign not match" in detail, detail)


def e2e_slack(rep, sink):
    rep.section("Slack")
    sink.reset(200, "ok", "text/plain")
    ch = channel("slack", {"url": sink.base() + FAKE_SLACK_PATH})
    ok, detail = notify.send(ch, "[PVEpanel] 订阅到期", "节点: pve1\n剩余: 12 天")
    rec = sink.only()
    rep.check("请求已到达 sink", rec is not None, sink.count())
    if rec:
        rep.eq("路径", rec["path"], FAKE_SLACK_PATH)
        body = json.loads(rec["body"])
        rep.eq("标题用 mrkdwn 加粗后并进 text",
               body.get("text"), "*[PVEpanel] 订阅到期*\n节点: pve1\n剩余: 12 天")
    rep.check("纯文本 ok 判成功", ok is True, detail)

    sink.reset(500, "channel_not_found", "text/plain")
    ok, detail = notify.send(ch, "t", "b")
    rep.check("HTTP 500 判失败", ok is False and "500" in detail, detail)


def e2e_webhook(rep, sink):
    rep.section("自定义 Webhook")
    sink.reset(200, {"ok": True})
    ch = channel("webhook", {
        "url": sink.base() + "/custom/hook",
        "headers": "X-Token: " + FAKE_WEBHOOK_HEADER + "\nX-Empty:\n# comment line",
    })
    ok, detail = notify.send(ch, "[PVEpanel] 有新版本", "当前: 26.11.0\n最新: 26.11.1")
    rec = sink.only()
    rep.check("请求已到达 sink", rec is not None, sink.count())
    if rec:
        rep.eq("路径", rec["path"], "/custom/hook")
        rep.eq("自定义头已带上", rec["headers"].get("x-token"), FAKE_WEBHOOK_HEADER)
        rep.check("无冒号的注释行被丢弃",
                  "# comment line" not in rec["headers"] and len(rec["headers"]) >= 5,
                  sorted(rec["headers"]))
        rep.eq("空值头保留为空字符串（不禁用，交给服务端判断）",
               rec["headers"].get("x-empty"), "")
        body = json.loads(rec["body"])
        rep.eq("title 独立字段", body.get("title"), "[PVEpanel] 有新版本")
        rep.eq("content 是正文", body.get("content"), "当前: 26.11.0\n最新: 26.11.1")
        rep.eq("source 标识本产品", body.get("source"), "PVEpanel")
    rep.check("200 判成功", ok is True, detail)

    sink.reset(404, "not found", "text/plain")
    ok, detail = notify.send(ch, "t", "b")
    rep.check("HTTP 404 判失败", ok is False and "404" in detail, detail)


def e2e_edges(rep, sink):
    rep.section("边界与健壮性")
    sink.reset(200, {"errcode": 0})
    ok, detail = notify.send(channel("nope", {}), "t", "b")
    rep.check("未知类型不发请求且失败", ok is False and sink.count() == 0, detail)

    rep.check("空 channel 不抛异常", notify.send(None, "t", "b")[0] is False)
    rep.check("channel 不是 dict 也不抛异常", notify.send("x", "t", "b")[0] is False)

    # 连不上的端口：必须变成 (False, 原因)，不能抛
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    ok2, detail2 = notify.send(channel("webhook", {"url": "http://127.0.0.1:%d/x" % dead_port}),
                               "t", "b", timeout=1.0)
    rep.check("连不上时返回 False 而不抛", ok2 is False, detail2)
    rep.eq("没有任何请求打到 sink", sink.count(), 0)

    # test_channel 用的是预置文案
    sink.reset(200, {"errcode": 0, "errmsg": "ok"})
    ok, detail = notify.test_channel(channel("wecom", {"key": FAKE_WECOM_KEY}, name="运维群"))
    rec = sink.only()
    rep.check("test_channel 判成功", ok is True, detail)
    if rec:
        content = json.loads(rec["body"]).get("text", {}).get("content", "")
        rep.check("测试文案带渠道名", "运维群" in content, content)
        rep.check("测试标题带产品前缀", content.startswith("[PVEpanel] 通知测试"), content[:30])

    # send_all：坏渠道不影响其它渠道
    sink.reset(200, {"errcode": 0})
    sink.set_route("/wecom", 200, {"errcode": 0})
    sink.set_route("/dingtalk", 200, {"errcode": 400, "errmsg": "bad"})
    res = notify.send_all([
        channel("wecom", {"key": FAKE_WECOM_KEY}, cid="a"),
        {"id": "off", "type": "wecom", "enabled": False, "config": {"key": "x"}},
        channel("dingtalk", {"url": sink.base() + "/dingtalk/bot", "secret": ""}, cid="b"),
        channel("bark", {"key": ""}, cid="c"),
    ], "[PVEpanel] 测试", "正文")
    rep.eq("send_all 只处理启用渠道", len(res), 3)
    rep.check("第一个成功", res[0]["ok"] is True, res[0])
    rep.check("第二个失败但没中断", res[1]["ok"] is False, res[1])
    rep.check("第三个因缺 key 失败", res[2]["ok"] is False, res[2])
    rep.check("结果里带 id", [r["id"] for r in res] == ["a", "b", "c"], [r["id"] for r in res])


# ------------------------------------------------------------------- UNIT
def unit_merge(rep):
    rep.section("merge_config：密钥保留（__SET__ 事故防线）")
    old = {"channels": [
        {"id": "a", "name": "Bark", "type": "bark", "enabled": True,
         "config": {"server": "https://api.day.app", "key": "REALKEY"}},
        {"id": "b", "name": "钉钉", "type": "dingtalk", "enabled": True,
         "config": {"url": "https://oapi.dingtalk.com/robot/send?access_token=REAL",
                    "secret": "REALSECRET"}},
    ], "notify_enabled": True, "notify_events": {"node_offline": True}}

    # 1) 前端把整份打码配置回传（真实事故现场）
    masked = notify.mask(old)
    merged = notify.merge_config(old, masked)
    rep.eq("回传打码配置后，bark key 仍是真实值",
           merged["channels"][0]["config"]["key"], "REALKEY")
    rep.eq("回传打码配置后，钉钉 url 仍是真实值",
           merged["channels"][1]["config"]["url"],
           "https://oapi.dingtalk.com/robot/send?access_token=REAL")
    rep.eq("回传打码配置后，钉钉 secret 仍是真实值",
           merged["channels"][1]["config"]["secret"], "REALSECRET")
    rep.check("落盘值里不含 __SET__ 字面量",
              "__SET__" not in json.dumps(merged, ensure_ascii=False),
              json.dumps(merged, ensure_ascii=False)[:200])

    # 2) 真的填了新密钥
    merged2 = notify.merge_config(old, {"channels": [
        {"id": "a", "type": "bark", "config": {"key": "NEWKEY"}}]})
    rep.eq("填新 key 时替换", merged2["channels"][0]["config"]["key"], "NEWKEY")
    rep.eq("没提到的 server 保留", merged2["channels"][0]["config"]["server"], "https://api.day.app")
    rep.eq("没提到的渠道保留", len(merged2["channels"]), 2)
    rep.eq("没提到的设置保留", merged2["notify_enabled"], True)

    # 3) 空串等于不修改
    merged3 = notify.merge_config(old, {"channels": [
        {"id": "a", "type": "bark", "config": {"key": "", "server": ""}}]})
    rep.eq("空串不覆盖密钥", merged3["channels"][0]["config"]["key"], "REALKEY")
    rep.eq("空串会清空非密钥字段", merged3["channels"][0]["config"]["server"], "")

    # 4) 非密钥字段照常更新
    merged4 = notify.merge_config(old, {"channels": [
        {"id": "a", "type": "bark", "config": {"server": "https://bark.example.com"}}]})
    rep.eq("非密钥字段正常更新",
           merged4["channels"][0]["config"]["server"], "https://bark.example.com")

    # 5) 只提交 settings 分区时，channels 原样不动
    merged5 = notify.merge_config(old, {"notify_enabled": False})
    rep.eq("只提交 settings 时 channels 不动", merged5["channels"], old["channels"])
    rep.eq("只提交 settings 时值确实变了", merged5["notify_enabled"], False)

    # 6) 新增渠道被追加，显式空表清空
    merged6 = notify.merge_config(old, {"channels": [
        {"id": "c", "name": "Slack", "type": "slack", "config": {"url": "https://hooks.slack.com/x"}}]})
    rep.eq("新渠道被追加", len(merged6["channels"]), 3)
    rep.eq("新渠道内容正确", merged6["channels"][2]["config"]["url"], "https://hooks.slack.com/x")
    rep.eq("显式空表清空", notify.merge_config(old, {"channels": []})["channels"], [])

    # 7) 输入不被就地修改
    snapshot = json.dumps(old, ensure_ascii=False, sort_keys=True)
    notify.merge_config(old, {"channels": [{"id": "a", "config": {"key": "X"}}]})
    rep.eq("merge_config 不修改入参", json.dumps(old, ensure_ascii=False, sort_keys=True), snapshot)

    # 8) 标记跑到非密钥字段里也不落盘
    merged8 = notify.merge_config(old, {"channels": [
        {"id": "a", "type": "bark", "config": {"server": "__SET__"}}]})
    rep.eq("非密钥字段收到 __SET__ 时保留旧值",
           merged8["channels"][0]["config"]["server"], "https://api.day.app")

    # 9) 深层嵌套：notify_events 里的单个开关
    merged9 = notify.merge_config(old, {"notify_events": {"storage_full": False}})
    rep.eq("嵌套开关被改", merged9["notify_events"]["storage_full"], False)
    rep.eq("嵌套里别的开关保留", merged9["notify_events"]["node_offline"], True)


def unit_length(rep, sink):
    rep.section("超长告警不会撑爆渠道限制")
    sink.reset(200, {"errcode": 0, "errmsg": "ok"})
    big = "\n".join("guest-%03d: 未被任何备份任务覆盖" % i for i in range(300))
    rep.check("构造的正文确实超长", len(big) > 4000, len(big))
    ok, detail = notify.send(channel("wecom", {"key": FAKE_WECOM_KEY}), "[PVEpanel] 虚拟机无备份", big)
    rec = sink.only()
    rep.check("超长正文仍然发送成功", ok is True, detail)
    if rec:
        body = json.loads(rec["body"])
        content = body.get("text", {}).get("content", "")
        rep.check("正文按 UTF-8 字节截到企业微信 2048 字节限额内",
                  len(content.encode("utf-8")) <= 2048, len(content.encode("utf-8")))
        rep.check("截断没有切坏多字节字符", "�" not in content, repr(content[-10:]))
        rep.check("截断处有说明而不是静默丢字", "已截断" in content, content[-40:])
        rep.check("标题保留在开头", content.startswith("[PVEpanel] 虚拟机无备份"), content[:40])

    sink.reset(200, {"code": 200})
    ok, detail = notify.send(channel("bark", {"key": FAKE_BARK_KEY, "server": sink.base() + "/bark"}),
                             "[PVEpanel] " + "很长的标题" * 40, "正文")
    rec = sink.only()
    rep.check("超长标题也被截断", ok is True and rec is not None, detail)
    if rec:
        title = json.loads(rec["body"]).get("title", "")
        rep.check("标题长度受限（按字节）",
                  len(title.encode("utf-8")) <= notify.MAX_TITLE_BYTES, len(title.encode("utf-8")))


def unit_store_shape(rep):
    rep.section("与 store.py 的键名对齐（notify_channels）")
    # store.DEFAULTS 用的键是 notify_channels，不是 channels；两种写法都要安全。
    stored = {"notify_channels": [
        {"id": "a", "name": "Bark", "type": "bark", "enabled": True,
         "config": {"server": "https://api.day.app", "key": "REALKEY"}},
    ], "notify_enabled": True}

    masked = notify.mask(stored)
    rep.eq("整份设置里 notify_channels 也被打码",
           masked["notify_channels"][0]["config"]["key"], notify.SECRET_MARK)
    rep.check("整份设置打码后不含真实密钥", "REALKEY" not in json.dumps(masked, ensure_ascii=False))

    merged = notify.merge_config(stored, masked)
    rep.eq("回传 notify_channels 打码配置不覆盖密钥",
           merged["notify_channels"][0]["config"]["key"], "REALKEY")
    rep.check("落盘值不含 __SET__", "__SET__" not in json.dumps(merged, ensure_ascii=False))

    merged2 = notify.merge_config(stored, {"notify_channels": [
        {"id": "a", "type": "bark", "config": {"key": "NEWKEY"}}]})
    rep.eq("notify_channels 下填新密钥能替换",
           merged2["notify_channels"][0]["config"]["key"], "NEWKEY")

    # 真实 store 的默认设置跑一遍，确认不炸也不丢键
    try:
        sys.path.insert(0, SERVER_DIR)
        import store as store_mod
        defaults = dict(store_mod.DEFAULTS)
        defaults["notify_channels"] = stored["notify_channels"]
        out = notify.mask(defaults)
        rep.check("对 store.DEFAULTS 打码不改变其它设置",
                  out.get("refresh") == store_mod.DEFAULTS["refresh"]
                  and out.get("notify_events") == store_mod.DEFAULTS["notify_events"])
        rep.eq("store.DEFAULTS 的 notify_channels 被打码",
               out["notify_channels"][0]["config"]["key"], notify.SECRET_MARK)
        rep.eq("notify_events 的键与 notify.EVENTS 完全对应",
               set(store_mod.DEFAULTS["notify_events"]), set(notify.EVENTS))
        rep.eq("store.py 的 SECRET_MARK 与本模块一致",
               store_mod.SECRET_MARK, notify.SECRET_MARK)
    except ImportError:
        print("  skip store.py 不在本次测试目录里，跳过与 store 的对齐检查")
    except Exception as exc:
        rep.check("能加载 store.py 做对齐检查", False, exc)


def unit_mask(rep):
    rep.section("mask / channel_public")
    ch = {"id": "a", "name": "Bark", "type": "bark", "enabled": True,
          "config": {"server": "https://api.day.app", "key": "REALKEY"}}
    pub = notify.mask(ch)
    rep.eq("密钥换成标记", pub["config"]["key"], notify.SECRET_MARK)
    rep.eq("非密钥原样", pub["config"]["server"], "https://api.day.app")
    rep.check("公开结果不含真实密钥", "REALKEY" not in json.dumps(pub, ensure_ascii=False))

    whole = notify.mask({"channels": [ch], "notify_enabled": True})
    rep.eq("整份配置里的渠道也被打码", whole["channels"][0]["config"]["key"], notify.SECRET_MARK)
    rep.eq("整份配置里的普通设置保留", whole["notify_enabled"], True)

    empty = notify.mask({"channels": [{"id": "z", "type": "bark", "config": {}}]})
    rep.eq("未设置的密钥显示空串（而不是标记）", empty["channels"][0]["config"]["key"], "")

    rep.eq("mask(None) 返回空字典", notify.mask(None), {})
    rep.check("未知类型渠道不泄密",
              notify.mask({"id": "u", "type": "unknown", "config": {"x": "y"}})["config"] == {})

    for ctype in notify.CHANNEL_TYPES:
        fields = notify.CHANNEL_TYPES[ctype]["fields"]
        secrets = notify.CHANNEL_TYPES[ctype]["secrets"]
        sample = {f: ("REAL-" + f) for f in fields}
        pub = notify.channel_public({"id": "x", "type": ctype, "config": sample})
        leaked = [f for f in secrets if "REAL-" + f in json.dumps(pub, ensure_ascii=False)]
        rep.check("%s 的所有密钥字段都被打码" % ctype, not leaked, leaked)


def unit_judge(rep):
    rep.section("_judge：判成败不看 HTTP 状态码")
    rep.check("200 + 无 code 判成功", notify._judge(200, "ok")[0] is True)
    rep.check("500 判失败", notify._judge(500, "boom")[0] is False)
    rep.check("201 判成功", notify._judge(201, "")[0] is True)
    rep.check("200 但非 JSON 不硬判失败", notify._judge(200, "<html>", "errcode", 0)[0] is True)
    rep.check("errcode=0 成功", notify._judge(200, '{"errcode":0}', "errcode", 0)[0] is True)
    rep.check("errcode 字符串 '0' 也算成功",
              notify._judge(200, '{"errcode":"0"}', "errcode", 0)[0] is True)
    rep.check("errcode=310000 失败",
              notify._judge(200, '{"errcode":310000,"errmsg":"x"}', "errcode", 0)[0] is False)
    rep.check("telegram ok=false 失败",
              notify._judge(200, '{"ok":false,"description":"x"}', "ok", True)[0] is False)
    rep.check("telegram ok=true 成功",
              notify._judge(200, '{"ok":true}', "ok", True)[0] is True)


def unit_dingtalk_sign(rep):
    rep.section("_dingtalk_url：签名算法")
    url = "https://oapi.dingtalk.com/robot/send?access_token=T"
    signed = notify._dingtalk_url(url, "SECabc")
    parsed = urllib.parse.urlsplit(signed)
    q = urllib.parse.parse_qs(parsed.query)
    rep.eq("access_token 保留", q.get("access_token"), ["T"])
    stamp = (q.get("timestamp") or [""])[0]
    sign = (q.get("sign") or [""])[0]
    expect = base64.b64encode(hmac.new(b"SECabc", ("%s\nSECabc" % stamp).encode("utf-8"),
                                       hashlib.sha256).digest()).decode("ascii")
    rep.eq("签名 = HMAC-SHA256(secret, ts+\n+secret) 的 base64", sign, expect)
    rep.check("签名字符串里确实是 时间戳换行密钥（不是密钥换行时间戳）",
              sign != base64.b64encode(hmac.new(b"SECabc", ("SECabc\n%s" % stamp).encode("utf-8"),
                                                hashlib.sha256).digest()).decode("ascii"))
    rep.eq("无密钥时原样返回", notify._dingtalk_url(url, ""), url)
    rep.eq("无密钥且空白时原样返回", notify._dingtalk_url(url, "   "), url)
    rep.check("无 query 时用 ? 拼接",
              "?timestamp=" in notify._dingtalk_url("https://x/y", "s"))


def unit_meta(rep):
    rep.section("元数据表")
    rep.check("九种通道齐全", len(notify.CHANNEL_TYPES) == 9, list(notify.CHANNEL_TYPES))
    for ctype, meta in notify.CHANNEL_TYPES.items():
        rep.check("%s 有双语显示名" % ctype,
                  bool(meta.get("label_zh")) and bool(meta.get("label_en")))
        rep.check("%s 每个字段都有双语 labels" % ctype,
                  all(set((meta.get("labels") or {}).get(f) or {}) >= {"zh", "en"}
                      for f in meta["fields"]))
        rep.check("%s 的 secrets 是 fields 子集" % ctype,
                  set(meta["secrets"]) <= set(meta["fields"]))
    zh = notify.type_meta("zh")
    en = notify.type_meta("en")
    rep.eq("中文标签", zh["bark"]["labels"]["key"], "设备 Key")
    rep.eq("英文标签", en["bark"]["labels"]["key"], "Device key")
    rep.eq("双语表同时下发", zh["bark"]["labels_i18n"]["key"]["en"], "Device key")
    rep.eq("type_list 中文", [x["type"] for x in notify.type_list("zh")][0], "bark")
    rep.eq("事件清单 12 条", len(notify.EVENTS), 12)
    rep.check("事件双语", all(set(v) >= {"zh", "en", "level"} for v in notify.EVENTS.values()))
    rep.eq("未知事件名原样回显", notify.event_label("nope"), "nope")


def unit_format(rep):
    rep.section("format_alert / format_test")
    title, body = notify.format_alert("storage_full", node="pve1",
                                     fields={"存储": "local-lvm", "用量": "96%"},
                                     ts="2026-11-05 20:00:00")
    rep.eq("标题形如 [PVEpanel] 存储水位告警", title, "[PVEpanel] 存储水位告警")
    rep.check("正文含节点", "节点: pve1" in body, body)
    rep.check("正文含存储名", "local-lvm" in body, body)
    rep.check("正文含百分比", "96%" in body, body)
    rep.check("正文含时间", "2026-11-05 20:00:00" in body, body)
    rep.check("正文是多行", len(body.splitlines()) >= 4, body)
    rep.check("正文没有 Markdown 表格", "|" not in body and "---" not in body, body)

    t_en, b_en = notify.format_alert("node_offline", node="pve9", lang="en")
    rep.eq("英文标题", t_en, "[PVEpanel] Node offline")
    rep.check("英文正文用 Node:", "Node: pve9" in b_en, b_en)

    t, b = notify.format_test("运维群")
    rep.check("测试标题", t.startswith("[PVEpanel]"), t)
    rep.check("测试正文点名渠道", "运维群" in b, b)
    # One message carries both languages: a push goes to a phone, and the
    # panel's language setting belongs to a browser - they are different people.
    rep.eq("测试标题中英合一", t, "[PVEpanel] 通知测试 / Notification test")
    rep.check("测试正文含英文行", "channel is configured correctly" in b, b)
    rep.check("测试正文中英之间留空行", "\n\n" in b, repr(b[:120]))
    t_en, b_en = notify.format_test("ops", lang="en")
    rep.check("英文面板下英文在前", b_en.split("\n\n")[0].startswith("If you can read this"), b_en[:80])
    rep.check("英文面板下中文仍在", "渠道配置正确" in b_en, b_en)


def unit_notifier_bilingual(rep):
    """The alert pushes carry both languages too, not just the test message."""
    rep.section("notifier 推送双语")
    import notifier

    class _Store(object):
        var_dir = ""

        def __init__(self, settings):
            self._s = settings

        def settings(self):
            return self._s

    class _Notify(object):
        def __init__(self):
            self.sent = []

        def send(self, ch, title, body):
            self.sent.append((ch, title, body))
            return True, "ok"

    settings = {"notify_enabled": True, "lang": "zh",
                "notify_channels": [{"id": "a", "type": "webhook", "name": "hook",
                                     "url": "http://127.0.0.1:1/x", "enabled": True}],
                "notify_events": {"node_offline": True, "storage_full": True,
                                  "auth_failed": True}}
    mock = _Notify()
    nt = notifier.Notifier(_Store(settings), mock, lambda *_a: None)
    nt.observe({"server": {"name": "pve1"},
                "meta": {},
                "alerts": [{"title": "节点离线", "detail": "节点: pve1"}]},
               "pve1")
    rep.check("离线告警推送了一条", len(mock.sent) == 1, mock.sent)
    if mock.sent:
        _ch, title, body = mock.sent[0]
        rep.eq("告警标题中英合一", title, "[PVEpanel] 节点离线 / Node offline")
        rep.check("告警正文含英文行", "\n\n" in body, repr(body[:120]))

    mock.sent.clear()
    nt2 = notifier.Notifier(_Store(settings), mock, lambda *_a: None)
    nt2.observe({"server": {"name": "pve1"},
                 "meta": {"auth_failed": True},
                 "alerts": []}, "pve1")
    rep.check("Token 失效也推送了", len(mock.sent) == 1, mock.sent)
    if mock.sent:
        _ch, title, body = mock.sent[0]
        rep.eq("Token 失效标题中英合一", title,
               "[PVEpanel] Token 失效 / Token invalid")
        rep.check("Token 失效正文含英文行", "polling has stopped" in body, body)


def main():
    rep = Report()
    print("PVEpanel notify.py 端到端自检")
    print("  module : %s" % notify.__file__)
    print("  python : %s" % sys.version.split()[0])

    sink = Sink().start()
    print("  sink   : %s" % sink.base())
    saved = patch_endpoints(sink)
    try:
        e2e_bark(rep, sink)
        e2e_serverchan(rep, sink)
        e2e_pushplus(rep, sink)
        e2e_wxpusher(rep, sink)
        e2e_telegram(rep, sink)
        e2e_wecom(rep, sink)
        e2e_dingtalk(rep, sink)
        e2e_slack(rep, sink)
        e2e_webhook(rep, sink)
        e2e_edges(rep, sink)
        unit_length(rep, sink)
        unit_merge(rep)
        unit_store_shape(rep)
        unit_mask(rep)
        unit_judge(rep)
        unit_dingtalk_sign(rep)
        unit_meta(rep)
        unit_format(rep)
        unit_notifier_bilingual(rep)
    finally:
        restore_endpoints(saved)
        sink.stop()

    total = rep.passed + len(rep.failed)
    print("")
    print("=" * 62)
    print("  PASS %d / %d" % (rep.passed, total))
    if rep.failed:
        print("  FAILED:")
        for name in rep.failed:
            print("    - %s" % name)
    print("=" * 62)
    return 0 if not rep.failed else 1


if __name__ == "__main__":
    sys.exit(main())
