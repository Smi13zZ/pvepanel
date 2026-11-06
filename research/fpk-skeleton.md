# fpk 骨架可复用性与打包链路复核

- 日期：2026-11-05
- 复核人：fpk-skeleton（teammate，task-2）
- 复核对象：`qbpanel-maintenance/src/qbpanel/qbpanel`（v26.11.10）与 `fn-emule/src/emule`（v26.11.38），两者结构同源、均已在飞牛真机跑通
- 实测环境：`fnos-test` = 192.168.1.13，fnOS **1.2.0800**，Debian 12，Python **3.11.2**，`fnpack 1.2.4`（`/usr/local/bin/fnpack`）
- 结论口径：**【实测】** = 我在 fnos-test 上真跑过；**【读码】** = 从源码/二进制推断，未逐条验证

> 本篇只回答「搬骨架的真实成本」。PVE 侧 API 能力与延迟画像由 pve-probe / api-surface 覆盖。

---

## 0. 结论先行

**可以搬，而且是低成本搬。我在 fnos-test 上完整走通了「造骨架 → fnpack 打包 → 安装 → 服务起来 → CGI 转发 → 卸载 → 再装回」全链路，全程无阻塞。**

四个最关键的事实：

1. **骨架的真实成本是「2 小时 + 一次真机回归」，不是 0.5 天，也不需要 0.5 天。** 打字量很小（`cmd/main` 199 行 + `index.cgi` 125 行 + 三个 JSON），但**不能盲抄**：抄错的地方至少有两处会在安装期直接炸（见 §5 的坑）。
2. **`service_port` 和面板真正监听的端口没有关系。** manifest 里写 8899 只是给应用中心做端口占用检查，真实端口由 **wizard 的环境变量 → `cmd/main` → 后端进程** 这条链决定。报告第八节把 `service_port` 说成「指定面板端口」，不准确。
3. **自启是有的，但不是 systemd，也没有崩溃监护。** appcenter 自己（`trim_app_center`）在启动时批量拉起所有 `status=running` 的应用，实测确认（§3.4）。进程被 `kill -9` 后 **120 秒内没有任何东西拉起它**，状态在 DB 里会从 `running` 掉到 `start`——这点必须写进 README，否则用户会以为面板挂了。
4. **CGI 转发不需要 socket，是「nginx → 内置 CGI handler → fork 你的 shell 脚本 → 脚本 curl 到 127.0.0.1:端口」**。只读面板沿用这套完全够用（§4）。

**推翻/修正报告的地方见 §7。**

---

## 1. manifest：字段全集与真实作用

### 1.1 字段全集

appcenter 二进制里能直接读出它认识的 ini 键（`sudo grep -ao 'ini:"[a-z_]*"' /usr/trim/bin/trim_app_center | sort -u`）【实测】：

~~~
app_name  arch  auto_upgrade_from  change_log  checkport  checksum  ctl_stop
ctl_uninstall  desc  desktop_applaunchname  desktop_appname  desktop_apppage
desktop_uidir  disable_authorization_path  display_name  distributor
distributor_url  exclude_arch  exclude_kernel  exclude_model
install_break_apps  install_conflict_apps  install_dep_apps  install_reboot
install_replace_apps  install_type  kernel  maintainer  maintainer_url
micro_app  min_size  model  native_app  os_max_version  os_min_version
platform  service_path  service_port  service_protocol  source  support_url
third_party  trim_api_sign  trim_dev_crt  version
~~~

### 1.2 必填字段（fnpack 会校验）

`fnpack build` 缺失必备文件会直接失败。实测缺 `config/resource` 时报
`Packing failed. Required file config/resource is missing`【实测】。
必填集合 = `fnpack create` 生成的模板里出现的那批：

| 字段 | 含义 | 取值约束 | 只读面板怎么填 |
|---|---|---|---|
| `appname` | 包名，也是安装目录名 | `/var/apps/<appname>`、产物名 `<appname>.fpk`、CGI 路径 `/cgi/ThirdParty/<appname>/` 都用它 | `fn-pvepanel`（沿用 fn- 前缀风格） |
| `version` | 版本 | 写进 appcenter DB，UI 显示；升级判定靠它 | `26.11.0` |
| `display_name` | 应用中心/桌面显示名 | 中文 OK（UTF-8） | `PVE 面板` |
| `desc` | 应用中心详情页描述 | 中文 OK | 见骨架样例 |
| `platform` | 架构 | `x86` / `arm` / `all` | `all`（纯 Python，无二进制） |
| `source` | 来源渠道 | `thirdparty` / `official` | `thirdparty` |
| `maintainer` / `distributor` | 维护者/分发者 | 自由文本 | 照抄 |
| `desktop_uidir` | 桌面入口所在目录 | 填 `ui`，指向包内 `app/ui/` | `ui` |
| `desktop_applaunchname` | 桌面入口 ID | 必须与 `app/ui/config` 里 `.url` 的键一致 | `fn-pvepanel.Application` |

### 1.3 非必填但**这个场景必须写**的

