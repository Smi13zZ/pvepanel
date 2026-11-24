#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_clean_log 的单元测试：appcenter-cli 的转圈帧必须被滤掉，真日志必须留下。

用真实设备上的 install.log 片段当 fixture —— 那是最容易出错的输入
（`\r` 覆写 + 中文 + 缩进）。
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRV = os.path.join(HERE, "..", "src", "fn-pvepanel", "app", "server")
sys.path.insert(0, os.path.abspath(SRV))

RAW = (
    "=== 2026-11-20 16:03:45 升级到 26.11.99 ===\n"
    "=== stop fn-fail2ban ===\n"
    "\r\\ stopping.\r| stopping.\r/ stopping.\r- stopping.\r\\ stopping.\n"
    "\r| stopping..\r/ stopping..\r- stopping..\r\\ stopping..\n"
    "[Info]Application [fn-fail2ban] stop success\n"
    "\r\\ Verifying files.\r| Verifying files.\r/ Verifying files.\n"
    "[Info]Installation in progress with /vol1/@apphome/x/update.env\n"
    "\r\\ installing.\r| installing.\r/ installing.\n"
    "\r\\ starting.\r| starting.\r/ starting.\n"
    "[Info]Installation complete.\n"
    "=== 已升级到 26.11.99 ===\n"
    "\n\n"
    "   \n"
)

fails = []
passes = [0]


def ok(name, cond, extra=""):
    if cond:
        passes[0] += 1
    else:
        fails.append(name + ((" → " + str(extra)) if extra else ""))


def main():
    from installer import _clean_log
    rows = _clean_log(RAW)

    text = "\n".join(rows)
    ok("保留中文头部", "升级到 26.11.99" in text, text)
    ok("保留 stop 标记", "=== stop fn-fail2ban ===" in rows, rows)
    ok("保留 Info 行", any(r.startswith("[Info]") for r in rows), rows)
    ok("保留三条 Info", sum(1 for r in rows if r.startswith("[Info]")) == 3, rows)
    ok("保留结论", any("已升级到" in r for r in rows), rows)

    bad = [r for r in rows if "stopping" in r.lower() or "installing." in r.lower()
           or "starting." in r.lower() or "Verifying files" in r]
    ok("转圈帧全部滤掉", not bad, bad)
    ok("空行不占位", all(r.strip() for r in rows), rows)
    ok("行数合理", 5 <= len(rows) <= 8, len(rows))
    ok("没有 \r 残留", all("\r" not in r for r in rows), rows)

    # 空输入与 None 不能炸
    ok("空串", _clean_log("") == [], _clean_log(""))
    ok("None", _clean_log(None) == [], _clean_log(None))

    # 纯符号行丢掉，但带内容的中文行（哪怕很短）要留下
    ok("纯符号行丢弃", _clean_log("- - -") == [], _clean_log("- - -"))
    ok("短内容行保留", _clean_log("完成") == ["完成"], _clean_log("完成"))

    if fails:
        print("FAIL %d / %d" % (len(fails), len(fails) + passes[0]))
        for f in fails:
            print("  - " + f)
        return 1
    print("OK %d assertions" % passes[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
