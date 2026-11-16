#!/usr/bin/env python3
"""check_update_ui.py - the one-click update UI contract, asserted statically.

Two classes of defect this catches, both of which compile and render fine:

  * a phase or a failure code the frontend cannot translate - the server sends
    a language-neutral token and the page is responsible for the sentence, so a
    missing key shows up as a raw "update.code.noAsset" on screen;
  * the "100 B / 100 B" frame: the progress counters are BYTES, and writing a
    literal 100 into them renders a fake finished download.

The front-end is a plain script with no build step, so this reads the sources
instead of running a browser. Anything that needs an actual render is covered by
tools/console_check.py on a device.

Run:  python tools/check_update_ui.py
"""
import os
import re
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS_DIR)
WEB = os.path.join(ROOT, "src", "fn-pvepanel", "app", "web")
SERVER = os.path.join(ROOT, "src", "fn-pvepanel", "app", "server")

FAILS = []
N = [0]


def check(name, cond, detail=""):
    N[0] += 1
    if cond:
        print("  ok   %s" % name)
        return True
    FAILS.append(name)
    print("  FAIL %s  %s" % (name, detail))
    return False


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def i18n_tables():
    """Split i18n.js into its zh and en key sets."""
    src = read(os.path.join(WEB, "js", "i18n.js"))
    at = src.find("en: {")
    zh = set(re.findall(r'^\s*"([A-Za-z][A-Za-z0-9_.]*)"\s*:', src[:at], re.M))
    en = set(re.findall(r'^\s*"([A-Za-z][A-Za-z0-9_.]*)"\s*:', src[at:], re.M))
    return zh, en


def installer_codes():
    """Every code the backend can hand the browser."""
    src = read(os.path.join(SERVER, "installer.py"))
    codes = set(re.findall(r'UpdateError\(\s*"([A-Za-z]+)"', src))
    codes |= set(re.findall(r'_refuse\(\s*"([A-Za-z]+)"', src))
    codes |= set(re.findall(r'return "(net[A-Z][A-Za-z]*|gh[0-9]+|checkFailed)"', src))
    # update.py adds the release-lookup side
    up = read(os.path.join(SERVER, "update.py"))
    # Only real codes, not reason names: "http"/"timeout"/"network" are the
    # fallback chain's own vocabulary and are translated through
    # update.detail.<reason>, not through update.code.<code>.
    codes |= set(re.findall(r'code="([A-Za-z0-9]+)"', up))
    reason_map = re.search(r"_REASON_CODE = \{(.*?)\n\}", up, re.S)
    if reason_map:
        codes |= set(re.findall(r':\s*"([A-Za-z0-9]+)"', reason_map.group(1)))
    return set(c for c in codes if c)


