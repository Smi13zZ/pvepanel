#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""升级进度收敛规则的静态闸门（PVEpanel）。

这一份覆盖三组检查，缺一条都会让已经修好的问题回来：

  A. 进度收敛三条规则（阶段只前进 / total 只增 / attempt 隔离）
     —— 「点一键更新后卡在遮罩里」的直接成因
  B. 遮罩期间静默 + Release 正文走 Markdown + 安装日志只失败时展开
     —— 用户投诉「右下角一直弹 Failed to fetch」与「说明里全是 ##」
  C. 不确定态流动条 + 版本行图标对齐 + 检查按钮旋转图标
     —— 三处都是实测数值定下来的，删掉会以同样形态回来

用法：python3 tools/check_update_mono.py
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WEB = os.path.join(REPO, "src", "fn-pvepanel", "app", "web")
APP_JS = os.path.join(WEB, "js", "app.js")
CSS = os.path.join(WEB, "style.css")
HTML = os.path.join(WEB, "index.html")


def slice_function(text, marker):
    start = text.find(marker)
    if start < 0:
        return ""
    brace = text.find("{", start)
    if brace < 0:
        return ""
    depth = 0
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def check_monotonicity(src, fails):
    """A. 进度收敛三条规则。"""
    body = slice_function(src, "applyUpdateState(st)")
    if not body:
        fails.append("找不到 applyUpdateState")
        return
    if not re.search(r"checking\s*:\s*0", body):
        fails.append("规则 1 缺少阶段顺序表")
    if "nextRank < prevRank" not in body:
        fails.append("规则 1 缺少「后退快照丢弃」")
    if "updateMonoAttempt" not in body:
        fails.append("规则 3 缺少 attempt 基线")
    if "attempt !== this.updateMonoAttempt" not in body:
        fails.append("规则 3 缺少「attempt 变了就重置」")
    if 'rawPhase === "downloading"' not in body:
        fails.append("规则 2 缺少 downloading 分支")
    incr = ("Math.max(this.updateProgressTotal || 0, st.total)" in body
            or re.search(r"if\s*\(\s*total\s*>\s*this\.updateProgressTotal\s*\)", body))
    if not incr:
        fails.append("规则 2 下载阶段没有让 total 单调递增")
    if "update.verifying" not in body:
        fails.append("缺少校验阶段文案")
    if "reloadWhenBack" not in src:
        fails.append("缺少 reloadWhenBack（装完直接 reload 会撞进离线窗口）")
    finish = slice_function(src, "finishUpdate(version)")
    if finish and "reloadWhenBack" not in finish:
        fails.append("finishUpdate 没有走 reloadWhenBack")


def check_quiet_and_notes(src, fails, css, html):
    """B. 遮罩期间静默 / Markdown 渲染 / 安装日志。"""
    # 1) 遮罩存在期间不许弹轮询错误
    if not re.search(r"^\s*quietPollError\(e\)\s*\{", src, re.M):
        fails.append("缺少 quietPollError 定义（遮罩期间会连弹轮询错误）")
    else:
        q = slice_function(src, "quietPollError(e)")
        if "updateInstalling" not in q:
            fails.append("quietPollError 没有按 updateInstalling 静默")
    if len(re.findall(r"this\.quietPollError\(e\)", src)) < 3:
        fails.append("quietPollError 调用点不足（bootstrap / refresh / loadMaint 都要）")
    if "async refresh(force)" in src:
        r = slice_function(src, "async refresh(force)")
        if "updateInstalling" not in r:
            fails.append("refresh 没有在升级期间直接返回")

    # 2) 网络层异常不许把浏览器英文串丢给用户
    if "netErrText" not in src:
        fails.append("缺少 netErrText（fetch 的英文异常会原样显示给用户）")

    # 3) 装完不再弹第二条 toast
    finish = slice_function(src, "finishUpdate(version)")
    if finish and "this.toast(" in finish:
        fails.append("finishUpdate 仍在弹 toast（遮罩已显示同一句话）")

    # 4) Release 正文走 md.js
    if "updateNotesHtml" not in src:
        fails.append("缺少 updateNotesHtml（Release 正文没有走 Markdown 渲染）")
    if "md.js" not in html:
        fails.append("index.html 没有引入 md.js")
    if not os.path.isfile(os.path.join(WEB, "js", "md.js")):
        fails.append("缺少 web/js/md.js")

    # 5) 安装日志只在失败时展开
    if "updateInstallLog" not in html:
        fails.append("index.html 没有安装日志块")
    for line in html.splitlines():
        if "upinstalllog" in line and "v-if=" in line and "updateError" not in line:
            fails.append("安装日志没有绑在 updateError 上（升级途中会一直显示）")
            break

    # 6) 不再有第二条 /api/health 轮询
    if "waitForUpgrade" in src:
        fails.append("waitForUpgrade 又回来了（会和进度轮询抢 updateInstallDetail）")


