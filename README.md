# PVEpanel（fn-pvepanel）

飞牛 fnOS 上的 Proxmox VE 只读监控面板。用 fnOS 原生的 fpk 形态分发，后端是纯
Python 3 标准库写的本机服务，前端是离线打包的 Vue 3。

**浏览器永远不直连 PVE。** 集群地址和 API Token 只保存在设备本地，所有请求由本机
服务转发——既避开了 CORS，也让 Token 不出设备。

- 当前版本：`26.11.7`
- 安装形态：`fn-pvepanel-26.11.7-fnos-all.fpk`，在飞牛「应用中心 → 手动安装」导入
- 运行位置：`/usr/local/apps/@appcenter/fn-pvepanel`，数据在 `@appdata/fn-pvepanel`
- 监听：`127.0.0.1:8899`（只走桌面入口的 CGI，天然带 fnOS 登录校验）

---

## 它做什么

七个页面：

| 页面 | 内容 |
|---|---|
| 总览 | 节点 CPU/内存/负载卡片、集群仲裁与 HA、告警汇总、资源曲线、存储水位 |
| 虚拟机 | VM 与 CT 混排、状态筛选、搜索、CPU/内存占用、运行时长、模板标记 |
| 存储 | 每个存储的用量环、可用容量、内容类型、共享标记 |
| 磁盘 | 物理盘型号容量、健康、**温度**、SSD 寿命；以及「未被任何备份覆盖」的机器 |
| 任务 | 备份/迁移/启停记录与成败；订阅状态；系统服务健康 |
| 设置 | 服务器管理、通知推送、维护（备份/恢复）、更新（含一键升级）、外观与语言 |

**不只是只读**：虚拟机页可以直接开机 / 关机 / 强制停止 / 重启，带二次确认。

告警覆盖：节点离线、集群失去仲裁、存储水位、**虚拟机无备份**、备份任务失败、
磁盘 SMART 异常、**磁盘温度超阈值**、负载偏高、内存紧张、订阅即将到期、权限不足、
Token 失效。所有这些都能通过九种通道推送到手机。

服务器可以加多个，顶栏切换，总览只显示当前那一台的节点。

中英文切换、明暗主题跟随系统；手机端侧栏变成抽屉，表格自动收列。

---

## 多服务器

面板可以同时保存多台 PVE，顶栏切换当前查看的那一台。总览、虚拟机、存储、磁盘、
任务全部只针对当前服务器——不会把两台机器的数字混成一张图。

服务端把每台服务器的缓存、连接池、认证状态分开保存（`collector.ServerState`），
所以一台 Token 失效不会拖累另一台。

从 26.11.0 升上来的老配置会自动迁移：原来 `config.json` 里的地址与
`token.json` 里的 Token 会迁成第一条服务器记录，不用重填。

---

## 虚拟机启停

虚拟机页每行右侧有操作按钮：开机（停止状态时）、重启 / 关机 / 强制停止（运行状态时）。
强制停止是红色，并且会额外弹一句「相当于拔电源，可能丢失数据」。

这是面板**唯一会写 PVE 的地方**，走 `POST /nodes/{node}/{qemu|lce}/{vmid}/status/{action}`，
返回任务的 UPID。需要 Token 上有 `VM.PowerMgmt`——`PVEAuditor` 是纯只读，**不含这个权限**，
所以要么换成 `PVEVMAdmin` 之类的角色，要么额外授一条：

```bash
pveum acl modify /vms --tokens 'monitor@pve!dash' --roles PVEVMAdmin --propagate 1
```

没有这个权限时按钮不会消失，点了会明确提示「权限不足」，比隐藏按钮更好排查。

---

## 通知推送

九种通道：Bark / Server 酱 / PushPlus / WxPusher / Telegram / 企业微信机器人 /
钉钉机器人 / Slack / 自定义 Webhook。

十二类事件可以逐条开关。推送的判据就是面板上显示的告警——**同一条规则不会出现
「页面红了但没推送」或反过来**。每个事件带冷却时间（存储 6 小时、负载 30 分钟、
订阅 24 小时……），并且同一条告警重复出现时不会反复推送。

推送在独立线程里发送，webhook 慢或挂掉都不会拖住页面。

---

## 更新与维护

**一键更新**在「设置 → 更新」里：读取 GitHub 上的最新发布版本，与当前安装的版本比较，
发现新版本可直接下载安装。流程是先校验安装包（应用名、版本号、可执行位）再卸载旧版，
装不上会自动回滚到旧包；下载期间显示真实百分比，安装期间弹窗不关闭，服务重启后自动恢复。

后台线程每小时自查一次，与浏览器是否打开无关——没人开面板也会知道有新版本。

**维护**在「设置 → 维护」里：导出 / 导入配置、导出到设备备份目录、自动备份
（间隔 + 保留天数 + 上次成功 / 下次运行 / 上次错误）、按单个文件恢复或删除。

两种备份的取舍是有意为之：

- **下载到本地的**不含 API Token。这个文件会离开设备，不能带集群凭据。
- **设备上的自动备份和「导出到备份目录」**保留 Token。否则恢复出来是个不能用的面板，
  还得把每台机器的 Token 重填一遍。文件权限 0600，目录 0700。