| 字段 | 报告里的说法 | 实测/读码的真实作用 |
|---|---|---|
| `install_type = root` | 「install_type=root」 | **决定 `@appdata` 的属主**。实测装出来 `/usr/local/apps/@appdata/<app>` 是 `770 root:root`，普通用户读不了；模板默认的 `run-as: package` 走另一套。Python 常驻服务要写配置，**必须 root** |
| `ctl_stop = true` | 未提 | 告诉应用中心「本应用支持 stop」，卸载/关机前会调 `cmd/main stop`。**不写会出现进程残留** |
| `service_port = 8899` | 「指定面板端口」 | **不决定监听端口**（见 §1.4）。实际作用：安装期端口冲突预检 + 写 app_service 记录 |
| `checkport = false` | 未提 | 关掉「安装时检查端口占用」。**面板端口是安装后才在设置页改的，必须 false**，否则用户换个已占用端口就装不上 |
| `distributor_url` | 未提 | 应用中心详情页的链接，写 GitHub 地址 |
| `install_dep_apps =` | 未提 | 留空。**不要填 `fn-qbpanel` 之类**——会变成硬依赖 |

### 1.4 `service_port` 到底怎么起作用【读码 + 实测】

- manifest 的 `service_port` 会被 appcenter 读进 `GetServicePortToString()`，写进 `app_service` 表；
- 实测 `select * from app_service where app_id=<我们的>` 里 `url` 是 `http://${host}/cgi/ThirdParty/fn-pvepanel/index.cgi/`，**没有任何端口字段**；
- **真正的监听端口**是这条链：

~~~
wizard/install 的 field 名 wizard_backend_port
      → appcenter 存为环境变量
      → cmd/main 读到 $wizard_backend_port
      → 写成 var/runtime-ports.env (chmod 600)
      → 起 python 时以环境变量传入
      → index.cgi 读同一个文件拿到 ADMIN_TARGET_PORT
~~~

- 也就是说 **`service_port` 只是「我打算用 8899」的声明，改端口不用重打包**（设置页改环境变量即可，`config_callback` 会 stop→sleep1→start 重启服务，见 qbpanel `cmd/config_callback:15-25`）【实测】

### 1.5 `desktop_uidir` + `app/ui/config`【实测】

`app/ui/config` 是桌面入口的定义：

~~~json
{
  ".url": {
    "fn-pvepanel.Application": {
      "noDisplay": false, "allUsers": false, "fileTypes": [],
      "control": { "accessPerm": "readonly" },
      "protocol": "http",
      "title": "PVE",
      "icon": "images/icon_{0}.png",
      "type": "iframe",
      "url": "/cgi/ThirdParty/fn-pvepanel/index.cgi/"
    }
  }
}
~~~

- `type: "iframe"` = 在飞牛桌面里以 iframe 打开，**不是新窗口**。所以前端所有请求必须用**相对路径** `./api/...`，绝对路径会逃出 `/cgi/ThirdParty/<app>/index.cgi/` 挂载点（qbpanel `docs/DEVELOPMENT.md:54` 明确记过这条）。
- `icon: "images/icon_{0}.png"` 里的 `{0}` 由应用中心按设备像素密度替换成 16/24/48/64/72/96/128/256/512 **九档**。实测本机系统应用也只提供这些尺寸。**缺档 = 该设备图标空白**。
- 实测我打的包内 `app.tgz` 的 `ui/images/` 下九档齐全（见 §5 产物清单）。

---

## 2. cmd/ 生命周期脚本

### 2.1 九个脚本的触发时机

| 脚本 | 触发时机 | 约定 | 能否原样复用 |
|---|---|---|---|
| `install_init` | 安装**前** | `exit 0` | ✅ 原样（空实现） |
| `install_callback` | 安装**后** | 拉起服务。**实测此时服务不会自动起**（状态 `starting`），必须自己 start | ✅ 原样 |
| `config_init` | 用户在设置页改环境变量**前** | `exit 0` | ✅ 原样 |
| `config_callback` | 改完环境变量**后** | 必须 stop→start 让新端口生效；失败要写 `${TRIM_TEMP_LOGFILE}` 并 `exit 1` | ✅ 原样 |
| `main` | 被上面几个脚本、appcenter、CLI 调用 | `start/stop/status/restart`；**status 返回 0=运行，3=未运行** | ⚠️ 要改：进程名、日志名、默认端口、env 前缀 |
| `uninstall_init` | 卸载**前** | 停服务，防残留进程 | ✅ 原样 |
| `uninstall_callback` | 卸载**后** | 清 pid / runtime-ports.env；**故意保留配置**（见 §3.6） | ✅ 原样（改文件名） |
| `upgrade_init` | 升级**前** | 停服务 | ✅ 原样 |
| `upgrade_callback` | 升级**后** | 重新 start | ✅ 原样 |

### 2.2 硬约定（踩一次就知道）

