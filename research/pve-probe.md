# PVE 侧真实行为与延迟画像（实测）

- 探测日期：2026-11-05（Asia/Shanghai）
- 探测方：从 `fnos-test`(192.168.1.13) 出发（网关 192.168.1.2，同网段直连），另用 Windows 本机与 `fnos-prod`(192.168.1.10) 交叉验证
- 被探测方：PVE `192.168.1.3:8006`（节点名 `Chin`）
- 探测性质：**纯只读**。全程只发 GET；只发过两次 POST，且都是**空 body** 打 `/api2/json/access/ticket`（用来观察参数校验分支的耗时），**没有提交任何用户名/密码，没有任何字典或爆破尝试**。
- 本文严格区分 **【实测】**（有命令与输出）与 **【推断/源码】**（读源码或推理得出，未在真实登录态直接验证）。

---

## 〇、结论摘要

1. **【实测+源码】那 3 秒不是"API 往返延迟"，是 PVE 对 `HTTP 401` 的固定惩罚。**
   同一套 `/api2/json/*` 路径、同一个 `pve-api-daemon/3.0`：需认证的端点未认证 → 3.005s；**免认证的 200 端点 → 0.0035~0.005s**。
   参考报告 §2.4 说"3 秒发生在服务端响应阶段"方向对，但没定位到「**只在认证失败分支**」，因此风险 1 的影响面被写偏了。

2. **【源码】3 秒是"距请求开始的差值补齐"，不是"额外加 3 秒"。**
   `AnyEvent.pm:1041` 为 `$delay = 3 - tv_interval($reqstate->{starttime})`；`AnyEvent.pm:1644` 为 `my $delay = 3`（auth_handler 抛异常时）。
   因此实测落在 **3.004~3.056s**，**永不小于 3.00**，抖动只向上。

3. **【实测】3 秒惩罚可被并发完全吃掉，不是全局锁。**
   8 / 16 / 40 / 100 / 150 / 200 路并发 401：每路仍 ~3.01s，**墙钟也只 ~3.0~3.3s**。
   串行 8 端点 = 24082ms；并发 8 端点 = 3019ms，差 8 倍。

4. **【实测】认证成功后的往返不是 3s 量级。**
   用同一 daemon、同一条 `/api2/json` 管线里**免认证的 200 端点** `/api2/json/access/domains` 做"传输+调度"基准：
   单请求 p50 **4.4ms**；300 并发 **wall 302ms、零错误**；keep-alive 复用后 **0.35~0.45ms/请求**。
   即"网络 + daemon 调度"这段被实测框死在**个位数毫秒**。

5. **【实测】HTTP/2 不支持（无 ALPN），只有 HTTP/1.1；2xx 保活，4xx 强制 `Connection: close`。**
   客户端应复用长连接（快 6~10 倍），但不要指望多路复用。

6. **架构结论**：报告"必须并发抓取 + 缓存"**保留**，但理由要换 —— 不是为了扛常态 3s，而是：
   (a) Token 失效时把故障成本从"8 端点 × 3s = 24s 假死"压到"一次 3s 内报错并熔断"；
   (b) rrddata / SMART 这类后端计算型端点耗时另计，串行会叠加。

---

## 一、延迟画像：3 秒到底是什么

### 1.1 【实测】同主机同端口、不同路径的耗时分解

命令（在 `fnos-test` 上，curl 7.88.1 / OpenSSL 3.0.20）：

```bash
curl -sk -o /dev/null \
  -w '%{http_code}|dns=%{time_namelookup}|conn=%{time_connect}|tls=%{time_appconnect}|ttfb=%{time_starttransfer}|total=%{time_total}\n' \
  --max-time 30 'https://192.168.1.3:8006<PATH>'
```