- **保留期只清理自动备份**，不动手动导出的；读不出类型的文件也保留——更可能是损坏而不是可弃。

## 四条不显眼但必须有行为

这四条都是踩过才写出来的，改动时别顺手删掉。

### 1. 401 立刻熔断

PVE 对**每一次**认证失败都会压 3 秒才回（`pve-http-server` 里写死的
`$delay = 3 - elapsed`），并且 4xx 强制 `Connection: close`。一个失效的 Token
如果放任轮询，8 个端点串行就是 24 秒，三节点集群 60 秒——面板看起来就是死了。

所以：**首个 401 立刻中止整轮采集**，不重试、不继续拉剩下的端点，把面板切到
「Token 已失效」状态并停止轮询。实测一次强制刷新正好 3.0 秒收尾，不会随时间累积。

同时保留**最后一次成功的数据**继续显示（标记为「已冻结」），而不是清空成 0 ——
巡检的人需要看到出事前的样子。

### 2. 探活绝不打需要认证的端点

`/api2/json/version` 需要一个有效 Token，没配 Token 时每次探活白烧 3 秒。
面板改用 `GET /`（免认证、约 5 ms）做连通性探测，顺带从页面里读出 PVE 版本和
节点名——所以** Token 还没填的时候，首屏也能显示「PVE 在线 / 版本 / 节点名」**。

### 3. Token 掩码不会被当成新值写回

`GET /api/settings` 把已保存的 Token 换成字面量 `__SET__` 返回，前端输入框留空并
提示「已保存，留空保留原值」。写入路径**两道防线**：

- 服务端收到 `__SET__` 或空串时沿用旧值，只有真填了新值才替换；
- 前端只提交真正改动的字段，不回传整份带掩码的 draft。

少任何一道，用户改个阈值就会把 Token 写成 `__SET__`，而且**无法自动恢复**。

---

## 权限：一条命令的差别

单个 `PVEAuditor` 角色就够（它已含 `Sys.Audit` + `Datastore.Audit` + `VM.Audit` +
`Pool.Audit` + `SDN.Audit`），**但必须授在 `/` 且开启 propagate**：

```bash
pveum user add monitor@pve --comment "fnOS PVE panel (read-only)"
pveum acl modify / --users monitor@pve --roles PVEAuditor --propagate 1

# Token 默认 privsep=1（权限取交集），ACL 必须额外授给 token 本身
pveum user token add monitor@pve dash --privsep 1 --expire <unix 秒> --comment "fnOS panel token"
pveum acl modify / --tokens 'monitor@pve!dash' --roles PVEAuditor --propagate 1
```

只授在 `/nodes/<node>` 子树的话，`/cluster/status` 与 `/nodes/<node>/disks/smart`
会直接 403——因为这两个端点查的是**根路径**权限。面板会把 403 显示成「权限不足，
请确认 PVEAuditor 授在 / 并开启 propagate」，而不是笼统的「连接失败」。

> Token 不设 `expire` 就是**永不过期**。建议显式设一个，并在到期前换掉。

---

## 拿不到的数据

这三项在 PVE 的 API 层根本不存在，不是没做：

| 拿不到 | 原因 |
|---|---|
| CPU 温度 / 主板温度 | 全部 437 个叶子端点里 `temperature` / `sensor` / `thermal` 命中 0 |
| 风扇转速 | `fan` / `rpm` 命中 0 |
| 单物理网卡吞吐 | `/network` 只有配置；`netstat` 只覆盖 tap/veth。只能看节点合计的 `netin/netout` |

**磁盘温度是可以拿的**，但按盘型走不同分支：ATA 盘取
`attributes[id=="194"].raw` 的首个 token，NVMe/SAS 只能从 `text` 自由文本里正则抠。
注意 `disks/list` 的 `type` 是 `ssd/hdd/nvme`，而 `disks/smart` 的 `type` 是
`ata/text` —— **两套枚举，混用会让温度全部解析失败**。

---

## 刷新节奏

每类数据有自己的 TTL，不做「一键全刷」：

| 数据 | TTL | 为什么 |
|---|---|---|
| 免认证指纹 | 5 s | 4~6 ms，白送 |
| 集群 / 节点 / 虚拟机 | 8 s | pvestatd 本身就是 10 s 周期，更快没意义 |
| 存储 | 30 s | 变化慢 |
| 任务 / 服务 | 30~60 s | 变化慢 |
| 曲线 RRD | 60 s | 分钟粒度 |
| 磁盘 / SMART | 300 s | smartctl 是全场最慢的一类，每块盘一次请求 |

连接复用是必须的：实测 keep-alive 后单请求 0.45 ms，每次新建 TLS 是 2.7 ms。

---

## 目录

