# pve-panel 工具链

三个可反复调用的工具，全部在本机 Windows 运行、通过 SSH 驱动 fnos-test。
本机没有 python/node，用 DSH 自带运行时：

```powershell
$PY = "C:\Users\PVE\.dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\python\python.exe"
cd "D:\Workspace\DeepSeek Harness\pve-panel"
```

> 单条同步命令有 ~120 秒硬上限。这三个工具自己把远程操作压在几十秒内完成，
> 不需要放后台；只有一次连拍很多张超大页面时才可能接近上限。

---

## 0. pve-test.py —— 在测试 PVE 上跑命令

测试用的 PVE 是一台跑在 `fnos-test` 里的 libvirt 虚拟机（真实 PVE 8.4.21），
在 NAT 网络 `192.168.122.0/24` 里，只有 NAS 能直连。这个脚本封装了两跳 SSH：

```powershell
& $PY tools\pve-test.py "qm list"
& $PY tools\pve-test.py "pveversion -v"
& $PY tools\pve-test.py --shell
```

环境细节见 [../docs/TEST-ENV.md](../docs/TEST-ENV.md)。

---

## 1. build_fpk.py —— 打 fpk

在 fnos-test 上打包（**必须**在 Linux 上打：Windows 会把文件权限全变成 0666，
安装报 10111），产物拉到 `pve-panel/output/`。

```powershell
& $PY tools\build_fpk.py
& $PY tools\build_fpk.py --host fnos-test
& $PY tools\build_fpk.py --no-verify          # 跳过包内 manifest 自检
```

做的事：
1. 读 `src/fn-pvepanel/manifest` 的 appname / version / platform
2. 本地打 tar（跳过 `__pycache__`、`.pyc`、`.git`、`*.log`、`*.fpk`）
3. scp 到 `fnos-test:/tmp/pvepanel-build`
4. 远端：去 CRLF → 删 pyc/junk → `chmod 755/644` → `chmod 755 cmd/* app/ui/index.cgi` → `fnpack build`
5. scp 回 `output/fn-pvepanel-<version>-fnos-<platform>.fpk`
6. 校验包内 manifest 的 appname/version 与源码一致、checksum 已生成

实测输出（26.11.0，200 KB 包）：

```
      tar pvepanel-src.tar (520.0 KB)
[1] source tar                 0.1s
[2] upload scp                 0.3s
[3] fnpack build               0.6s
[4] download scp               0.7s
[5] verify packed manifest     0.8s
TOTAL 0.8s
ARTIFACT ...\pve-panel\output\fn-pvepanel-26.11.0-fnos-all.fpk
```

---

## 2. deploy.py —— 装到 fnos-test

```powershell
& $PY tools\deploy.py                                     # 装 output/ 里最新的 fpk
& $PY tools\deploy.py --env wizard_backend_port=8899
& $PY tools\deploy.py --fpk output\fn-pvepanel-26.11.1-fnos-all.fpk
& $PY tools\deploy.py --no-uninstall                      # 干净机器首次安装
& $PY tools\deploy.py --no-install --timeout 60           # 只重启+探活，不重装
```

处理了三个飞牛特有的坑：
- **原地覆盖安装必失败（code 18000）** → 固定走 `stop → uninstall → install-fpk`；
- **`appcenter-cli start` 返回 10500 但服务其实起来了** → 完全不信返回码，
  自己轮询 `127.0.0.1:<port>/api/health` 直到 2xx（默认 60s 超时）；
- **卸载不删 `@appdata`** → 所以重装会继承配置，这是正常的迭代循环。

失败时不会只说 "timeout"：会打印 health 的原始响应、root 页可达性、
`@appdata/fn-pvepanel/pvepanel.log` 的最后 25 行、端口连通性。

成功时额外走一次 `index.cgi`（桌面 iframe 实际用的那条链路），确认 CGI 转发也通。

实测（26.11.0）：

```
[1] upload fpk                 0.2s
[2] install                   21.6s
[3] probe                     21.8s
HTTP 200
{"ok": true, "app": "fn-pvepanel", "version": "26.11.0", "python": "3.11.2", ...}
TOTAL 22.0s
DEPLOYED fn-pvepanel 26.11.0 on port 8899
```

---

## 3. shot.py —— 截图（最常用）

```powershell
& $PY tools\shot.py                                  # 桌面 1440x1000，默认打 :8899
& $PY tools\shot.py --preset all                     # 桌面 + 手机 + 深色 + 深色手机
& $PY tools\shot.py --preset mobile --dpr 2          # 390x844 的高清版
& $PY tools\shot.py --mobile                         # --preset mobile 的简写
& $PY tools\shot.py --size 1440x2200 --preset tall   # 长页面
& $PY tools\shot.py --out image\home.png             # 指定输出文件
& $PY tools\shot.py --expect "节点|集群"              # 额外断言渲染内容
& $PY tools\shot.py --url http://127.0.0.1:8771/x.html --budget 12000
```

三种模式由 `--preset` 给：

| preset | 尺寸 | 深浅色 |
| --- | --- | --- |
| `desktop` | 1440x1000 | 浅色 |
| `mobile` | 390x844 | 浅色 |
| `dark` | 1440x1000 | 深色 |
| `mobile-dark` | 390x844 | 深色 |
| `tall` | 1440x2200 | 浅色 |
| `all` | 上面前四个 | |

输出默认落 `pve-panel/image/<host>_<port>-<preset>.png`。

### 等首屏数据：三个必须知道的实测结论