| 路径 | 状态 | dns | tcp | tls | **ttfb** | 说明 |
|---|---|---|---|---|---|---|
| `/` | 200 | 0.00002 | 0.00035 | 0.00310 | **0.0248 首次 / 0.0056** | 静态页，快路径 |
| `/api2/json/access/ticket`（GET） | **200** | 0.00002 | 0.00021 | 0.00334 | **0.0048** | 免认证 200，**不慢** |
| `/api2/json/access/domains` | **200** | — | — | — | **0.0040~0.0049** | 免认证 200，**不慢** |
| `/api2/json/version` | **401** | 0.00001 | 0.00014 | 0.00275 | **3.0065** | 需认证 → 吃满 3s |
| `/api2/json/nodes` | **401** | 0.00002 | 0.00028 | 0.00399 | **3.0075** | 需认证 → 吃满 3s |
| `/api2/json/nonexistent` | **401** | 0.00002 | 0.00021 | 0.00349 | **3.0056** | **不存在的路径也是 401 + 3s** |
| `/api2/extjs/version` | 401 | — | — | — | **3.0073** | extjs 格式一样 |
| `/pve2/` | 500 | — | — | — | **0.0035** | 静态分支，不走 auth |
| `POST /api2/json/access/ticket`（空 body） | **400** | — | — | — | **0.0063~0.0081** | 参数校验失败 → 快，**不吃 3s** |

**判据成立**：网络层（dns+tcp+tls）稳定在 3~4ms，**3 秒 100% 发生在服务端决定回什么之后、真正 write 之前，且只挂在 401 上**。
`/api2/json/nonexistent` 也是 401 —— PVE 在"找不到端点"之前先判认证，所以**路径写错不会让面板更快暴露问题**。

### 1.2 【实测】3 秒是 per-request 独立计时，不是全局锁

| 并发 | 墙钟 | 单请求 min / p50 / max |
|---|---|---|
| 5 串行 | **15070 ms** | 3.005 / 3.006 / 3.008 |
| 5 并发 | **3018 ms** | ~3.007 |
| 8 并发 | ~3010 ms | 3.0077 / 3.0094 / 3.0111 |
| 16 并发 | **3028 ms** | 3.0074 / — / 3.0184 |
| 40 并发 | **3065 ms** | 3.0097 / 3.0258 / 3.0451 |
| 100 并发 | **3158 ms** | 3.0063 / 3.0151 / 3.0403 |
| 150 并发 | **3182 ms** | 3.0060 / 3.0211 / 3.0568 |
| 200 并发（Python 200 线程） | **3247 ms** | 3.0054 / 3.0090 / 3.0167 |

**读法**：并发 1→200，单请求只从 3.005s 爬到 3.02~3.06s（多排队 10~50ms），墙钟几乎不变 → **每请求各自挂一个 AnyEvent 定时器，事件循环不阻塞、不互斥。**

### 1.3 【实测】事件循环确实没被堵住

20 个 401 还挂在 3s 定时器里时，同时打快路径：

```
/                         ttfb=0.0057 0.0057 0.0047 0.0047 0.0056   (200)
/api2/json/access/domains ttfb=0.0035 0.0037 0.0038                  (200)
```

→ 3 秒等待期间，服务对其它请求仍是 4~6ms。**"部分页面慢"不会扩散成全站卡死。**

### 1.4 【实测】keep-alive 是否线性累加

- **401 分支**：响应头带 `Connection: close`（源码 `AnyEvent.pm:325`：`$reqstate->{keep_alive} = 0 if ($code >= 400)`）。curl 实测 "Closing connection 0"。
  → **每个 401 都要重做 TLS 握手（约 3ms）**。同一条 curl 命令里连发 3 个 401，三次 `conn`/`tls` 都非零（0.0002/0.0031、0.0001/0.0025、0.0001/0.0019）——**确实是三条新连接**，各吃各的 3s，**不会累加成 3/6/9s**。
- **200 分支**：`Connection: Keep-Alive`，curl 显示 "Re-using existing connection #0"。
  5 个请求同连接：**0.00379 / 0.00060 / 0.00040 / 0.00035 / 0.00033**
  同 5 个但 `--no-keepalive`：**0.00374 / 0.00375 / 0.00356 / 0.00348 / 0.00355**
  → **复用后 0.3~0.6ms，不复用 3.5ms，差约 10 倍。**

### 1.5 【源码】3 秒是 PVE 官方实现的"未授权固定延时"

