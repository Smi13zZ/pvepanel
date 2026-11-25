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
    #    这一条踩过两次，两次都是「看着齐了、换个字就不齐」：
    #    a) 只对齐墨迹中线：中线确实重合，但 14px 的地球墨迹比文字高 3.6px，
    #       上下各超出近 2px，看着像图标沉下去；
    #    b) 换成固定补偿 translateY(-1.5px)：那个像素值是照一种等宽字标定的，
    #       换一种等宽字，数字墨迹从 8.17px 变 8.71px，偏移立刻失准（实测偏 1.12px）。
    #    现在要求：对齐基线，尺寸与偏移一律用 em，并且禁止固定像素尺寸。
    if not re.search(r"\.ver-line\s*\{[^}]*line-height:\s*16px", css):
        fails.append("版本行没有 line-height: 16px（行盒比字形高小半行，图标会偏）")
    if not re.search(r"\.ver-line\s*\{[^}]*align-items:\s*baseline", css):
        fails.append("版本行没有 align-items: baseline（固定像素补偿会随字体失准）")
    rules = re.findall(r"\.ver-line\s+svg\s*\{([^}]*)\}", css)
    if not rules:
        fails.append("找不到 .ver-line svg 规则")
    else:
        if not any("em" in b and "translateY(" in b for b in rules):
            joined = " | ".join(b.strip()[:70] for b in rules)
            fails.append("版本行图标尺寸/偏移没用 em（换一种字就会偏：%s）" % joined)
        # 固定像素是那两次出错的共同形态，抓死它
        fixed = [b for b in rules
                 if re.search(r"(?:^|[;{\s])(?:width|height|flex)\s*:\s*[\d.]+px", b)]
        if fixed:
            fails.append("版本行图标写回了固定像素尺寸（%s）" % fixed[0].strip()[:70])
    # 图标盒子的尺寸也必须用 em：只改 svg、不改盒子，盒子仍按固定 px 占位，
    # 图标会被挤在中间，居中对齐时又回到「看着偏」的老问题。
    ic_rules = re.findall(r"\.ver-line\s+\.ic\b[^{]*\{([^}]*)\}", css)
    if not any("em" in b for b in ic_rules):
        fails.append("版本行图标盒子的尺寸没用 em（只改 svg 不够）")

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


    fails.extend(check_app_shell(css))
    fails.extend(check_notify_section_order())

    return fails


def check_notify_section_order():
    """通知区的结构：通知（事件开关）→ 通知渠道 → 投递记录。

    用户明确要求过这个顺序和形态，回退会立刻可见，所以锁死：
      1. 第一张卡是「通知」，事件用整行开关（swrow），不是两列勾选网格
      2. 第二张卡是「通知渠道」，必须出现在投递记录之前
      3. 通道行用 chrow（圆形图标位 + 名称/元信息 + 三个按钮）
      4. 「通知渠道」那张卡必须带「添加通道」按钮
    """
    fails = []
    web = WEB
    html_path = os.path.join(web, "index.html")
    css_path = os.path.join(web, "style.css")
    if not os.path.isfile(html_path):
        return ["找不到 index.html，无法核对通知区结构"]
    html = io.open(html_path, encoding="utf-8", errors="replace").read()
    css = (io.open(css_path, encoding="utf-8", errors="replace").read()
           if os.path.isfile(css_path) else "")

    # 只在通知分区里判断，别把别的分区的 card 算进来
    start = html.find("setTab === 'notify'")
    end = html.find("setTab === 'update'")
    if start < 0 or end < start:
        return ["找不到通知分区（setTab === 'notify' 到 update 之间的模板）"]
    seg = html[start:end]

    i_events = seg.find("t('set.notifyEvents')")
    i_channels = seg.find("t('set.notifyChannels')")
    i_log = seg.find("t('notifyLog.title')")

    if i_events < 0:
        fails.append("通知分区缺少事件卡（set.notifyEvents）")
    if i_channels < 0:
        fails.append("通知分区缺少「通知渠道」卡（set.notifyChannels）")
    if i_log < 0:
        fails.append("通知分区缺少投递记录卡（notifyLog.title）")
    if i_events >= 0 and i_channels >= 0 and i_log >= 0:
        if not (i_events < i_channels < i_log):
            fails.append("通知区卡片顺序不对（应为 通知 → 通知渠道 → 投递记录）")

    # 事件必须是整行开关，不能退回两列勾选网格
    if 'class="evgrid"' in seg:
        fails.append("事件又用回两列勾选网格了（应为整行开关 swrow）")
    if 'class="swrow"' not in seg:
        fails.append("事件没有用整行开关（swrow）")

    # 通道行必须是 chrow，且带圆形图标位
    if 'class="chrow"' not in seg:
        fails.append("通道行没用 chrow（应为圆形图标 + 名称 + 按钮）")
    if 'class="chico"' not in seg:
        fails.append("通道行缺少 chico 圆形图标位")

    # 「通知渠道」卡要有添加按钮
    if i_channels >= 0:
        tail = seg[i_channels:i_channels + 700]
        if "set.notifyAdd" not in tail:
            fails.append("「通知渠道」卡缺少添加通道按钮")

    # 样式必须存在（否则结构对了也是一团乱）。
    # 注意不能只判「选择器出现过」：移动端 @media 里也有 `.chico { order: 1 }`，
    # 主规则被删掉时那条还留着，断言照样通过 —— 变异测试抓过这个洞。
    # 所以每个类都要有一条含实质声明的规则。
    REQUIRED = {
        ".swrow": r"border-bottom|padding",
        ".swtext": r"flex|min-width",
        ".swname": r"font-size|font-weight",
        ".chlist": r"flex-direction|gap",
        ".chrow": r"padding|border-radius",
        ".chico": r"width|height|border-radius",
        ".chname": r"font-size|font-weight",
        ".chmeta": r"font-size|color",
    }
    for cls, need in REQUIRED.items():
        rules = re.findall(re.escape(cls) + r"\s*\{([^}]*)\}", css)
        if not rules:
            fails.append("style.css 缺少 %s 规则（通知卡样式不全）" % cls)
        elif not any(re.search(need, b) for b in rules):
            fails.append("style.css 的 %s 规则没有实质声明（可能只剩移动端覆盖）"
                         % cls)

    return fails


