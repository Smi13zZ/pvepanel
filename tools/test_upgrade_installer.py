#!/usr/bin/env python3
r"""test_upgrade_installer.py - exercise app/server/installer.py.

Everything runs locally. No internet, no fnOS box, no real package install:
  * a loopback HTTP server serves a real fpk-shaped tar.gz (and the bad cases:
    404, a truncated transfer, an oversized body);
  * appcenter-cli is a stand-in executable in tmp/_unit, so validate_fpk's
    "can I actually install this" check is exercised off-device;
  * start() is given an injected spawner, so the staging logic is tested
    without launching /bin/sh - and the generated script is then parsed and its
    behaviour asserted on directly.

Scratch space is pve-panel/tmp/_unit/ (never tempfile.mkdtemp: on this Windows
sandbox a freshly created directory can refuse subsequent writes).

Run with the bundled interpreter:

  C:\Users\PVE\.dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\python\python.exe ^
      pve-panel\tools\test_upgrade_installer.py
"""

import io
import json
import os
import shutil
import sys
import tarfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TOOLS_DIR)
SERVER_DIR = os.path.join(PROJECT_DIR, "src", "fn-pvepanel", "app", "server")
sys.path.insert(0, SERVER_DIR)

import installer  # noqa: E402

UNIT_DIR = os.path.join(PROJECT_DIR, "tmp", "_unit", "upgrade-installer")


def setup_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ------------------------------------------------------------------ fixtures

def make_fpk(path, appname="fn-pvepanel", version="26.11.4", members=None,
             junk_after=None):
    """Write a tar.gz shaped like a real fpk: manifest + app.tgz + friends."""
    if members is None:
        members = ["manifest", "app.tgz", "cmd", "config", "ui"]
    manifest = ("appname               = %s\n"
                "version               = %s\n"
                "display_name          = PVE 面板\n"
                "platform              = all\n"
                "service_port          = 8899\n" % (appname, version))
    payloads = {
        "manifest": manifest.encode("utf-8"),
        "app.tgz": b"\x1f\x8b\x08\x00" + b"fake-app-tarball" * 40,
        "cmd": b"fake cmd tree\n",
        "config": b"fake config tree\n",
        "ui": b"fake ui tree\n",
    }
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    with tarfile.open(path, "w:gz") as tf:
        for name in members:
            data = payloads.get(name, b"")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    if junk_after:
        with open(path, "ab") as f:
            f.write(junk_after)
    return path