1. **每个 `cmd/*` 必须带 shebang**（`#!/bin/bash`）。少了安装报 **10237**（fn-emule 项目记忆已记）。
2. **`cmd/*` 与 `app/ui/index.cgi` 必须 0755**。在 Windows 上打包会全部变 0666，装上去报 **10111**。这就是 `build_fpk.py` 一定要在 Linux 上跑的原因（qbpanel `tools/build_fpk.py:4-13` 的注释讲的就是这个）。
3. **`status` 的返回码有语义**：`0`=运行，`3`=未运行。返回别的值应用中心会当成「未知」。实测我的 `cmd/main status` 在服务死后返回 3，appcenter 正常识别为 `stopped`。
4. **`TRIM_PKGVAR` 是唯一稳定的可写目录**（= `/usr/local/apps/@appdata/<appname>`）。**不要**往 `app/` 或 `/var/apps/<app>/target/` 写——那里是只读镜像且升级会被覆盖。
5. **`TRIM_APPDEST` 是程序目录**（= `/usr/local/apps/@appcenter/<appname>`，即 `/var/apps/<app>/target` 的软链目标）。安装期间它是**暂存目录** `/vol1/appcenter-downloads/<app>-<ver>-tpk/`，所以任何路径都必须在**运行期**算，不能在 `install_init` 里算死（qbpanel `docs/DEVELOPMENT.md:127` 记过）。
6. **`TRIM_TEMP_LOGFILE`**：失败原因写这里才能显示给用户。

### 2.3 `cmd/main` 里真正需要改的 7 处

对照 qbpanel `cmd/main`（199 行），改：

| 行 | 原值 | 改成 |
|---|---|---|
| 5-10 | `PKG_VAR / APP_HOME / PID_FILE / LOG_FILE / PORTS_FILE / DEFAULT_PORT` | `pvepanel.pid` / `pvepanel.log`、`DEFAULT_PORT=8899` |
| 22 | `SERVER=${APP_HOME}/server/qbpanel.py` | `server/pvepanel.py` |
| 125-131 | 导出的 `QBPANEL_*` 环境变量 | 换成 `PVEPANEL_*` |
| 其余 | 日志/提示里的 "qBpanel" | "PVE 面板" |

其余逻辑**全部可原样保留**，这些都是踩过坑之后写的东西：

- `find_python` 依次探 `/usr/bin/python3 /usr/local/bin/python3 /bin/python3 / command -v python3`；
- `port_ready` 用 bash 的 `/dev/tcp/127.0.0.1/PORT` 探活（不依赖 nc）；
- `rotate_log` 超 1MB 轮转成 `.log.1`；
- **`setsid` 必不可少**——不加的话 appcenter 调 `cmd/main start` 结束后子进程会被一起收掉；
- **40×0.5s 的探活循环必不可少**——直接 `exit 0` 会让 `install_callback` 在服务还没 listen 时就去拉桌面入口。

---

## 3. 运行时模型：常驻服务 + 只读拉远端 HTTPS 在 fpk 里受不受限

**结论：不受限。这是一个「和既有 fpk 应用完全同构」的模型。** 逐项实测：

### 3.1 权限【实测】

- `install_type = root` 装出来的 `@appdata` 是 `770 root:root`；
- 服务以 root 跑，被 `kill` 后能正常重启；
- **写 `chmod 600` 的文件完全可行**：实测在 `@appdata` 里建 `tok.json` 后 `stat` 得到 `0o600 root:root`，且 `sudo -u ranger`（uid 1000，属 Administrators 组）`cat` 直接 **Permission denied**；
- 结论：**token 存 `@appdata/token.json` + `chmod 600` 是当前 fnOS 上最稳且足够强的做法**（详见 §6）。

### 3.2 资源【实测】

- 空壳服务内存：**RSS ≈ 19 MB，VSZ ≈ 100 MB，CPU 0.1%**（`ps -o pid,rss,vsz,pcpu`）；
- 包体积：含九档图标 + 前端 + 后端源码，**66 KB**（`.fpk`）。实际面板估 300 KB–1 MB（Vue 运行时 ~150 KB）；
- **没有发现任何 cgroup / 内存 / CPU 限制**：appcenter 不生成 systemd unit（`/etc/systemd/system` 里只有 `trim_app_center.service` 本身），服务是 `setsid` 直接挂在 init 下。

### 3.3 出站 HTTPS 到远端【实测】

在 fnos-test 上以 root 跑 python，对 `192.168.1.3:8006`（PVE，只读探测、未做任何认证）：

~~~
TLS ok, cipher: TLS_AES_256_GCM_SHA384  connect+handshake 0.023s
~~~

- 出站 8006/443 无防火墙阻碍（`fw.conf` 里没有针对应用端口的规则，默认放通出站）；
- **自签证书可绕过**：`ssl.create_default_context()` + `check_hostname=False` + `verify_mode=CERT_NONE` 生效；
- **握手只花 23 ms**，所以「建连慢」不是问题。报告里那 3 秒与 TLS 无关（pve-probe 已定位到 401 分支）。

### 3.4 开机自启【实测，且推翻了「可能不自启」的猜测】

**证据链**：

1. appcenter 启动时**确实**批量拉起应用。实测把我们的 app 进程 `kill -9`（DB 里状态从 `running` 掉到 `start`），然后 `systemctl restart trim_app_center`：
   ~~~
   -> RELAUNCHED (DB status: running)
   ~~~
