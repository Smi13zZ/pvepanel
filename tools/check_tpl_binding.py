#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模板与定义方式必须匹配。

Vue 的 computed 在模板里按属性访问，methods 必须加括号调用。
eMule 的 updateNotesHtml 定义在 methods 里，模板写成属性访问时
渲染出来是 `function () { [native code] }` —— 这个坑本仓已经踩过一次，
所以固化成断言。
"""
import io
import os
import sys

CASES = [
    # (模板, js, 名字, 期望调用方式 "call" 或 "prop")
    (r"D:\Workspace\DSH\fn-emule\src\emule\app\web\index.html",
     r"D:\Workspace\DSH\fn-emule\src\emule\app\web\js\app.js",
     "updateNotesHtml", "call"),
    (r"D:\Workspace\DSH\qbpanel-maintenance\src\qbpanel\qbpanel\app\web\index.html",
     r"D:\Workspace\DSH\qbpanel-maintenance\src\qbpanel\qbpanel\app\web\js\app.js",
     "updateNotes", "prop"),
    (r"D:\Workspace\DSH\pve-panel\src\fn-pvepanel\app\web\index.html",
     r"D:\Workspace\DSH\pve-panel\src\fn-pvepanel\app\web\js\app.js",
     "updateNotesHtml", "prop"),
    (r"D:\Workspace\DSH\fn-fail2ban-upgrade\src\pkg\fn-fail2ban\app\web\app.js",
     r"D:\Workspace\DSH\fn-fail2ban-upgrade\src\pkg\fn-fail2ban\app\web\app.js",
     "renderNotes(info.notes)", "call"),
]

fails = []

for tpl, js, name, want in CASES:
    t = io.open(tpl, encoding="utf-8").read()
    if want == "call":
        # Fail2ban 那一项名字里本身带参数，别再补一对括号
        needle = name if name.endswith(")") else name + "()"
        if needle not in t:
            fails.append("%s 模板没有以 %s 调用" % (os.path.basename(tpl), needle))
        bare = name.split("(")[0]
        if "v-html=\"" + bare + "\"" in t:
            fails.append("%s 模板把 %s 当属性用（methods 必须加括号）"
                         % (os.path.basename(tpl), bare))
    else:
        if "v-html=\"" + name + "\"" not in t:
            fails.append("%s 模板没有以属性方式引用 %s" % (os.path.basename(tpl), name))

    s = io.open(js, encoding="utf-8").read()
    ci = s.find("computed:")
    mi = s.find("methods:")
    i = s.find(name)
    if i >= 0 and ci >= 0 and mi >= 0:
        where = "computed" if ci < i < mi else ("methods" if i > mi else "?")
        want_where = "methods" if want == "call" else "computed"
        if where != want_where:
            fails.append("%s 定义在 %s，模板按 %s 用 —— 不匹配"
                         % (name, where, "方法调用" if want == "call" else "属性"))

if fails:
    for f in fails:
        print("FAIL", f)
    sys.exit(1)
print("OK   模板与定义方式一致（computed 当属性 / methods 加括号）")