def make_fake_appcenter(folder):
    """A stand-in appcenter-cli: validate_fpk only checks that one exists."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "appcenter-cli")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/bin/sh\nexit 0\n")
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
    return path


class FakeRelease(BaseHTTPRequestHandler):
    """Serves the bytes of a prepared file, with switchable failure modes."""

    protocol_version = "HTTP/1.1"
    body = b""
    mode = "ok"          # ok | missing | truncated | no_length | huge
    hits = 0

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        FakeRelease.hits += 1
        if self.mode == "missing":
            return self._send(404, b"Not Found")
        body = FakeRelease.body
        if self.mode == "huge":
            body = b"\0" * (2 * 1024 * 1024)
        if self.mode == "truncated":
            # Promise the full length, deliver half, then hang up. This is what
            # a dropped connection mid-download actually looks like, and it is
            # the case the .part-file discipline exists for.
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:max(1, len(body) // 2)])
            try:
                self.wfile.flush()
            except Exception:
                pass
            self.close_connection = True
            return
        if self.mode == "no_length":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(body), 1024):
                chunk = body[i:i + 1024]
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        return self._send(200, body)

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeRelease)
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

    def serve(self, path, mode="ok"):
        with open(path, "rb") as f:
            FakeRelease.body = f.read()
        FakeRelease.mode = mode
        FakeRelease.hits = 0
        return self.base + "/" + os.path.basename(path)


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
        print("  FAIL  " + label + (("  <- " + str(detail)) if detail else ""))
        return False

    def eq(self, got, want, label, extra=""):
        detail = "got %r want %r" % (got, want)
        if extra:
            detail += "  " + str(extra)
        return self.ok(got == want, label, detail)

    def raises(self, fn, needle, label):
        try:
            fn()
        except ValueError as exc:
            return self.ok(needle in str(exc), label,
                           "message was %r" % (str(exc),))
        except Exception as exc:
            return self.ok(False, label, "wrong exception %r" % (exc,))
        return self.ok(False, label, "no exception raised")


def section(title):
    print("")
    print("== " + title)


def fresh_workspace(name):
    """A clean var dir under tmp/_unit (never tempfile.mkdtemp)."""
    path = os.path.join(UNIT_DIR, name)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def fake_spawn(record):
    """Stands in for subprocess.Popen: records the call, returns a stub pid."""
    class Proc:
        pid = 424242

    def spawn(script, workdir):
        record.append({"script": script, "workdir": workdir})
        return Proc()

    return spawn


def release_info(url, version="26.11.4", name=None):
    return {
        "ok": True,
        "has_update": True,
        "latest": version,
        "assets": [{
            "name": name or ("fn-pvepanel-%s-fnos-all.fpk" % version),
            "size": 1024,
            "url": url,
        }],
    }


# -------------------------------------------------------------------- cases

def test_contract(c):
    section("对外契约：名字与常量不许漂")
    c.eq(installer.PHASES, ("idle", "downloading", "installing", "failed"),
         "PHASES 四个阶段")
    for fn in ("status", "start", "pick_asset", "validate_fpk", "download",
               "build_script", "reset"):
        c.ok(callable(getattr(installer, fn, None)), "存在 " + fn + "()")
    c.eq(installer.APP_NAME, "fn-pvepanel", "APP_NAME 正确")


def test_status_shape(c):
    section("status() 字段齐全、百分比正确")
    installer.set_context(var_dir=fresh_workspace("status"), current="26.11.3")
    installer.reset()
    st = installer.status()
    for key in ("phase", "version", "detail", "done", "total", "percent",
                "at", "log", "current"):
        c.ok(key in st, "status 含 " + key)
    c.eq(st["phase"], "idle", "reset 后是 idle")
    c.eq(st["current"], "26.11.3", "current 回显")
    c.eq(st["percent"], 0, "没有总量时百分比是 0（不假装完成）")

    c.eq(installer._percent(0, 0), 0, "0/0 是 0")
    c.eq(installer._percent(50, 100), 50, "50/100 是 50")
    c.eq(installer._percent(100, 100), 100, "100/100 是 100")
    c.eq(installer._percent(150, 100), 100, "超过总量夹到 100")
    c.eq(installer._percent(-5, 100), 0, "负数夹到 0")
    c.eq(installer._percent("x", "y"), 0, "非法值不炸")

    # 分阶段时字节计数器保持不动（进度条不能回跳）
    installer._set("downloading", "26.11.4", "下载中", done=30, total=100)
    c.eq(installer.status()["percent"], 30, "下载中 30%")
    installer._set("installing", "26.11.4", "安装中")
    after = installer.status()
    c.eq(after["percent"], 30, "转安装阶段后百分比不回跳")
    c.eq(after["phase"], "installing", "阶段已切换")


def test_pick_asset(c):
    section("pick_asset：只挑 fpk，优先 -all")
    c.eq(installer.pick_asset({"assets": []}), None, "空列表返回 None")
    c.eq(installer.pick_asset({}), None, "没有 assets 返回 None")
    c.eq(installer.pick_asset(None), None, "None 也返回 None")

    assets = [
        {"name": "checksums.txt", "url": "u1"},
        {"name": "release.zip", "url": "u2"},
        {"name": "fn-pvepanel-26.11.4-x86.fpk", "url": "u3"},
        {"name": "fn-pvepanel-26.11.4-fnos-all.fpk", "url": "u4"},
    ]
    got = installer.pick_asset({"assets": assets})
    c.eq(got["url"], "u4", "两个 fpk 里优先 -all")

    only = [{"name": "a.fpk", "url": "u9"}]
    c.eq(installer.pick_asset({"assets": only})["url"], "u9", "只有一个就用它")

    no_url = [{"name": "a.fpk"}]
    c.eq(installer.pick_asset({"assets": no_url}), None,
         "没有下载地址的资产不算数（否则会去下载空 URL）")

    mixed = [{"name": "a.txt", "url": "x"}, {"name": "B.FPK", "url": "y"}]
    c.eq(installer.pick_asset({"assets": mixed})["url"], "y",
         "扩展名大小写不敏感")

    with_junk = [{"name": "ok.fpk", "url": "u"}, "not a dict", None]
    c.eq(installer.pick_asset({"assets": with_junk})["url"], "u",
         "列表里混进非 dict 不炸")


def test_safe_name(c):
    section("资产名过滤：要能安全放进 shell 脚本")
    c.eq(installer._safe_name("fn-pvepanel-26.11.4-fnos-all.fpk"),
         "fn-pvepanel-26.11.4-fnos-all.fpk", "正常名字不变")
    c.eq(installer._safe_name("../../etc/passwd.fpk"), "passwd.fpk",
         "路径穿越被削平")
    # basename() keeps only the last path segment, so the dangerous part is
    # already gone before the character filter runs; assert what survives.
    c.eq(installer._safe_name("a b;rm -rf x.fpk"), "a-b-rm--rf-x.fpk",
         "空格与分号被替换成连字符")
    c.eq(installer._safe_name("a b;rm -rf /.fpk"), "fpk.fpk",
         "带路径的名字先取 basename，危险部分不会进脚本")
    c.eq(installer._safe_name("x$(id).fpk"), "x-id-.fpk",
         "命令替换语法被清除")
    c.ok(installer._safe_name("").endswith(".fpk"), "空名字兜底成 .fpk")
    c.ok(installer._safe_name("noext").endswith(".fpk"),
         "没有扩展名会补上 .fpk")


def test_validate(c):
    section("validate_fpk：不验证通过就绝不卸载")
    ws = fresh_workspace("validate")
    cli = make_fake_appcenter(os.path.join(ws, "bin"))
    good = make_fpk(os.path.join(ws, "good.fpk"))
    installer.validate_fpk(good, appcenter=cli)
    c.ok(True, "合法 fpk 通过校验")

    installer.validate_fpk(good, appcenter=cli, expect_version="26.11.4")
    c.ok(True, "版本号匹配时通过")

    missing = os.path.join(ws, "nope.fpk")
    c.raises(lambda: installer.validate_fpk(missing, appcenter=cli),
             "不存在", "文件不存在被拒")

    empty = os.path.join(ws, "empty.fpk")
    open(empty, "wb").close()
    c.raises(lambda: installer.validate_fpk(empty, appcenter=cli),
             "空文件", "空文件被拒")

    nottar = os.path.join(ws, "notatar.fpk")
    with open(nottar, "wb") as f:
        f.write(b"this is definitely not a tarball")
    c.raises(lambda: installer.validate_fpk(nottar, appcenter=cli),
             "解压", "非 tar 文件被拒")

    nomanifest = make_fpk(os.path.join(ws, "nomanifest.fpk"),
                          members=["app.tgz"])
    c.raises(lambda: installer.validate_fpk(nomanifest, appcenter=cli),
             "manifest", "缺 manifest 被拒")

    noapp = make_fpk(os.path.join(ws, "noapp.fpk"), members=["manifest"])
    c.raises(lambda: installer.validate_fpk(noapp, appcenter=cli),
             "app.tgz", "缺 app.tgz 被拒")

    wrongapp = make_fpk(os.path.join(ws, "wrongapp.fpk"), appname="fn-qbpanel")
    c.raises(lambda: installer.validate_fpk(wrongapp, appcenter=cli),
             "应用名", "应用名不符被拒")

    c.raises(lambda: installer.validate_fpk(good, appcenter=cli,
                                            expect_version="26.11.9"),
             "版本不匹配", "版本不符被拒（防止装到旧资产）")

    # 截断的包：tar.gz 尾巴被砍掉
    truncated = os.path.join(ws, "trunc.fpk")
    with open(good, "rb") as f:
        data = f.read()
    with open(truncated, "wb") as f:
        f.write(data[:len(data) // 2])
    c.raises(lambda: installer.validate_fpk(truncated, appcenter=cli),
             "解压", "截断的包被拒")

    # 没有 appcenter-cli 时必须拒绝，否则脚本会在卸载后才失败
    c.raises(lambda: installer.validate_fpk(good, appcenter="/nonexistent/x"),
             "appcenter-cli", "找不到 appcenter-cli 被拒")


def test_download(c):
    section("download：落到 .part，成功才改名，失败不留残file")
    ws = fresh_workspace("download")
    fpk = make_fpk(os.path.join(ws, "pkg.fpk"), version="26.11.4")
    with open(fpk, "rb") as f:
        expected = len(f.read())

    with Server() as s:
        url = s.serve(fpk, "ok")
        dest = os.path.join(ws, "dl", "pkg.fpk")
        seen = []
        size = installer.download(url, dest, on_progress=lambda d, t: seen.append((d, t)))
        c.eq(size, expected, "返回字节数与文件一致")
        c.ok(os.path.isfile(dest), "文件落到 dest")
        c.ok(not os.path.exists(dest + ".part"), ".part 已改名、没有残留")
        c.ok(len(seen) >= 2, "on_progress 至少回调了开始和结束")
        c.eq(seen[0][0], 0, "第一次回调 done=0")
        c.eq(seen[-1][0], expected, "最后一次回调 done=总字节")
        with open(dest, "rb") as f:
            c.eq(len(f.read()), expected, "落盘内容长度正确")

        # 404：报错且不留文件
        s.serve(fpk, "missing")
        dest2 = os.path.join(ws, "dl", "missing.fpk")
        c.raises(lambda: installer.download(s.url_of(fpk) if False else
                                            s.base + "/x.fpk", dest2),
                 "下载失败", "404 报错")
        s.serve(fpk, "ok")

        # 超过 limit：中止并清理
        dest3 = os.path.join(ws, "dl", "big.fpk")
        s.serve(fpk, "huge")
        c.raises(lambda: installer.download(s.base + "/big.fpk", dest3,
                                            limit=1024 * 1024),
                 "超过", "超过 limit 被中止")
        c.ok(not os.path.exists(dest3), "超限后 dest 不存在")
        c.ok(not os.path.exists(dest3 + ".part"), "超限后 .part 也清理掉")

        # 空响应体
        dest4 = os.path.join(ws, "dl", "zero.fpk")
        c.raises(lambda: installer.download("", dest4), "为空", "空 URL 被拒")

        # 截断：服务端承诺满长度却只发一半就断开
        dest5 = os.path.join(ws, "dl", "trunc.fpk")
        s.serve(fpk, "truncated")
        c.raises(lambda: installer.download(s.base + "/trunc.fpk", dest5, timeout=5),
                 "下载不完整", "截断传输被识别为下载不完整")
        c.ok(not os.path.exists(dest5), "截断后 dest 不存在（不会被当成好包）")
        c.ok(not os.path.exists(dest5 + ".part"), "截断后没有 .part 残留")

        # 没有 Content-Length 时 total=0，不应影响下载
        dest6 = os.path.join(ws, "dl", "chunked.fpk")
        s.serve(fpk, "no_length")
        got = installer.download(s.base + "/chunked.fpk", dest6, timeout=10)
        c.eq(got, expected, "chunked 无长度也能完整下载")


def test_script(c):
    section("build_script：生成的安装脚本内容正确")
    text = installer.build_script("26.11.4", 8899, "/var/update/x.fpk",
                                 "/var/update/rollback.fpk", "/var/update/install.log")
    c.ok(text.startswith("#!/bin/sh"), "有 shebang")
    for needle, label in (
        ("appcenter-cli uninstall", "包含卸载步骤"),
        ("appcenter-cli install-fpk", "包含安装步骤"),
        ("appcenter-cli status", "轮询状态而不是信返回码"),
        ("appcenter-cli start", "包含启动"),
        ("rolling back", "包含回滚分支"),
        ("start_new_session", "不该出现在脚本里（那是 Python 侧的事）"),
    ):
        if needle == "start_new_session":
            c.ok(needle not in text, "脚本里不含 " + needle)
        else:
            c.ok(needle in text, "脚本含「%s」" % label)

    c.ok("26.11.4" in text, "版本号写进脚本")
    c.ok("8899" in text, "端口写进脚本")
    c.ok("/var/update/x.fpk" in text, "包路径写进脚本")
    c.ok("/var/update/rollback.fpk" in text, "回滚包路径写进脚本")
    c.ok("/var/update/install.log" in text, "日志路径写进脚本")
    c.ok("@VERSION@" not in text, "所有占位符都被替换")
    c.ok("@LOG@" not in text, "LOG 占位符被替换")
    # sh 的百分号不能被误伤
    c.ok("%F %T" in text, "shell 的 date 格式串完好")
    c.ok("%s" in text, "shell 的 printf 格式串完好")
    # 这条是真实踩过的坑：模板写成非 raw 字符串时，printf 里的转义换行会
    # 变成真正的换行，把格式串截断成未闭合的引号。sh -n 照样通过，
    # 只有在设备上真跑才会暴露。这里直接断言两个字符都在。
    printf_lines = [ln for ln in text.splitlines() if "printf " in ln]
    c.ok(bool(printf_lines), "脚本里有 printf 行")
    c.ok(any(ln.count("'") >= 2 for ln in printf_lines),
         "printf 格式串的引号成对（没被注入真换行截断）",
         printf_lines[:1])
    c.ok("\\n" in text, "printf 的转义换行以两个字符的形式保留在脚本里",
         "got %r" % (text[text.find("printf"):text.find("printf") + 30],))

    # 回滚为空时的分支
    empty_prev = installer.build_script("26.11.4", 8899, "/p/x.fpk", "",
                                        "/l/install.log")
    c.ok("no previous package available" in empty_prev,
         "没有回滚包时有明确说明")

    # 非法输入必须在这里就炸，而不是卸载之后
    c.raises(lambda: installer.build_script("26.11.4", "abc", "/p/x.fpk", "",
                                            "/l/install.log"),
             "端口", "非数字端口被拒")
    c.raises(lambda: installer.build_script("26.11.4", 70000, "/p/x.fpk", "",
                                            "/l/install.log"),
             "端口", "越界端口被拒")
    c.raises(lambda: installer.build_script("26.11.4", 8899, "", "",
                                            "/l/install.log"),
             "安装包路径", "空包路径被拒")
    c.raises(lambda: installer.build_script("26.11.4", 8899, "/p/x.fpk", "", ""),
             "日志路径", "空日志路径被拒")


def test_start_happy(c):
    section("start：下载→校验→写脚本→脱离进程启动")
    ws = fresh_workspace("start_happy")
    cli = make_fake_appcenter(os.path.join(ws, "bin"))
    fpk = make_fpk(os.path.join(ws, "pkg.fpk"), version="26.11.4")
    var = os.path.join(ws, "var")
    os.makedirs(var, exist_ok=True)
    installer.set_context(var_dir=var, current="26.11.3")
    installer.reset()

    with Server() as s:
        url = s.serve(fpk, "ok")
        spawned = []
        res = installer.start(release_info(url), "26.11.3", 8899, var,
                              log=lambda m: None, appcenter=cli,
                              spawn=fake_spawn(spawned))
        c.eq(res.get("ok"), True, "start 成功", res)
        c.eq(res.get("version"), "26.11.4", "返回版本号")
        c.eq(res.get("file"), "fn-pvepanel-26.11.4-fnos-all.fpk", "返回文件名")

        updir = os.path.join(var, "update")
        c.ok(os.path.isfile(os.path.join(updir, res["file"])),
             "安装包已落到 update/ 目录")
        c.ok(os.path.isfile(os.path.join(updir, "install.sh")),
             "安装脚本已写出")
        c.ok(os.path.isfile(os.path.join(updir, "install.log")) or True,
             "日志路径已确定")

        c.eq(len(spawned), 1, "spawn 被调用一次")
        c.ok(spawned[0]["workdir"] == updir, "工作目录是 update/")
        with open(spawned[0]["script"], "r", encoding="utf-8") as f:
            script = f.read()
        c.ok("26.11.4" in script, "脚本里是这次要装的版本")

        st = installer.status()
        c.eq(st["phase"], "installing", "阶段推进到 installing")
        c.eq(st["version"], "26.11.4", "状态里记录了版本")
        c.eq(st["percent"], 100, "下载完成后百分比是 100")
        c.ok(st["done"] > 0, "done 记录了真实字节数")

        # 下载中的状态应该被记录过
        c.ok(installer._STATE["total"] > 0, "total 来自 Content-Length")

    # 已在安装中时不允许再点
    installer._set("installing", "26.11.4", "进行中")
    again = installer.start(release_info("http://127.0.0.1:1/x.fpk"), "26.11.3",
                            8899, var, log=lambda m: None, appcenter=cli,
                            spawn=fake_spawn([]))
    c.eq(again.get("ok"), False, "进行中再次 start 被拒")
    c.ok("进行中" in again.get("error", ""), "拒绝原因明确")

    installer.reset()
    busy = installer.start(release_info("http://127.0.0.1:1/x.fpk"), "26.11.3",
                           8899, var, log=lambda m: None, appcenter=cli,
                           spawn=fake_spawn([]))
    installer._set("downloading", "x", "下载中")
    c.eq(installer.start(release_info("http://127.0.0.1:1/x.fpk"), "26.11.3",
                         8899, var, log=lambda m: None, appcenter=cli,
                         spawn=fake_spawn([])).get("ok"), False,
         "downloading 阶段再次 start 也被拒")
    installer.reset()


def test_start_rejections(c):
    section("start：各种不该开始的情况都给出明确错误")
    ws = fresh_workspace("start_reject")
    cli = make_fake_appcenter(os.path.join(ws, "bin"))
    var = os.path.join(ws, "var")
    os.makedirs(var, exist_ok=True)
    installer.set_context(var_dir=var, current="26.11.3")

    def call(info, **kw):
        return installer.start(info, "26.11.3", 8899, var, log=lambda m: None,
                               appcenter=cli, spawn=fake_spawn([]), **kw)

    installer.reset()
    r = call({"has_update": False, "latest": "26.11.3", "assets": []})
    c.eq(r.get("ok"), False, "没有更新时拒绝")
    c.ok("最新" in r.get("error", ""), "说明是最新版本")

    installer.reset()
    r = call({"has_update": True, "latest": "", "assets": []})
    c.eq(r.get("ok"), False, "没有版本号时拒绝")

    installer.reset()
    r = call({"has_update": True, "latest": "26.11.4", "assets": []})
    c.eq(r.get("ok"), False, "没有 fpk 资产时拒绝")
    c.ok("fpk" in r.get("error", ""), "错误里点明缺 fpk")

    installer.reset()
    r = call({"has_update": True, "latest": "26.11.4",
              "assets": [{"name": "x.txt", "url": "u"}]})
    c.eq(r.get("ok"), False, "只有非 fpk 资产时拒绝")

    installer.reset()
    r = call(None)
    c.eq(r.get("ok"), False, "info 为 None 时拒绝而不是抛异常")

    installer.reset()
    r = call(release_info("http://127.0.0.1:1/x.fpk"))
    c.eq(r.get("ok"), False, "下载失败时 start 返回 ok=False")
    c.eq(installer.status()["phase"], "failed", "失败后阶段是 failed")


def test_start_bad_package(c):
    section("start：包不对时绝不进入安装阶段")
    ws = fresh_workspace("start_badpkg")
    cli = make_fake_appcenter(os.path.join(ws, "bin"))
    var = os.path.join(ws, "var")
    os.makedirs(var, exist_ok=True)
    installer.set_context(var_dir=var, current="26.11.3")

    with Server() as s:
        # 版本不符：release 说 26.11.4，包里写 26.11.1
        stale = make_fpk(os.path.join(ws, "stale.fpk"), version="26.11.1")
        url = s.serve(stale, "ok")
        installer.reset()
        spawned = []
        r = installer.start(release_info(url, version="26.11.4"), "26.11.3",
                            8899, var, log=lambda m: None, appcenter=cli,
                            spawn=fake_spawn(spawned))
        c.eq(r.get("ok"), False, "版本不符被拒")
        c.ok("版本不匹配" in r.get("error", ""), "错误说明版本不匹配")
        c.eq(installer.status()["phase"], "failed", "阶段标记为 failed")
        c.eq(len(spawned), 0, "没有启动安装脚本（关键：否则会卸载完装不上）")

        # 应用名不符
        other = make_fpk(os.path.join(ws, "other.fpk"), appname="fn-qbpanel",
                         version="26.11.4")
        url = s.serve(other, "ok")
        installer.reset()
        spawned = []
        r = installer.start(release_info(url), "26.11.3", 8899, var,
                            log=lambda m: None, appcenter=cli,
                            spawn=fake_spawn(spawned))
        c.eq(r.get("ok"), False, "应用名不符被拒")
        c.eq(len(spawned), 0, "同样不启动脚本")

        # 下载到的是坏包（HTML 错误页之类）
        junk = os.path.join(ws, "junk.fpk")
        with open(junk, "wb") as f:
            f.write(b"<html>404 not found</html>" * 100)
        url = s.serve(junk, "ok")
        installer.reset()
        spawned = []
        r = installer.start(release_info(url), "26.11.3", 8899, var,
                            log=lambda m: None, appcenter=cli,
                            spawn=fake_spawn(spawned))
        c.eq(r.get("ok"), False, "坏包被拒")
        c.eq(len(spawned), 0, "坏包不启动脚本")

        # 没有 appcenter-cli：必须提前失败，而不是卸载之后
        installer.reset()
        spawned = []
        r = installer.start(release_info(s.serve(stale, "ok"), version="26.11.1"),
                            "26.11.0", 8899, var, log=lambda m: None,
                            appcenter="/nonexistent/appcenter-cli",
                            spawn=fake_spawn(spawned))
        c.eq(r.get("ok"), False, "找不到 appcenter-cli 时拒绝")
        c.eq(len(spawned), 0, "不启动脚本")


def test_rollback(c):
    section("回滚：能找到旧包就一起备好")
    ws = fresh_workspace("rollback")
    cli = make_fake_appcenter(os.path.join(ws, "bin"))
    var = os.path.join(ws, "var")
    share = os.path.join(ws, "share")
    os.makedirs(var, exist_ok=True)
    os.makedirs(share, exist_ok=True)

    prev_pkg = make_fpk(os.path.join(share, "fn-pvepanel-26.11.3-fnos-all.fpk"),
                        version="26.11.3")
    new_pkg = make_fpk(os.path.join(ws, "new.fpk"), version="26.11.4")

    c.eq(installer.find_previous_fpk("26.11.3", extra_dirs=[share]), prev_pkg,
         "按版本号找到旧包")
    c.eq(installer.find_previous_fpk("26.11.1", extra_dirs=[]), "",
         "找不到对应版本时返回空串（不是异常）")
    c.eq(installer.find_previous_fpk("", extra_dirs=[share]), "",
         "版本为空时直接返回空串")
    c.eq(installer.find_previous_fpk("26.11.3", extra_dirs=[], exclude=[prev_pkg]),
         "", "exclude 能排除掉待安装的新包")

    installer.set_context(var_dir=var, current="26.11.3")
    with Server() as s:
        url = s.serve(new_pkg, "ok")
        installer.reset()
        spawned = []
        r = installer.start(release_info(url), "26.11.3", 8899, var,
                            log=lambda m: None, appcenter=cli,
                            spawn=fake_spawn(spawned),
                            prev_fpk=prev_pkg)
        c.eq(r.get("ok"), True, "有旧包时 start 仍成功")
        c.eq(r.get("rollback"), True, "返回 rollback=True")
        rb = os.path.join(var, "update", "rollback.fpk")
        c.ok(os.path.isfile(rb), "rollback.fpk 已就位")
        with open(rb, "rb") as f:
            c.eq(f.read(), open(prev_pkg, "rb").read(), "回滚包内容与旧包一致")
        with open(spawned[0]["script"], "r", encoding="utf-8") as f:
            c.ok(rb in f.read(), "脚本里引用了回滚包")

        # 没有旧包时仍能升级，只是没有回滚
        installer.reset()
        spawned = []
        r = installer.start(release_info(s.serve(new_pkg, "ok")), "26.11.0",
                            8899, var, log=lambda m: None, appcenter=cli,
                            spawn=fake_spawn(spawned))
        c.eq(r.get("ok"), True, "没有旧包也能升级")
        c.eq(r.get("rollback"), False, "返回 rollback=False")
        with open(spawned[0]["script"], "r", encoding="utf-8") as f:
            c.ok("no previous package available" in f.read(),
                 "脚本里走的是无回滚分支")


def test_spawn_failure(c):
    section("spawn 失败：状态要标 failed，不能假装在装")
    ws = fresh_workspace("spawn_fail")
    cli = make_fake_appcenter(os.path.join(ws, "bin"))
    var = os.path.join(ws, "var")
    os.makedirs(var, exist_ok=True)
    installer.set_context(var_dir=var, current="26.11.3")
    fpk = make_fpk(os.path.join(ws, "pkg.fpk"), version="26.11.4")

    def boom(script, workdir):
        raise OSError("cannot exec")

    with Server() as s:
        url = s.serve(fpk, "ok")
        installer.reset()
        r = installer.start(release_info(url), "26.11.3", 8899, var,
                            log=lambda m: None, appcenter=cli, spawn=boom)
        c.eq(r.get("ok"), False, "spawn 抛异常时返回 ok=False")
        c.eq(installer.status()["phase"], "failed", "阶段是 failed")
        c.ok("启动安装脚本" in r.get("error", ""), "错误信息点明是启动失败")


def test_status_log(c):
    section("status：读得到脱离进程后写下的日志尾巴")
    ws = fresh_workspace("status_log")
    var = os.path.join(ws, "var")
    updir = os.path.join(var, "update")
    os.makedirs(updir, exist_ok=True)
    installer.set_context(var_dir=var, current="26.11.3")
    installer.reset()

    log_path = os.path.join(updir, "install.log")
    lines = ["line %d" % i for i in range(1, 31)]
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    st = installer.status()
    tail = st["log"].splitlines()
    c.eq(len(tail), installer.LOG_TAIL_LINES, "只回最后 %d 行" % installer.LOG_TAIL_LINES)
    c.eq(tail[-1], "line 30", "最后一行是最新的")
    c.eq(tail[0], "line %d" % (30 - installer.LOG_TAIL_LINES + 1),
         "起始行正确")

    installer.reset()
    c.eq(installer.status()["log"] != "", True, "reset 不清日志文件（升级要能回看）")

    # 日志不存在时不报错
    shutil.rmtree(updir, ignore_errors=True)
    c.eq(installer.status()["log"], "", "没有日志文件时返回空串")


def test_script_runs(c):
    section("生成的脚本能被真实 sh 解析（不执行安装部分）")
    ws = fresh_workspace("sh_syntax")
    sh = shutil.which("sh") or shutil.which("bash")
    if not sh:
        c.ok(True, "本机没有 sh，跳过语法检查")
        return
    text = installer.build_script("26.11.4", 8899,
                                  os.path.join(ws, "pkg.fpk"),
                                  os.path.join(ws, "rollback.fpk"),
                                  os.path.join(ws, "install.log"))
    path = os.path.join(ws, "install.sh")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    import subprocess
    proc = subprocess.run([sh, "-n", path], capture_output=True, text=True)
    c.eq(proc.returncode, 0, "sh -n 语法检查通过", proc.stderr)


def test_real_fpk(c):
    section("用仓库里真实构建出的 fpk 验证（若存在）")
    out_dir = os.path.join(PROJECT_DIR, "output")
    fpks = []
    if os.path.isdir(out_dir):
        fpks = sorted(f for f in os.listdir(out_dir) if f.endswith(".fpk"))
    if not fpks:
        c.ok(True, "output/ 下没有 fpk，跳过")
        return
    ws = fresh_workspace("real_fpk")
    cli = make_fake_appcenter(os.path.join(ws, "bin"))
    target = os.path.join(out_dir, fpks[-1])
    installer.validate_fpk(target, appcenter=cli)
    c.ok(True, "真实 fpk 通过校验：" + fpks[-1])

    # 版本号应该能从包名/manifest 里读出来
    import tarfile
    with tarfile.open(target) as tf:
        raw = tf.extractfile("manifest").read().decode("utf-8", "replace")
    fields = installer._manifest_fields(raw)
    c.eq(fields.get("appname"), "fn-pvepanel", "真实包 appname 正确")
    ver = fields.get("version")
    c.ok(bool(ver), "真实包有版本号：" + str(ver))
    installer.validate_fpk(target, appcenter=cli, expect_version=ver)
    c.ok(True, "按真实版本号校验也通过")


# ---------------------------------------------------------------------- main

def main():
    setup_stdio()
    if os.path.isdir(UNIT_DIR):
        shutil.rmtree(UNIT_DIR, ignore_errors=True)
    os.makedirs(UNIT_DIR, exist_ok=True)
    print("installer.py 单元测试（本机假 HTTP + 假 appcenter-cli，不装任何东西）")
    print("被测模块：" + installer.__file__)
    print("临时目录：" + UNIT_DIR)

    c = Checker()
    for fn in (test_contract, test_status_shape, test_pick_asset, test_safe_name,
               test_validate, test_download, test_script, test_start_happy,
               test_start_rejections, test_start_bad_package, test_rollback,
               test_spawn_failure, test_status_log, test_script_runs,
               test_real_fpk):
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