```
src/fn-pvepanel/            fpk 源码树
  manifest                  版本、端口、安装类型
  cmd/                      9 个生命周期脚本（8 个原样，main 改过）
  config/                   privilege（run-as root）、resource
  wizard/                   安装时问端口
  app/ui/                   桌面入口配置、index.cgi、九档图标
  app/server/               pveclient（连接池+错误分流）/ collector（采集+缓存+告警）/ store（配置与 Token）/ pvepanel（HTTP 服务）
  app/web/                  index.html / style.css / js/
tools/                      build_fpk.py / deploy.py / shot.py / inspect_fpk.py
output/                     打好的 fpk
image/                      截图
report/ research/           设计与实测记录
```

---

## 开发

打包只能在 Linux 上做（Windows 会丢可执行位，安装报 10111）。构建脚本通过 SSH 在
测试机上完成：

```bash
python tools/build_fpk.py          # 结构检查 → 打包到 output/
python tools/deploy.py             # 卸载旧版 → 装新版 → 起服务 → 探活
python tools/shot.py --preset desktop --url "http://127.0.0.1:8899/?view=dash"
python tools/pve-test.py "qm list" # 在测试 PVE 上跑命令
```

### 发布

推送 `v*` 标签即触发 GitHub Actions：构建 → 校验 → 传 artifact → 发布 Release。
只有「标签名 == `v` + manifest 版本」才会发 Release，临时标签只出 artifact。

```bash
# 改 manifest 版本、CHANGELOG、index.html 的 ?v=
git commit -am "..." && git push
git tag -a v26.11.8 -m "PVEpanel 26.11.8" && git push origin refs/tags/v26.11.8

# CI 绿了之后，验证发布出来的东西真的能用
python tools/release.py v26.11.8
```

`release.py` 查三件事，每一件都曾经被「CI 绿了」漏掉过：

1. 这次标签的 Actions 跑成功了，并列出每个步骤的结果；
2. 发布出来的 .fpk 能下载、大小对得上、`inspect_fpk --strict` 通过（可执行位、
   九档图标、包内版本号）；
3. 测试机上的面板能通过它自己的更新检查读到这个版本——**能读到才叫发布成功**，
   传上去了但读不到等于没发。

第 3 步对着的是运行在 fnOS 上的面板，不在这台开发机上，所以默认地址是
`$PVEPANEL_URL`（或先 `ssh -N -L 18899:127.0.0.1:8899 fnos-test`）。

### 发布前的闸门

`py_compile` 只证明文件能解析。这个项目实际踩到的 bug 全都是语法完全合法的：
方法体缩进跑偏导致类里没有这个方法、`self.foo` 读了但任何地方都没赋值、
用了但没定义的名字。所以有三道更硬的闸门：

```bash
python tools/check_structure.py    # 缩进跑偏 / 未定义名字 / self 属性缺失
python tools/test_bootstrap.py     # 首屏不许被网络阻塞
python tools/test_maint.py         # 配置备份往返
python tools/test_upgrade_installer.py
python tools/test_notify.py
python tools/test_update.py
```

`build_fpk.py` 会把 `check_structure.py` 作为打包前的一道闸门跑一遍，失败就**不打包**。

浏览器侧有两个只能真跑才知道的检查（它们抓的是静态分析看不见的运行时错误）：

```bash
# 在每个视图下渲染页面并收集 console.error / 未捕获异常
python tools/console_check.py
# 测量列表表的真实渲染结果：有没有列被隐藏、内容比容器宽时能不能滚
python tools/probe_cols.py
```

两者都**必须在设备上跑**（要 chromium 和已安装的 web 目录写权限）。

一个测量上的坑值得记下来：这台 chromium 的 `--dump-dom` **忽略 `--window-size`**，
三个不同尺寸全报 `innerWidth = 780`。用 `--dump-dom` 量"手机布局"得到的其实是桌面布局。
`--screenshot` 是正常的。要断言手机下的表现，就看截图，或者把结论写进页面再截图。

详见 [tools/README.md](tools/README.md)。

**本机没有 python/node**，用 DSH 自带运行时：
`C:\Users\PVE\.dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\python\python.exe`。

本地开发**已经有一台真的 PVE 可用**：`fnos-test` 上跑着一台完整的 Proxmox VE 8.4.21
（libvirt 虚拟机，面板已连上它）。连法、凭据、重建步骤见 [docs/TEST-ENV.md](docs/TEST-ENV.md)，
跑命令用 `tools/pve-test.py`。

另有一个行为仿真的 Mock（真实的 3 秒 401 惩罚、两种形态磁盘温度、可切 401/403/offline），
只在需要复现故障态时切过去，用法见 `tmp/mock/README.md`。

---

## 已知限制

- **崩溃不自愈。** 开机自启是有的（appcenter 启动时批量拉起），但进程被 `kill -9`
  后没有任何东西会把它拉回来，状态会停在 `start`。异常退出后到应用中心点一下启动。
- **直连端口没有鉴权。** 面板监听 `127.0.0.1`，只通过桌面入口的 CGI 访问，
  那条路有 fnOS 登录校验。如果改成 `0.0.0.0`，任何能访问该端口的人都能看到整个集群
  的只读数据——要做的话得另外加一层面板密码。
- **写操作只有虚拟机启停。** 快照、备份触发、创建/删除虚拟机都未实现，且都需要
  更高的权限集。