def check_indeterminate_and_verline(css):
    """C. 不确定态流动条 + 版本行对齐 + 检查按钮旋转。"""
    fails = []
    if not css:
        fails.append("找不到 style.css，无法核对遮罩样式")

    # 1) 不确定态：一条会动的条，不是静态文字
    if not re.search(r"\.upprog\.indeterminate\s*\{[^}]*\}", css):
        fails.append("缺少 .upprog.indeterminate 规则（不确定态退回静态文字）")
    if not re.search(r"\.upprog\.indeterminate\s+\.upprog-flow\s*\{[^}]*\}", css):
        fails.append("缺少 .upprog-flow 规则（那条流动的亮条）")
    if not re.search(r"@keyframes\s+upprogFlow\s*\{", css):
        fails.append("缺少 upprogFlow 动画（条不会动，等于还是静态）")
    m = re.search(r"\.upprog\.indeterminate\s+\.upprog-flow\s*\{([^}]*)\}", css)
    if m:
        body = m.group(1)
        if "animation" not in body:
            fails.append(".upprog-flow 没有挂动画")
        if "var(--primary)" not in body:
            fails.append("不确定条没有用 var(--primary)（夜间模式不会反色）")
        for hard in ("#3b82f6", "#2563eb", "#000", "#111", "rgb(0, 0, 0)"):
            if hard in body:
                fails.append("不确定条写死了颜色 %s（夜间模式不会反色）" % hard)
    rm = re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{"
                   r"[\s\S]{0,400}?\.upprog-flow[\s\S]{0,200}?\}", css)
    if not rm:
        fails.append("缺少 prefers-reduced-motion 兜底（前庭敏感用户会不适）")
    elif "animation: none" not in rm.group(0):
        fails.append("reduced-motion 兜底里没有关掉动画")

    # 2) 版本行对齐
    #    只对齐中线不够：14px 的地球墨迹有 11.8px 高、文字只有 8.2px，
    #    上下各超出 1.75px，看起来像图标沉下去了。11px 时两者同高
    #    （9.2 vs 8.2），中线差 0.0，上下超出对称在 0.5px 内。
    if not re.search(r"\.ver-line\s*\{[^}]*line-height:\s*16px", css):
        fails.append("版本行没有 line-height: 16px（图标会比文字低 2px）")
    # 注意：样式表里还有一条 `.side .ver-line svg { stroke-width }` 排在前面，
    # 用单次 re.search 会先命中它（它不含 width），于是永远判失败。
    # 这里取全部同名规则，只要有一条同时带 width 与 translateY 就通过。
    rules = re.findall(r"\.ver-line\s+svg\s*\{([^}]*)\}", css)
    if not rules:
        fails.append("找不到 .ver-line svg 规则")
    elif not any("width: 11px" in b and "translateY(-1.5px)" in b for b in rules):
        joined = " | ".join(b.strip()[:70] for b in rules)
        fails.append("版本行图标不是 11px / translateY(-1.5px)（实际：%s）" % joined)

    # 3) 「检查更新」按钮的旋转图标
    # 注意两个坑（都是变异测试抓出来的）：
    #   a) reduced-motion 里也有一条同选择器的 `{ animation: none; }`，
    #      用单次 search 会先命中它，主规则删掉也能过 —— 必须要求动画名是 checkSpin。
    #   b) 关键帧用 `in` 做子串判断，改名成 checkSpinXX 仍是前缀，必须正则 + 边界。
    spin_rules = re.findall(r"\.btn\s+\.ic\.spinning\s+svg\s*\{([^}]*)\}", css)
    if not any("checkSpin" in b and "animation" in b for b in spin_rules):
        fails.append("检查按钮缺少 .btn .ic.spinning 的 checkSpin 旋转动画")
    if not re.search(r"@keyframes\s+checkSpin\s*\{", css):
        fails.append("缺少 checkSpin 关键帧（图标不会转）")
    if not re.search(r"prefers-reduced-motion[\s\S]{0,400}?spinning[\s\S]{0,200}?animation:\s*none",
                     css):
        fails.append("缺少 spinning 的 reduced-motion 兜底")

    # 4) 按钮图标容器必须有显式尺寸
    #    有的产品的图标是 <svg viewBox="0 0 24 24"> 不带 width/height 属性，
    #    尺寸全靠 CSS；没有这条规则会塌成 0，按钮上只剩文字。
    if not re.search(r"\.btn\s+\.ic\s*\{[^}]*width:\s*16px", css):
        fails.append("缺少 .btn .ic 的显式尺寸（图标宽度会塌成 0）")

    return fails


def main():
    src = io.open(APP_JS, encoding="utf-8").read()
    css = io.open(CSS, encoding="utf-8").read() if os.path.isfile(CSS) else ""
    html = io.open(HTML, encoding="utf-8").read() if os.path.isfile(HTML) else ""

    fails = []
    check_monotonicity(src, fails)
    if fails:
        for f in fails:
            print("FAIL", f)
        return 1

    check_quiet_and_notes(src, fails, css, html)
    if fails:
        for f in fails:
            print("FAIL", f)
        return 1

    extra = check_indeterminate_and_verline(css)
    if extra:
        for item in extra:
            print("FAIL", item)
        return 1

    print("OK   升级进度收敛规则齐备（阶段只前进 / total 只增 / attempt 隔离）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