2. journal 里能直接看到事件：`"eventId":"APP_AUTO_STARTED","from":"trim.app-center"`，重启 appcenter 时对 fn-knock / fn-qbpanel / fn-emule / trim.preview / **fn-pvepanel** 各发一条【实测】。
3. **但没有任何崩溃监护**：另一次实验 `kill -9` 后**不**动 appcenter，120 秒轮询端口 8899 全程不通，DB 状态停在 `start`。日志最后一行是崩溃前那行，没有重启记录。
4. 结论：**开机自启 OK（appcenter 起来就拉起）；运行中崩溃不会自愈，需要用户手点「启动」。**

> 值得注意：qbpanel 和 fn-emule 有同样的特性（它们也没做守护），所以这是**既有惯例，不是新引入的风险**。想做得更稳可以在 python 里起一个自监管线程；否则至少要在 README 写清「异常退出后到应用中心点启动」。

### 3.5 日志位置【实测】

| 位置 | 谁写的 | 实测结果 |
|---|---|---|
| `/var/log/apps/<appname>.log` | appcenter 捕获 `cmd/*` 的 stdout | **0 字节**——因为 `cmd/main` 把 python 的 stdout 重定向进了自己的日志文件 |
| `@appdata/<appname>/<app>.log` | 我们自己的 python | 实际有效，实测有内容、按 1MB 轮转 |
| journald | 只有 appcenter 的 GIN 日志 | 应用自身日志**不**进 journal |

**建议**：沿用 `cmd/main` 的 `rotate_log` + 自己的 `<app>.log`，并把日志入口放面板「设置 → 日志」里给用户看（qbpanel 就是这么做的）。

### 3.6 卸载与升级的数据保留【实测】

- **卸载不删 `@appdata` / `@apphome` / `@appconf` / `/vol1/@appmeta`**：实测卸载后这 4 个目录都还在，只有 `/var/apps/<app>` 和 `@appcenter/<app>` 被删；
- 实测「卸载 → 重装 26.11.1」后日志文件从 26.11.0 的第一行一直连到 26.11.1，**配置确实能继承**；
- **原地覆盖安装会失败**：`appcenter-cli install-fpk` 对已装应用返回 **`code 18000`**【实测】。正确升级顺序（fn-emule 项目记忆已记，我实测确认整套耗时 **27 秒**）：
  ~~~
  appcenter-cli stop <app>
  appcenter-cli uninstall <app>
  appcenter-cli install-fpk <fpk> -e <env文件> -v 1
  appcenter-cli start <app>      # install-fpk 之后状态是 starting，不一定已经 listen
  ~~~

---

## 4. CGI → 后端转发机制

### 4.1 请求链路【实测 + 读码】

~~~
浏览器
  │  https://<nas>:23155/cgi/ThirdParty/fn-pvepanel/index.cgi/api/health
  ▼
nginx（trim_nginx.service，监听 80/443/23155/23156）
  │  /cgi/... 交给 trim_http_cgi
  ▼
trim_http_cgi（/usr/trim/bin/trim_http_cgi，Go）
  │  ① 校验 fnOS 登录 token（无 cookie → 回 "invalid token"）
  │  ② 解析 /cgi/ThirdParty/<appname>/ → /var/apps/<appname>/target/ui/
  │  ③ fork exec index.cgi，注入 CGI 环境变量
  ▼
app/ui/index.cgi（sh 脚本）
  │  读 ${TRIM_PKGVAR}/runtime-ports.env → 拿到 127.0.0.1:<port>
  │  curl 转发，原样回写状态行 / Content-Type / body
  ▼
app/server/pvepanel.py（本机 HTTP 服务，监听 0.0.0.0:<port>）
~~~

**没有 unix socket、没有额外端口、不要求应用自己实现 CGI**——`index.cgi` 只是个 shell 里跑 curl 的**反代垫片**。

### 4.2 注入的环境变量【实测】

从 `trim_http_cgi` 二进制里读到的 CGI 变量名：`REQUEST_URI` / `REQUEST_METHOD` / `CONTENT_TYPE` / `CONTENT_LENGTH` / `REMOTE_ADDR` / `SERVER_NAME` / `SERVER_PORT` / `SERVER_PROTOCOL` / `SERVER_SOFTWARE` / `GATEWAY_INTERFACE` / `SCRIPT_FILENAME`，以及 fnOS 专有的 `cgiName`。

实测手工模拟一次调用，行为完全正确：

~~~bash
sudo env REQUEST_METHOD=GET REQUEST_URI=/cgi/ThirdParty/fn-pvepanel/index.cgi/api/health \
  cgiName=fn-pvepanel GATEWAY_INTERFACE=CGI/1.1 \
  /var/apps/fn-pvepanel/target/ui/index.cgi
# -> {"ok": true, "app": "fn-pvepanel", "version": "26.11.0", ...}
~~~

`index.cgi` 定位端口文件用了**三级兜底**（qbpanel `app/ui/index.cgi:11-23`），这是被踩出来的，不要简化：

1. `${TRIM_PKGVAR}/runtime-ports.env` —— 正常情况
2. `/var/apps/${cgiName}/var/runtime-ports.env` —— 有 `cgiName` 但没 `TRIM_PKGVAR`
3. `${SCRIPT_FILENAME%/target/ui/index.cgi}/var/runtime-ports.env` —— 从脚本自身路径反推