[pve-http-server / AnyEvent.pm](https://github.com/proxmox/pve-http-server/blob/master/src/PVE/APIServer/AnyEvent.pm)：

```perl
# 1038-1043 行：正常鉴权流程结束后，status 为 401 时补延时
my $delay = 0;
if ($res->{status} == HTTP_UNAUTHORIZED) {
    # always delay unauthorized calls by 3 seconds
    $delay = 3 - tv_interval($reqstate->{starttime});
    $delay = 0 if $delay < 0;
}

# 1643-1644 行：auth_handler 抛异常（票/Token 无效）时
# always delay unauthorized calls by 3 seconds
my $delay = 3;

# 388-398 行：response() 用 AnyEvent 定时器把回复压住，到点再 push_write
} elsif ($delay && $delay > 0) {
    my $w;
    $w = AnyEvent->timer(after => $delay, cb => sub {
        undef $w;
        return if !$reqstate->{hdl};   # 客户端已断开就丢弃
        $reqstate->{hdl}->push_write($res);
        $self->finish_response($reqstate);
    });
}
```

**一条源码解释三件事**：为什么"恰好 3s、绝不小于"（`3 - 已耗时` 补齐）；为什么"200 并发不变长"（每请求一个 timer）；为什么"只有 401 才有"（`$delay` 默认 0）。

### 1.6 【实测】不是网络路径 / 前置设备造成的

| 起点 | `/`（200） | `/api2/json/access/domains`（200） | `/api2/json/version`（401） |
|---|---|---|---|
| fnos-test（192.168.1.13，同网段，网关 192.168.1.2） | 0.0056~0.0063 | 0.0040~0.0049 | **3.0051~3.0082** |
| Windows 本机 | 0.0085~0.0121 | — | **3.0075~3.0288** |
| fnos-prod（192.168.1.10） | 0.0061 | 0.0039 | **3.0063** |

**三台机器、三条链路：401 都是 3.00x，200 都是个位数毫秒** → 排除本机路由/代理因素，确切落在 PVE 自身。

反证：
- 同机 **3128（spiceproxy，另一个 daemon）**：`GET /` 与 `GET /bogus` 都返回 **501，ttfb 1.1~1.4ms** —— 那里**没有** 3s 逻辑。
- 同机端口：8006 开、3128 开、22 开；**8007 关闭、80 关闭**。

### 1.7 【实测】不是限流/封禁的渐进惩罚

| 前置动作 | 之后快路径 | 之后 401 |
|---|---|---|
| 40 连发 | — | 3.0053 / 3.0044 / 3.0065 …（无变化） |
| 200 并发 | `/` 0.0046 (200)、domains 0.0038 (200) | **3.0054（仍是 3.00，没升级）** |
| 150 + 300 并发（累计 ~450 次 401） | — | 3.0036~3.0136，**没有 6s/10s，也没有 429** |

累计打了 **1000 次以上 401**：全部 401、全部 ~3.0s，**没有出现 429 / 封锁 / 延时递增**。
【源码】`AnyEvent.pm` 中搜 `429|rate_limit|ratelimit|fail2ban|throttl` —— **无命中**（唯一的 429 是字体 `woff` 的 MIME 表项）。
→ PVE 默认**不做**基于频率的封禁，只有这条固定 3s。

---

## 二、认证成功后的真实耗时（这是"地板"，不是最终答案）

> 诚实说明：**手上没有 PVE API Token**（见第四节），所以"认证成功后"没能在真实登录态直接测。
> 下面用**同一个 daemon、同一条 `/api2/json` 管线里唯一免认证的 200 端点**做"传输 + 调度"基准。
> 它是**地板值**：认证请求耗时 = 这个地板 + 后端真正干活的时间。

### 2.1 【实测】单请求分布（`/api2/json/access/domains`，50 次）

p50 **4.4ms**，p95 **6.5ms**，max **8.9ms**，min **3.4ms**。

### 2.2 【实测】并发扫描（找拐点）

| 并发 | 墙钟 | 单请求 p50 | max | 错误 |
|---|---|---|---|---|
| 1 | 6.9ms | 5.9ms | 5.9ms | 0 |
| 3 | 6.0ms | 4.6ms | 5.1ms | 0 |
| 6 | 8.7ms | 5.7ms | 7.8ms | 0 |
| 12 | 13.7ms | 7.4ms | 8.4ms | 0 |
| 30 | 35.5ms | 14.0ms | 19.8ms | 0 |
| 60 | 70.9ms | 17.6ms | 31.1ms | 0 |
| 120 | 140.2ms | 24.9ms | 50.5ms | 0 |
| 200 | **259ms** | 37.0ms | 89.8ms | **0** |
| 300 | **302ms** | 30.1ms | 81.2ms | **0** |

吞吐随并发近似线性上升，直到 200 并发仍是"墙钟 ≈ 并发 × 1.3ms"，**没有排队塌方**。
【源码】`pveproxy.pm` / `pvedaemon.pm` 默认 `max_workers => 3`、`max_conn => 500`；`AnyEvent.pm` 默认 `max_conn = 800`。
→ **worker = 接受连接的进程数，每个 worker 是事件循环、能同时带多个连接**，不是"同时只能处理 3 个请求"。
→ 但也**别堆连接数**：默认 3 worker + 500/800 上限，面板开 4~8 条长连接足够。

### 2.3 【实测】keep-alive 收益（stdlib 客户端）

| 场景 | 结果 |
|---|---|
| 40 请求 / **1 条** keep-alive 连接 / 串行 | **0.018s（0.45ms/请求）** |
| 40 请求 / **8 条** keep-alive 连接 / 并行 | **0.014s（0.35ms/请求）** |
| 10 请求 / **每次新建 TLS 连接** | **0.027s（2.69ms/请求）** |

**一次 TLS 握手 ≈ 2.3~3.2ms，比一次 API 处理还贵** → 后端必须维护长连接。

### 2.4 【实测】协议能力

| 项 | 结果 |
|---|---|
| ALPN | **No ALPN negotiated**（HTTP/2 不支持） |
| `curl --http2` | 实际仍 `ver=1.1` |
| `curl --http2-prior-knowledge` | **失败，code=000** |
| TLS | 协商 **TLSv1.3 / TLS_AES_256_GCM_SHA384**；`-tlsv1_2` 也能连 |
| gzip | **支持**：`pvemanagerlib.js` 2,425,647 B → **404,795 B**（5.9 倍） |
| HEAD / OPTIONS | 均 **501** |
| keep-alive | 2xx 保活；**4xx/5xx 一律 `Connection: close`** |

---

## 三、TLS / 证书 / 服务指纹

### 3.1 【实测】证书

| 项 | 值 |
|---|---|
| subject | `OU = PVE Cluster Node, O = Proxmox Virtual Environment, CN = Chin.com` |
| issuer | `CN = Proxmox Virtual Environment, OU = 850f69d5-a795-425a-8955-77aec6a322cb, O = PVE Cluster Manager CA` |
| 有效期 | **2026-07-18 16:10:19 GMT → 2028-07-17 16:10:19 GMT** |
| 自签 | 是（PVE Cluster Manager CA 签发，**CA 不在 chain 里**） |
| chain 证书数 | **1 张（只有 leaf）** |
| SAN | `IP:127.0.0.1, IP:::1, DNS:localhost, IP:192.168.1.3, DNS:Chin, DNS:Chin.com` |
| key | RSA 2048 / RSA-SHA256 |
| `openssl verify` | **return code 21（unable to verify the first certificate）** |
| SHA-256 指纹 | `9A:5F:D9:44:92:33:AD:E3:D1:08:8F:BF:F1:5B:CD:B3:A2:89:AA:C9:45:92:91:DD:06:5D:35:D4:D5:34:BB:15` |

**判定**：SAN 里**有 `IP:192.168.1.3`**，但 **CA 不在证书链里**，`CERT_REQUIRED` 必然失败。
→ 面板要么 `CERT_NONE`，要么让用户导入 CA/leaf。
【推断】`create_default_context + check_hostname=False + verify_mode=CERT_NONE` 是**目前唯一零配置可用**的路（报告 §2.3 做法成立）。更稳的是**默认不校验 + 设置页允许粘贴 PEM 做指纹 pinning**（比对上面的 SHA-256 或整张 leaf）。

### 3.2 【实测】免认证可见的服务指纹

| 指纹 | 值 | 来源 |
|---|---|---|
| `Server` 头 | **`pve-api-daemon/3.0`** | 所有响应（含 401/500） |
| PVE 版本 | **`9.2.21`** | 根页面 `?ver=9.2.21`（`/` **200，免认证**） |
| proxmox-widget-toolkit | **`v5.2.10-t1789733295`** | `/proxmoxlib.js` 首行注释 |
| ExtJS | 7.0.0 | `pve2/ext6/ext-all.js?ver=7.0.0` |
| 节点名 | **`Chin`** | 根页面内联 `NodeName: 'Chin'` |
| 架构 | `amd64` | 根页面 `NodeArch` |
| 认证域 | `pve`（PVE 认证）、`pam`（Linux PAM） | **`GET /api2/json/access/domains` → 200，免认证** |
| PSI 压力字段 | `pressurecpufull / cpusome / iofull / iosome / memoryfull / memorysome` 全在 `pvemanagerlib.js` 中 | 说明 **PVE 9.x，PSI 字段齐全** |
| 401 响应体 | **空**（0 字节，无 `Content-Length`） | `-D` dump |
| 不存在路径 | **500 `no such file '/xxx'`**（不是 404） | `/pve2/index.html`、`/robots.txt` |
| 根页面 title | `Chin - Proxmox Virtual Environment` | `/` |

**对面板的直接价值**：
- **首屏在拿到 Token 之前就能显示"PVE 版本 / 节点名 / 可达性"** —— 打 `GET /`（5ms）正则抠 `ver=` 与 `NodeName` 即可，**完全不吃 3s**。
- **探活/健康检查应打 `/` 或 `/api2/json/access/domains`（都 200，4~6ms），绝不要打 `/api2/json/version`（401，3s）。** 若按 version 端点探活，每次白烧 3 秒。

### 3.3 【实测】PVE 版本区间

`ver=9.2.21` → **PVE 9.2.x**。报告 §7 风险 2 假设"PSI 仅新版有"，在**目标机上是 PVE 9.2、PSI 齐全**，风险下调；代码仍按"字段缺失即降级"写以兼容别的集群。

---

## 四、Token / 凭据线索（只报存在性，一个字节都没读）

**结论：没有找到任何可用的 PVE API Token。**

| 位置 | 结果 |
|---|---|
| `fnos-test` 环境变量（`env \| grep -iE 'pve\|proxmox'`） | 无 |
| `fnos-test` 的 `/root`、`/home/*`、`/etc` 下带 pve/prox 字样的文件 | 无 |
| `fnos-test` 上 `~/.netrc`、pve-panel 配置 | 均不存在 |
| Windows 环境变量（仅列名字） | 无 PVE/PROXMOX 相关 |
| `C:\Users\PVE\.ssh\config` 的 Host 条目 | 只有 `fnos-prod` / `fnos-test` / 两个 `-smb`，**无 PVE 条目** |
| Windows 凭据管理器（仅 `cmdkey /list` 的 Target 名） | 与 PVE 相关**只有 `Domain:target=Chin`**（Chin 是 PVE 节点名，但那是 **Windows 域凭据**语义，不是 PVE API Token；**未读取、未使用**） |
| 工作区带 pve 字样的文件 | 只有 `pve-panel/tmp/fpk-skeleton/fn-pvepanel-26.11.0-fnos-all.fpk`（别的成员骨架产物，未读） |

→ **【待用户提供】** 需要一个**只读** Token，格式 `PVEAPIToken=<user>@<realm>!<tokenid>=<uuid>`，权限建议 `PVEAuditor`（全局只读）。
拿到后需补测的唯一关键项：**认证态下 `/cluster/resources`、`/nodes/{node}/status`、`/rrddata`、`/disks/smart` 的真实耗时**（本文只有"传输+调度地板"）。

---

## 五、面板真实可用的刷新节奏

### 5.1 【实测】三种刷新模型（8 个端点）

| 模型 | 墙钟 |
|---|---|
| 串行 8 端点，**Token 缺失/失效** | **24082 ms** ← 这就是"面板假死"的真相 |
| 并行(8) 8 端点，**Token 缺失/失效** | **3019 ms** |
| 串行 8 端点，**有 Token**（按 4ms 地板） | **≈33 ms**（后端耗时另计） |
| 8 条 keep-alive 连接并行，**有 Token** | **≈5~10 ms** |

### 5.2 三条硬性设计约束（都有实测支撑）

1. **【必须】认证失败必须"一次就停"。** Token 无效时每请求 3s 且断连。无脑串行 → 24s（N=3 节点 60s）；无脑并发 → 3s+ 后才拿到一堆 401。
   正确做法：**首个 401 立刻终止本轮、cancel 其余在飞请求、进入"认证失效"状态并停止轮询**（否则每 10 秒烧 3 秒）。
2. **【必须】连接要长，并发度不要大。** 实测 keep-alive 让单请求 2.7ms → 0.45ms（6 倍）；服务端默认 3 worker / 500~800 连接。建议 **4~6 条长连接**（节点一批、rrddata 一批），不要每请求新建、也不要开几十条。
3. **【必须】缓存分层，TTL 按"数据变化速度"定。** rrddata / SMART / tasks 即便传输是毫秒级也不该每 10 秒拉。

### 5.3 建议刷新节奏（可直接写进设计）

| 数据 | 端点 | 建议 TTL / 节奏 | 理由 |
|---|---|---|---|
| 节点可达性 + 版本 + 节点名 | `GET /`（200，免认证） | **5 s** | 4~6ms 白送；Token 失效时仍能显示"PVE 在线" |
| 认证有效性 | `GET /api2/json/version` | **30 s 一次**（失败即熔断） | 成功 5ms、失败 3s，不能高频 |
| 集群总览 | `/cluster/resources` | **5~10 s** | 一次拿全量 VM/CT/存储，避免 N×M 放大 |
| 节点实时状态 | `/nodes/{n}/status` | **5~10 s** | pvestatd 本身 10s 周期，更快没意义 |
| VM/CT 列表状态 | `/nodes/{n}/qemu`、`/lxc` | **5~10 s** | 同上 |
| 存储容量 | `/nodes/{n}/storage` | **30 s** | 变化慢 |
| 曲线 rrddata | `/nodes/{n}/rrddata` | **60~300 s**（仅曲线页可见时拉） | 后端计算重，RRD 粒度 1 分钟 |
| 物理盘 / SMART | `/disks/list`、`/disks/smart` | **300 s** | smartctl 最慢的一类 |
| 任务 / 订阅 | `/tasks`、`/subscription` | **60 s** | 变化慢 |

**一次全量首屏（1 节点、8 端点、有 Token、4 条长连接）**：地板 ≈ **5~10 ms**，加后端计算估计 **100~300 ms** 一档 —— 但**骨架屏仍值得做**，因为"Token 错误 / 网络断"会直接卡 3s。

**N 节点放大**：请求数 ≈ `2 + 6N`。有 Token 时 4 连接并行，N=8 首屏仍 <1s；**Token 失效时**串行是 `(2+6N) × 3s`（N=3 就是 60s），并发也要 3s+。→ **"401 熔断"是必须项，不是优化项。**

### 5.4 对参考报告的修正

| 报告原文 | 实测结论 | 影响 |
|---|---|---|
| §2.4 "3 秒延迟全部发生在服务端响应阶段…符合『防爆破固定延时』或代理层节流" | **方向对但没定位根因**：是 `pve-http-server` 对 `401` 的显式 3s 惩罚（`AnyEvent.pm:1041 / 1644`），且是 `3 - elapsed` **补齐**而非叠加；实测 3.004~3.056s，只向上抖 | 从"未知的服务端延时"变成"已知、可绕开的失败路径惩罚" |
| §2.4 待确认"认证成功后是否仍有该延迟" | **已定性：不会有。** 同 daemon 免认证 200 端点 p50 4.4ms，300 并发零错误 | **报告风险 1「单次 API 往返 ~3s」在常态不成立** |
| §7 风险 1 "单次 API 往返 ~3s → 首屏慢、轮询压力大" | **常态不成立；故障态成立且更严重**：正常 4~10ms；Token 失效时 8 端点串行 24s、N=3 时 60s | 缓解手段从"扛延迟"改为"**401 熔断 + 长连接 + 分层 TTL**" |
| §7 风险 2 "PSI `pressure*` 仅新版有" | 目标机 **PVE 9.2.21**，PSI 六字段齐全 | 风险下调；仍保留字段存在性判断 |
| §2.2 "根路径 200" | 不止 200：**根页面免费泄露节点名 `Chin`、PVE `9.2.21`、架构、widget-toolkit 版本**；`/api2/json/access/domains` 也免认证 200 | **新增能力**：Token 未配置就能显示"版本 + 节点名 + 认证域" |
| §7 风险 5 "默认忽略校验 + 设置页导入 CA" | **同意**，并补充：leaf 的 SAN **有 `IP:192.168.1.3`** 但 **CA 不在 chain 里**（`openssl verify` 返回 21），"只加主机名校验"救不了，必须 `CERT_NONE` 或导入 CA / pinning | 实现路径明确 |
| 报告未覆盖 | **HTTP/2 不支持（无 ALPN）**；**2xx keep-alive / 4xx 强制 close**；**gzip 可用（2.4MB→405KB）**；**HEAD/OPTIONS 501**；**不存在路径返回 500 而非 404**；**PVE 默认无频率封禁（1000+ 次 401 无 429、无递增）** | 客户端实现细节 |

---

## 六、复现清单（命令原样）

```bash
# 0) 起点
ssh fnos-test

# 1) 单路径耗时分解（<PATH> 换成 / 或 /api2/json/version 等）
curl -sk -o /dev/null \
  -w '%{http_code}|dns=%{time_namelookup}|conn=%{time_connect}|tls=%{time_appconnect}|ttfb=%{time_starttransfer}|total=%{time_total}\n' \
  --max-time 30 'https://192.168.1.3:8006/api2/json/version'

# 2) 并发是否串行化（8 路）
S=$(date +%s%N)
for i in $(seq 1 8); do ( curl -sk -o /dev/null -w "%{time_total}\n" --max-time 60 \
    'https://192.168.1.3:8006/api2/json/version' ) & done
wait
E=$(date +%s%N); echo "wall_ms=$(( (E-S)/1000000 ))"

# 3) keep-alive 对比
curl -sk -o /dev/null -o /dev/null -o /dev/null --max-time 20 \
  -w "%{time_total} " 'https://192.168.1.3:8006/api2/json/access/domains' \
                    'https://192.168.1.3:8006/api2/json/access/domains' \
                    'https://192.168.1.3:8006/api2/json/access/domains'
for i in 1 2 3; do curl -sk --no-keepalive -o /dev/null -w "%{time_total} " \
  'https://192.168.1.3:8006/api2/json/access/domains'; done

# 4) 免认证指纹
curl -sk 'https://192.168.1.3:8006/' | grep -E "NodeName|ver="
curl -sk 'https://192.168.1.3:8006/api2/json/access/domains'

# 5) 证书
echo | openssl s_client -connect 192.168.1.3:8006 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates -ext subjectAltName

# 6) 协议能力
echo | openssl s_client -connect 192.168.1.3:8006 -alpn 'h2,http/1.1' 2>/dev/null | grep -i alpn
curl -sk --http2-prior-knowledge -o /dev/null -w 'ver=%{http_version}\n' 'https://192.168.1.3:8006/'
```

> 全程未做任何写操作，未提交任何凭据。

---

## 七、三条行动建议

1. **按"常态毫秒、故障态 3s 熔断"设计**，不要按"常态 3s"设计（否则 TTL 与并发度都会定偏）。
2. **探活打 `/` 或 `/api2/json/access/domains`（200、4~6ms），永远不要拿需认证端点做探活。**
3. **拿到只读 Token 后必做一次补测**：`/cluster/resources`、`/nodes/{n}/status`、`/rrddata`、`/disks/smart` 的真实耗时（尤其 `disks/smart` 会调 smartctl，很可能是全场最慢）。