def main():
    zh, en = i18n_tables()
    app = read(os.path.join(WEB, "js", "app.js"))
    html = read(os.path.join(WEB, "index.html"))
    inst = read(os.path.join(SERVER, "installer.py"))

    print("[阶段：服务端与界面用同一套名字]")
    phases = re.findall(r'PHASES = \((.*?)\)', inst)[0]
    server_phases = set(re.findall(r'"([a-z]+)"', phases))
    for phase in ("checking", "downloading", "installing", "failed"):
        check("服务端阶段 %s 存在" % phase, phase in server_phases, server_phases)
    # 每个非终态阶段都要有界面自己的句子
    for phase, key in (("checking", "update.checking"),
                       ("downloading", "update.downloading"),
                       ("installing", "update.restarting")):
        check("阶段 %s 有界面文案 %s" % (phase, key), key in zh and key in en)
        check("阶段 %s 的文案在 app.js 里被用到" % phase,
              ('t("%s")' % key) in app, key)

    print("[失败提示：任意页面可见 + 可关闭]")
    check("存在独立的失败条 .upfail", 'class="upfail"' in html)
    check("失败条绑定的不是设置页的 updateError",
          "updateFailText" in html and "{{ updateFailText }}" in html)
    check("失败条有「查看详情」跳转", "openUpdateSection" in html)
    check("失败条可以单独关闭", "dismissUpdateFail" in html)
    check("失败条只在遮罩收起后显示（不叠在升级弹窗上）",
          'v-if="updateFailText && !updateInstalling"' in html)
    check("updateFailText 在 app.js 里被赋值",
          "this.updateFailText = this.updateError" in app)
    check("dismissUpdateFail 会清掉它", 'this.updateFailText = ""' in app)

    print("[错误码：中英都能翻译，不缺键]")
    codes = installer_codes()
    check("后端至少给出 10 个码", len(codes) >= 10, sorted(codes))
    for code in sorted(codes):
        key = "update.code." + code
        check("中文有 %s" % key, key in zh)
        check("英文有 %s" % key, key in en)
    check("界面按 code 取文案（不是直显 detail）",
          'updateCodeText' in app and '"update.code." + code' in app)
    check("取不到 code 时回落 detail", "|| fallback" in app or "|| st.detail" in app)
    check("检查失败的结论也优先用 code",
          'this.updateCodeText(info.error_code || "")' in app)

    print("[attempt：失败状态不跨尝试复用]")
    check("前端记住基线 updateIgnoreAttempt", "updateIgnoreAttempt" in app)
    check("发安装请求前先读一次 status",
          app.find("await API.updateStatus()") < app.find("await API.installUpdate()"))
    check("轮询里跳过 <= 基线的失败", "st.attempt <= self.updateIgnoreAttempt" in app)
    check("安装返回值里的 attempt 也更新基线",
          'typeof r.attempt === "number"' in app)
    check("服务端 status 下发 attempt", '"attempt": 0' in inst)
    check("服务端每次尝试递增", '_STATE["attempt"] = int(_STATE.get("attempt") or 0) + 1' in inst)
    check("进行中判定含 checking", "ACTIVE_PHASES" in inst and
          'if phase in ACTIVE_PHASES' in inst)
    check("checking 在下载之前发布",
          inst.find('_STATE["phase"] = "checking"') < inst.find('_set("downloading"'))

    print("[成功帧：字节是字节，不是百分比]")
    check("成功帧不再写 100/100", "updateProgressDone = 100" not in app
          and "updateProgressTotal = 100" not in app)
    # finishUpdate must leave the byte counters exactly as the download left
    # them: they are bytes, and any "make it look complete" assignment here is
    # the original 100 B / 100 B defect coming back.
    body = app.split("finishUpdate(version) {")[1].split("\n      },", 1)[0]
    check("成功帧不动字节计数器",
          "updateProgressDone" not in body and "updateProgressTotal" not in body,
          body[:200])
    check("安装阶段把 total 清零（退回准备中）",
          "this.updateProgressTotal = 0" in app)
    check("遮罩用真实 done/total 渲染字节",
          "FMT.fmtBytes(updateProgressDone)" in html
          and "FMT.fmtBytes(updateProgressTotal)" in html)
    check("没有总量时显示「准备中…」",
          "update.progressUnknown" in html or "progressUnknown" in html)

    print("[回滚包：找不到就如实说]")
    check("服务端 status 下发 rollback 可用性",
          'out["rollback"]' in inst and '"rollback.fpk"' in inst)
    check("安装前把包实际落盘的目录放在搜索第一位",
          'updir = _update_dir(var_dir)' in inst
          and inst.find("dirs.append(updir)") < inst.find("dirs.extend(extra_dirs)"))
    check("找不到回滚包时只取最新的一份（按 mtime）",
          "best_t" in inst and "os.path.getmtime" in inst)
    check("界面有无回滚包的说明", "update.noRollback" in zh and "update.noRollback" in en)
    check("说明绑定在 updateRollbackReady 为 false 时",
          'v-if="updateNew && !updateRollbackReady"' in html)

    print("[存储页：行标签由界面自取，不下发中文]")
    storage = read(os.path.join(SERVER, "storage.py"))
    check("LAYOUT 不再带 label 列", "更新包缓存" not in storage)
    for key in ("updates", "backups", "panel_log", "panel_log1", "update_log"):
        check("storage.item.%s 中英都在" % key,
              ("storage.item." + key) in zh and ("storage.item." + key) in en)
    check("界面按 key 查表", '"storage.item." + it.key' in app)

    print("[遮罩文案]")
    for key in ("update.installingTitle", "update.keepOpen", "update.done",
                "update.rolledBack", "update.installTimeout",
                "update.installFailed"):
        check("遮罩/结果文案 %s 中英都在" % key, key in zh and key in en)

    print("")
    if FAILS:
        print("FAILED %d / %d" % (len(FAILS), N[0]))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("passed %d / %d" % (N[0], N[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