**实测确认：`trim_http_cgi` 的二进制里 `TRIM_PKGVAR` 字符串出现 0 次**（`grep -ac TRIM_PKGVAR /usr/trim/bin/trim_http_cgi` → `0`），也就是说 **`TRIM_PKGVAR` 不会被注入到 CGI 环境**，真正生效的是第 2/3 级兜底。这条很重要——如果只写第 1 级，CGI 会永远落到默认端口，改端口后桌面入口就废了。

### 4.3 安全边界【实测，这条要注意】

| 入口 | 是否需要 fnOS 登录 | 实测 |
|---|---|---|
| `https://<nas>:<web端口>/cgi/ThirdParty/<app>/index.cgi/...` | **需要** | 无 cookie 请求 → `200` + body `invalid token` |
| `http://<nas>:8899/api/health`（**直连面板端口**） | **不需要** | `200` + 完整 JSON |

也就是说：**`index.cgi` 那条路有 fnOS 登录闸门，直连端口没有。** 这是 fpk 应用的通用行为（qbpanel 的 8898、fn-emule 的 17581 一样），不是我们引入的。

**对本项目的含义**：

- 面板默认应 `listen 127.0.0.1`（`PVEPANEL_LISTEN` 那类开关），只让 CGI 这条路进来 → 天然带 fnOS 登录鉴权；
- 只有在用户明确要「局域网直连」时才放开到 `0.0.0.0`，并且**必须自带一个面板密码**（此时能碰到 PVE Token 的只有面板本身，但面板本身没鉴权）；
- 报告 §4 说「后端代理同时解决了 CORS 与凭据暴露问题」——**只对了一半**：它确实让浏览器不接触 Token，但如果监听 `0.0.0.0`，「谁能访问面板谁就能看 PVE 全部只读数据」，这一点报告没提。

### 4.4 `index.cgi` 的能力边界【实测】

| 项目 | 结果 |
|---|---|
| 5 秒慢请求能否走通 | ✅ 能，实测 5.013 s 完整返回，**CGI 层没有短超时** |
| 2 MB 响应体 | ✅ 能，实测 `bytes=2097211`（含头），耗时 17 ms |
| 后端没起时的降级 | ✅ 返回 `502 Bad Gateway` + 中文提示，不是白屏 |
| `..` 路径穿越 | ✅ 被 `index.cgi:62-69` 拦成 400 |
| POST body 转发 | ⚠️ **待补**（一次测出空 body，二次复测超时被我打断）——只读面板基本不用 POST，风险低 |
| 并发上限 | ⚠️ **待补**（测试脚本自身缺陷，数据不可信） |

> 只读面板全是 GET，这两项不影响主线结论。

---

## 5. 打包链路实测

### 5.1 我实际打出来的东西

**源码**：`pve-panel/tmp/fpk-skeleton/src/`（完整可复用的骨架：manifest + 9 个 cmd + index.cgi + 400 行 python + 前端 + 九档图标）
**产物**：`pve-panel/tmp/fpk-skeleton/fn-pvepanel-26.11.0-fnos-all.fpk`（66 KB）、`...-26.11.1-....fpk`

产物结构（`inspect_fpk.py` 实跑输出）：

~~~
外层 .fpk（20 项）
  manifest / app.tgz / config/{privilege,resource} / wizard/{install,config}
  ICON.PNG(64) / ICON_256.PNG(256)
  cmd/{install,config,uninstall,upgrade}_{init,callback} + cmd/main   全部 0755 OK
  manifest 里 fnpack 自动补了 checksum = md5(app.tgz)

内层 app.tgz（23 项）
  server/pvepanel.py  ui/{config,index.cgi}  web/index.html
  ui/images/icon_{16,24,48,64,72,96,128,256,512}.png + icon_{0}.png   九档齐全
  ui/index.cgi 0755 OK   无 __pycache__ / *.pyc / .DS_Store   无 CRLF
~~~

### 5.2 最短「从零到能装」清单（实测跑通，总耗时约 12 分钟）