def check_app_shell(css):
    """应用壳式布局：文档高度必须固定为视口高度。

    宿主按文档高度给应用框定尺寸，而外壳的 `min-height: 100vh` 又让文档随内容
    长高 —— 两者互相喂，框越撑越高，钉在 100vh 底边的侧栏底部被推出屏幕，
    概览页只剩上方一小块内容、下面一大片空白。

    实测（403x1155 视口）修复前各页文档高度：统计 4313 / 网络诊断 1974 /
    概览 1603 —— 分别 3.7 / 1.7 / 1.4 屏；修复后全部等于 1155。

    四条规则缺一不可，任一条缺失都会让文档重新长高：
      1. html, body 不能滚动（否则文档本身就能比视口高）
      2. 外壳用 height 而不是 min-height —— min-height 正是那个放大器
      3. 主栏的 min-height:0 —— flex 子项默认 min-height:auto 会拒绝缩小
      4. 内容区承担滚动，同样要 min-height:0
    """
    fails = []

    # 1) html, body 锁滚动
    rules = re.findall(r"html,\s*body\s*\{([^}]*)\}", css)
    if not rules:
        fails.append("找不到 html, body 规则")
    elif not any(re.search(r"overflow:\s*hidden", b) for b in rules):
        fails.append("html, body 没有 overflow: hidden（文档自己就能比视口高）")

    # 2) 外壳固定高度
    shell = re.findall(r"(?<![-\w.])\.shell\s*\{([^}]*)\}", css)
    if not shell:
        fails.append("找不到 %s 规则" % '.shell')
    else:
        body = " ".join(shell)
        if not re.search(r"(?<![-\w])height:\s*100vh", body):
            fails.append("%s 没有固定 height: 100vh（壳会随内容长高）" % '.shell')
        if re.search(r"min-height:\s*100vh", body):
            fails.append("%s 用回了 min-height: 100vh —— 那正是把页面越撑越长的原因"
                         % '.shell')
        if not re.search(r"min-height:\s*0", body):
            fails.append("%s 缺少 min-height: 0（会被内容顶高）" % '.shell')

    # 3) 主栏 min-height:0
    main = re.findall(r"(?<![-\w.])\.main\s*\{([^}]*)\}", css)
    if not main:
        fails.append("找不到 %s 规则" % '.main')
    elif not any(re.search(r"min-height:\s*0", b) for b in main):
        fails.append("%s 缺少 min-height: 0（flex 子项默认 auto，内容仍会撑高壳）"
                     % '.main')

    # 4) 内容区承担滚动
    content = re.findall(r"(?<![-\w.])\.content\s*\{([^}]*)\}", css)
    if not content:
        fails.append("找不到 %s 规则" % '.content')
    else:
        if not any(re.search(r"overflow-y:\s*auto", b) for b in content):
            fails.append("%s 没有 overflow-y: auto（长内容无处可滚，只能撑高文档）"
                         % '.content')
        if not any(re.search(r"min-height:\s*0", b) for b in content):
            fails.append("%s 缺少 min-height: 0（长内容会顶高主栏）" % '.content')

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