**① `--screenshot` 和 `--dump-dom` 绝对不能放同一次运行。**
这是本次踩得最贵的一个坑：同一条命令里同时给这两个开关，
只要页面需要一点时间才画出来，Chromium 会静默产出一张**纯白图**——
在真实面板上实测，`shot+dom` 得到 6.4 KB 全白 1440x1000 PNG，
而完全相同的其它参数下单独 `--screenshot` 得到 100 KB 的完整渲染页。
所以 shot.py 对每张图跑两次 chromium：一次只截图，一次只 dump DOM
（代价约 +0.5s/张）。

**② `--virtual-time-budget` 确实会等 fetch。**
实测 fetch + `setTimeout(1200)` 的页面在 `--virtual-time-budget=6000` 下
能拿到渲染后的内容（DOM 里是 `READY ... data=payload-ok`），
且整体墙钟仍只有 ~0.5s。它是把定时器快进 + 等挂起的网络请求，不是死等。
默认 `--budget 8000`；数据接口慢时可以调大。

**③ 深色只剩一条路：`--force-dark-mode`。**
实测这台 Chromium 156 上：
- `--force-dark-mode` → `prefers-color-scheme: dark` **为 true**（解码 PNG 像素：16,16,16）
- `--blink-settings=preferredColorScheme=2` → **无效**（像素仍是 255,255,255，JS 里也是 false）

**④ Chromium 的 stdin 必须重定向掉。**
远程脚本是走 `ssh host 'bash -s'` 的 stdin 送过去的，
如果 chromium 继承了这条管道，它会把剩下的脚本文本当标准输入读走——
表现是每秒多张图都"成功"退出、耗时 400~600ms、**一张 png 都没写**。
四个模式连拍时稳定复现，加 `< /dev/null` 后连跑 4 轮全绿。
这条不只影响截图，任何在 ssh stdin 脚本里起的交互式子进程都要显式断 stdin。

**⑤ 偶发空白帧会重试一次。**
即使上述都做对，chromium 仍偶有"exit 0 但没写文件"。shot.py 检测到 0 字节
就用全新 profile 重跑一次，并在输出里标 `retried <name>`。

### 自带渲染校验

每张图会额外判断，出问题直接 `RESULT: N problem(s)` 并以 1 退出：

- PNG 实际像素尺寸 == 期望尺寸 × dpr
- PNG 小于 6 KB → 大概率空白帧
- DOM 布局元素 < 12 个 → 页面没渲染出来
- 渲染文本 < 40 字符
- DOM 里还有 `加载中 / loading / 请稍候` 等字样 → 数据没到
- `--expect` 的正则没命中

> 校验会先剥掉 `<script>`/`<style>` 再统计。
> 这一步是必须的：Vue 应用把整份模板放在 JS 字符串里，
> 直接数原文的 `<div` 会把一个什么都没渲染的页面报成"有 67 个元素"。

实测（真实面板，26.11.0，四模式连拍）：

```
      desktop      rc=0   100703 B png    12363 B dom   2190 ms  1440x1000
      mobile       rc=0    48857 B png    12363 B dom   1153 ms  390x844
      dark         rc=0   100984 B png    12364 B dom   1005 ms  1440x1000  dark
      mobile-dark  rc=0    49228 B png    12364 B dom   1050 ms  390x844  dark
      desktop      DOM: 178 elements, 511 chars text
[2] download + verify          7.2s
RESULT: OK
```

反例（故意把 `--budget` 压到 300）：

```
WARN  custom: png is only 4655 bytes -- probably a blank frame
WARN  custom: DOM has only 2 layout elements -- page probably did not render
WARN  custom: rendered text is only 6 chars ('> 加载中…')
WARN  custom: DOM still shows loading text ['加载中'] -- data had not arrived
RESULT: 5 problem(s)
```

---

## 4. inspect_fpk.py —— 装之前先验包

```powershell
& $PY tools\inspect_fpk.py           # 看 output/ 里最新的
& $PY tools\inspect_fpk.py output\fn-pvepanel-26.11.0-fnos-all.fpk
& $PY tools\inspect_fpk.py --strict  # 有 warning 也返回 1
```

查的是历史上真实咬过人的东西：外层必需成员、`cmd/*` 与 `ui/index.cgi` 的可执行位
（缺了安装报 10111）、`ui/config` 里 `icon_{0}.png` 的九档图标是否齐全
（缺档不报错，只是图标空白）、`__pycache__`/`.pyc`/.DS_Store 垃圾、
CGI 里的 CRLF（`bad interpreter`）、`desktop_applaunchname` 与 `ui/config` 键名是否一致、
以及包内 version 与源码 manifest 是否对得上（防拿旧包发版）。

---

## 5. 一次典型的迭代

```powershell
$PY = "C:\Users\PVE\.dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\python\python.exe"
cd "D:\Workspace\DeepSeek Harness\pve-panel"

& $PY tools\build_fpk.py
& $PY tools\inspect_fpk.py
& $PY tools\deploy.py
& $PY tools\shot.py --preset all
```

跑完看 `pve-panel/image/` 下那四张图。改前端时通常只要最后一步
（面板服务端会直接读 `@appcenter/fn-pvepanel/web/`，重新 deploy 才更新）。

---

## 安全边界

- 只连 `fnos-test`（192.168.1.13）与本地工作区；**不碰 fnos-prod**，**不连 PVE**。
- 全部走 SSH 密钥认证（`~/.ssh/config` 里的 `fnos-test`），
  三个工具都不接收、不读取、不落盘任何密码或 Token。
- SSH 参数统一 `BatchMode=yes`，需要密码就直接失败而不是挂住等输入。