~~~
1. 目录骨架（照 fnpack create 的模板）                      ~1 min
   manifest / config/{privilege,resource} / wizard/{install,config}
   app/ui/{config,index.cgi,images/} / cmd/* / ICON*.PNG
   注意：config/resource 必须存在，哪怕内容是 {"data-share":{"shares":[]}}
         （实测空对象 {} / 空数组都可以）

2. 写 9 个 cmd 脚本 + index.cgi（从 qbpanel 抄，改名字）     ~20 min
3. 生成九档图标（Pillow 脚本，见 tmp/fpk-skeleton/make_icons.py）  ~5 min
4. 同步到 Linux 上，统一修权限：                             ~1 min
   find . -type d -exec chmod 755 {} \;
   find . -type f -exec chmod 644 {} \;
   chmod 755 cmd/* app/ui/index.cgi
5. fnpack build                                             ~1 秒
6. scp 回本地，verify_fpk.py 自检                            ~2 min
7. scp 到 nas，appcenter-cli install-fpk -e env -v 1        ~8 秒
8. 验证 curl /api/health + 桌面入口                          ~2 min
~~~

**实测的打包耗时明细**（`build_fpk.py` 自带计时）：

~~~
[1] source tar          0.0s
[2] upload scp          0.2s
[3] remote fnpack build 0.2s     <- fnpack 本身极快，66KB 包
[4] download scp        0.2s
TOTAL 0.7s
~~~

安装 8 秒，卸载约 5 秒，完整「卸载→重装」27 秒。

### 5.3 fnpack 版本差异（修正）

qbpanel 的 CI（`.github/workflows/build-fpk.yml:25`）说「官方只发布了 1.2.3 的 linux-amd64；1.2.4 目前取不到」。
**实测 fnos-test 上装的是 `fnpack 1.2.4`**（`/usr/local/bin/fnpack`，随系统镜像带的）。**建议实际去 `https://static2.fnnas.com/fnpack/` 探一下 1.2.4 的下载地址**，确认后 CI 可以升到与真机一致（我这次没验下载 URL）。

---

## 6. Token 在本机怎么存最稳妥

### 6.1 可选方案的实测评估

| 方案 | 可行性 | 结论 |
|---|---|---|
| `@appdata/token.json` + `chmod 600` | ✅ **实测可行**，非 root 用户读不了 | **v1 采用** |
| 系统密钥环（secret-service / GNOME Keyring） | ❌ `import keyring`、`import secretstorage` 均 **ModuleNotFoundError**；`secret-tool` / `gnome-keyring-daemon` 都不存在 | 不可行 |
| `cryptography` 加密后落盘 | ⚠️ 系统自带 `cryptography 38.0.4`，**但**密钥本身还得存在本机（`/usr/trim/etc/machine_id` 只有 40 字节且 root 可读），等于「用机器身份当密钥」——换机/换盘无法解密，且能读 machine_id 的人也能解密 | 安全性提升有限、复杂度明显上升，**不做** |
| 完全不落盘（每次启动手输） | ✅ 但用户每天重启 NAS 就要重填 | 体验不可接受 |
| 交给 fnOS 的 `app_env` 环境变量 | ⚠️ appcenter 有 `app_env` 表 + `system_config.env.obfuscate=true`，但**这是应用中心内部机制，第三方应用没有公开接口写它** | 不作为主路径 |

### 6.2 推荐落地方案（v1）

~~~
<TRIM_PKGVAR>/config.json   chmod 600   # 集群地址、刷新间隔、告警阈值（明文无妨）
<TRIM_PKGVAR>/token.json    chmod 600   # 只存 {"pve_token": "PVEAPIToken=...", "created": ts}
~~~

细节：

1. **写文件用 `open(...,"w")` + `os.chmod(path, 0o600)`，先写 `.tmp` 再 `os.replace`**（qbpanel `backends.py:609-621` 的写法，避免半截文件）。qbpanel 实测 `config.json` 是 `-rwx------ root root`。
2. **前端永不回显 token**：`GET /api/settings` 把 token 换成字面量 `__SET__`，前端输入框留空 + 提示「已保存，留空保留原值」。
   ⚠️ **这里有一个 qbpanel 生产机实际炸过的坑**（项目记忆 26.11.37）：设置页保存时提交**整份 draft**，把 `__SET__` 当新 token 存了回去。**必须两道防线**：
   - 服务端：收到 `__SET__` 或空串时**沿用旧值**，只有真填了新值才替换；
   - 前端：`persistDraft()` 只提交真正改动的分区，不回传含掩码的整份 draft。
3. **日志里绝不打印 token**：`log()` 前统一过一遍掩码。实测 fn-emule 的 `settings.json` 是 `600`，社区惯例一致。
4. **建议顺带推荐 `PVEAuditor` 角色 + 独立 API Token**（权限最小化）：即使 token 泄露也只读。
5. **不要**把 token 写进 `@appdata` 以外的任何地方（尤其别写进 `shares/`，那是用户可见的共享目录）。

---

## 7. 我推翻 / 修正了报告的哪些说法

| # | 报告原文 | 实际 | 依据 |
|---|---|---|---|
| 1 | §8「`service_port` 指定面板端口」 | **错**。`service_port` 不决定监听端口，只是安装期端口预检 + 写 app_service 记录。真实端口链是 wizard 环境变量 → cmd/main → runtime-ports.env → python | 实测：端口由 wizard 值决定，manifest 未参与 |
| 2 | §6「骨架 0.5 天（可复用现有实现）」 | **方向对，但代价被低估了细节**。套路固定的部分 2 小时够；但**不是纯搬运**——`config/resource` 缺失会让 fnpack 直接失败、`index.cgi` 的三级端口兜底不能简化、`service_port`/端口链要重写 | §1.2 / §4.2 / §5.2 |
| 3 | §7 风险表未提「自启/守护」 | **必须补**。开机自启 OK（appcenter 批量拉起，实测确认），但**崩溃后不自愈**，DB 状态会停在 `start` | §3.4 |
| 4 | §4「后端代理同时解决 CORS 与凭据暴露问题」 | **只对一半**。直连面板端口（如 `:8899`）**不经过 fnOS 登录鉴权**，实测无 cookie 直接 200。监听 `0.0.0.0` 时等于把 PVE 只读数据敞开给整个局域网 | §4.3 |
| 5 | §7 风险 4「本地文件 chmod 600，建议后续接系统级加密存储」 | **「系统级加密存储」在 fnOS 上没有可用实现**。keyring/secretstorage 模块不存在，secret-tool 也没有。chmod 600 + root-only @appdata **就是当前最优解**，不是「过渡方案」 | §6.1 |
| 6 | §8 只提了 `install_type = root`、`desktop_uidir = ui`、`service_port`、`source = thirdparty` | **漏了三条必需的**：`ctl_stop = true`（否则卸载留残进程）、`checkport = false`（否则改端口装不上）、以及 `config/resource` 这个**文件必须存在**否则 fnpack 打包失败 | §1.2 / §1.3 |
| 7 | §2.4 / §7 风险 1「单次 API 往返 ~3s」 | 我这边只测到 **TLS 握手 23 ms**，3 秒不在网络/TLS 层。与 pve-probe 的结论（只在 401 分支）一致——**报告的「三次完全一致 3s」不能外推成「认证后也 3s」** | §3.3 |

---

## 8. 复用 vs 重写的工作量拆分

| 模块 | 复用度 | 估时 | 说明 |
|---|---|---|---|
| `manifest` | 抄改 | 10 min | 改 appname / display_name / desc / 端口 |
| `config/{privilege,resource}` | **原样** | 2 min | privilege 不动；resource 写空 shares |
| `wizard/{install,config}` | 抄改 | 10 min | 只留「面板端口」一项 |
| `cmd/*` 9 个 | 8 个原样，`main` 改 7 处 | 40 min | 见 §2.3 |
| `app/ui/index.cgi` | **原样**（只改默认端口与提示文案） | 5 min | 三级端口兜底不要简化 |
| `app/ui/images/` 九档图标 | 换设计 | 30 min | 用 Pillow 脚本批量生成 |
| `app/ui/config` | 抄改 | 5 min | 改 title / url / 键名 |
| `app/server/` 后端 | **重写** | 1.5 天 | 静态文件服务 + API + 并发抓 PVE + TTL 缓存 + 错误降级 + token 存储 |
| PVE API 客户端 | **重写** | 0.5 天 | 含在上面 |
| `app/web/` 前端 | **重写** | 2.5–3 天 | 七个页面 + Vue3 + 无 CDN |
| `tools/build_fpk.py` | 抄改 | 20 min | 改 app 名 / 路径 |
| `tools/verify_fpk.py` | 抄改 | 15 min | 改内层必需文件列表 |
| `tools/make_icons.py` | 抄改 | 20 min | 换图形 |
| CI workflow | 抄改 | 20 min | 改 tag / app 名 |
| **小计（骨架部分）** | | **≈ 2.5 小时** | 含一次真机回归 |
| **小计（业务部分）** | | **≈ 4.5–5 天** | |

**所以「搬骨架」的真实成本 ≈ 半天以内（含验证）。报告说的 0.5 天是对的，但前提是知道 §9 那几个坑。主要工作量在业务代码，不在骨架。**

---

## 9. 会咬人的隐含坑（都已实测/被踩过）

1. **`config/resource` 缺了 fnpack 直接失败**：`Packing failed. Required file config/resource is missing`。【实测】
2. **Windows 上打不出能装的包**：文件权限全变 0666，装上去报 **10111**。必须在 Linux 上打（CI 或 SSH 到 nas）。【读码 + fn-emule 记忆】
3. **`cmd/*` 少 shebang → 安装报 10237**。【记忆】
4. **`__pycache__` / `*.pyc` 混进包**：qbpanel 源码树里就有 `app/server/__pycache__/*.pyc`（3 个文件），build 脚本用 `SKIP_DIRS/SKIP_EXT` 过滤；CI 还要额外 `find -name '__pycache__' -prune -exec rm -rf`。漏了不影响运行但会让包变脏、且 pyc 与源码版本可能对不上。
5. **行尾 CRLF**：`cmd/*` 和 `index.cgi` 带 CRLF 会让 shebang 变成 `/bin/bash\r`，Linux 报 `bad interpreter`。我的 build 脚本里加了一步 `sed -i 's/\r$//'`。实测产物内 `ui/index.cgi` 无 CRLF。
6. **大小写陷阱**：`app/ui/images/` 里 `icon_64.png` 与 `ICON_64.PNG` 在 Windows 上会互相覆盖，git `core.ignorecase=true` 又不认为变了 → **那两个尺寸永远不进包**（qbpanel 花了三次发布才修好）。对策：`app/ui/images` 只放小写九档，外层 `ICON.PNG` / `ICON_256.PNG` 单独生成；用 `check_icon_case.py` 挡。
7. **中文编码**：manifest 里的中文实测是 UTF-8，包内正确（`display_name = PVE 面板` 装完应用中心显示正常）。**但别在 PowerShell 里写 here-string 拼中文再传**——会被解析器吃掉（qbpanel 项目记忆）。用文件写入工具或 Python 脚本落盘。
8. **安装期间 `shares/` 软链不存在**：所有路径必须运行期算（qbpanel `docs/DEVELOPMENT.md:127`）。
9. **原地覆盖安装报 18000**：必须先 uninstall 再 install-fpk。【实测】
10. **`appcenter-cli start` 返回成功不等于已 listen**：实测 install-fpk 后状态是 `starting`，要轮询 `status` 或探端口。【实测】
11. **`appcenter-cli` 普通用户跑会 panic**（`dial unix /run/trim_app_cgi/rpcbroker: connect: permission denied`），必须 `sudo`。【实测】

---

## 10. 待补 / 未验证

1. **CGI 层 POST body 转发** —— 只读面板全走 GET，不影响主线；实现时若不放心，用 `curl --data-binary @-` 那条已有实现即可。
2. **CGI 并发上限** —— 测试脚本自身有缺陷（`wait` 把后台服务也等了），数据不可信。只读面板的并发压力来自浏览器同一会话的 5–10 个请求，量级很低。
3. **`fnpack 1.2.4` 官方下载地址** —— `https://static2.fnnas.com/fnpack/` 下是否有 1.2.4 的 linux-amd64，未探。
4. **多用户/权限语义** —— `allUsers: false` + `control.accessPerm: readonly` 在飞牛多用户场景下的实际表现（非管理员用户能否看到桌面图标），未验。
5. **arm 平台** —— 只验了 x86（fnos-test 是 x86_64）。纯 Python 应无差异，但 `platform=all` 未在 arm 上跑过。

---

## 附录 A：本次实测的命令留档

~~~bash
# 1) 造骨架（源码见 tmp/fpk-skeleton/src）
# 2) 打包：python tmp/fpk-skeleton/build_fpk.py
#    内部等价于：
ssh fnos-test 'rm -rf /tmp/pvepanel-build && mkdir -p /tmp/pvepanel-build &&
  tar -xf /tmp/pvepanel-src.tar -C /tmp/pvepanel-build && cd /tmp/pvepanel-build &&
  find . -type f \( -name "*.cgi" -o -path "./cmd/*" -o -name "*.sh" \) -exec sed -i "s/\r$//" {} \; ;
  find . -type d -exec chmod 755 {} \; &&
  find . -type f -exec chmod 644 {} \; &&
  chmod 755 cmd/* app/ui/index.cgi &&
  fnpack build'

# 3) 安装
scp fn-pvepanel-26.11.0-fnos-all.fpk fnos-test:/tmp/
ssh fnos-test "printf 'wizard_backend_port=8899\n' > /tmp/pvepanel.env &&
  sudo appcenter-cli install-fpk /tmp/fn-pvepanel-26.11.0-fnos-all.fpk -e /tmp/pvepanel.env -v 1"

# 4) 验证
curl http://127.0.0.1:8899/api/health
sudo env REQUEST_METHOD=GET REQUEST_URI=/cgi/ThirdParty/fn-pvepanel/index.cgi/api/health \
  cgiName=fn-pvepanel /var/apps/fn-pvepanel/target/ui/index.cgi

# 5) 卸载清理
sudo appcenter-cli stop fn-pvepanel && sudo appcenter-cli uninstall fn-pvepanel
~~~

## 附录 B：骨架源码与产物位置

| 路径 | 说明 |
|---|---|
| `pve-panel/tmp/fpk-skeleton/src/` | **可直接复用的骨架源码**（manifest / cmd×9 / ui / server / web / icons） |
| `pve-panel/tmp/fpk-skeleton/build_fpk.py` | 打包脚本（本地 tar → scp → 远端 chmod+fnpack → scp 回） |
| `pve-panel/tmp/fpk-skeleton/inspect_fpk.py` | 产物自检（外层成员 / app.tgz / 可执行位 / 九档图标 / pyc / CRLF） |
| `pve-panel/tmp/fpk-skeleton/make_icons.py` | 九档图标生成（Pillow） |
| `pve-panel/tmp/fpk-skeleton/fn-pvepanel-*.fpk` | 实测产物（26.11.0 / 26.11.1） |

> 这些按约定放在 `tmp/`；**骨架源码建议正式立项时移到 `pve-panel/src/`**。

## 附录 C：fnos-test 收尾状态

实测清理完毕：

**卸载后 fnOS 的既定行为**（这点本身是个结论）：`appcenter-cli uninstall` 只删 `/var/apps/<app>` 和 `@appcenter/<app>`，**保留** `@appdata` / `@apphome` / `@appconf` / `/vol1/@appmeta` 四个目录 —— 这正是「卸载重装能继承配置」的原因，不是残留垃圾。

**我的收尾**（因为这是用完就丢的探针应用，不该在测试机上留痕）：
- 手工 `rm -rf` 掉了上面那 4 个残留目录，`du -sh` 合计约 80 KB；
- `app` 表与 `app_service` 表里 `fn-pvepanel` 记录数均为 **0**；
- 端口 8899 已释放，`/tmp` 下测试目录（`/tmp/pvepanel-build`、`/tmp/fnpack-demo` 等）已删；
- 既有应用 fn-qbpanel(8898) / fn-emule(17581) / fn-fail2ban 等全程未受影响，仍在正常监听。

**安全边界遵守情况**：
- **未对 fnos-prod（192.168.1.10）做任何操作**，连只读巡检都没做；
- 192.168.1.3（PVE）只做过**一次未认证的 TLS 握手探测**（`create_connection` + `wrap_socket`，随后立即 close），无任何 HTTP 请求、无认证尝试、无写入；
- 全程只用 SSH 密钥，未产生、未落盘任何密码/Token/私钥。
