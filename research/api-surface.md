# PVE API 能力边界与权限模型 —— 权威源核对

- 作者：teammate `api-surface`（task-3）
- 日期：2026-11-05
- 核对对象：参考报告 `D:\Workspace\WorkBuddy\pve-panel\report\2026-11-05-PVE监控面板可行性分析.md` 第二、三节
- 方法：**只读权威源核对**，不做任何 PVE 写操作、不爆破认证
  - 官方 API schema：`https://pve.proxmox.com/pve-docs/api-viewer/apidoc.js`（由同目录 `index.html` 加载，即官方 API Viewer 的数据源）
  - 源码：GitHub 镜像 `github.com/proxmox/*`，`master`（= PVE 9.2.26）、`stable-8`（= PVE 8.4.22）、`stable-7`（= PVE 7.4-20）三分支逐一对比
  - 官方文档：`https://pve.proxmox.com/pve-docs/chapter-pveum.html`、`pve-docs` 仓库 `pveum.adoc`
- 证据分级：**【源码/官方 schema 证实】**= 直接读到代码或官方定义；**【社区说法】**= 仅第三方资料，本文不依赖
- 原始抓取物与解析脚本：`pve-panel/tmp/api-surface/`（`apidoc.js`、`schema.json`、`endpoints.json`、`ver/*.pm` 各版本源码切片）

---

## 零、结论速览

| # | 结论 | 与参考报告的关系 |
|---|---|---|
| 1 | **3 秒延迟是源码里硬编码的「未认证/认证失败固定延时」，只挂在 401 分支**；403（权限不足）与 2xx 完全不加延时，不是限速、不是 fail2ban、没有渐进退避 | **推翻**报告第 2.4 节的「待确认项」与第 7 节风险 1 的推论 |
| 2 | 官方 schema 里 `temperature` / `sensor` / `thermal` / `fan` / `rpm` 命中**均为 0**，报告的「0 命中」属实；但**磁盘温度确实拿得到**，只是没有独立字段名 | **确认 + 补精确字段** |
| 3 | 磁盘温度确切取法：`GET /nodes/{node}/disks/smart?disk=/dev/sdX` 的 `attributes[]` 里 ATA 盘找 `id=="194"`（`Temperature_Celsius`），部分盘用厂商属性（`id 190` `Airflow_Temperature_Cel`），温度在 `raw` 字符串首段；NVMe/SAS 温度只在 `text` 自由文本里 | 报告只说「温度在此」，**精确化** |
| 4 | `pressure*`（PSI）**是 PVE 9 独有**，PVE 7/8 的 VM/CT 状态里一个都没有；`memhost` 同样 9 才有 | **确认并给出降级规则** |
| 5 | 节点 `rrddata` **没有 `diskread`/`diskwrite` 这两个数据源**（那是 VM/CT 级才有的） | **推翻**报告第 3 节该行 |
| 6 | `/nodes/{node}/network` **只有配置，没有流量统计**；全 API 没有「单网卡吞吐」端点 | **推翻**报告第 3 节该行措辞 |
| 7 | **单条 `PVEAuditor` 授在 `/`（propagate）就够全只读监控**，不需要额外拼 `Sys.Audit`/`Datastore.Audit`；反过来，只授在 `/nodes/x` 子树会缺 `/cluster/status` 和 `disks/smart` | 报告「PVEAuditor 够用」方向对，**但漏了「必须授在根路径」** |
| 8 | API Token 默认 `privsep=1`（权限取交集），**必须把 ACL 额外授给 token 本身**；`expire` 不填 = **永不过期** | 报告未涉及，**补充** |
| 9 | 官方 schema 共 **680 个 method 条目 / 437 个叶子端点**，不是报告写的 454 | **修正数字** |

---

## 一、认证与「3 秒延迟」的源码级答案

### 1.1 认证头格式【源码/官方文档证实】

官方文档 `pve-docs/pveum.adoc` 第 114-116 行：

> To use an API token, set the HTTP header 'Authorization' to the displayed value of the form `PVEAPIToken=USER@REALM!TOKENID=UUID`

服务端解析在 `pve-http-server/src/PVE/APIServer/Formatter.pm` 第 78-88 行：

```perl
sub extract_auth_value {
    my ($header, $key) = @_;
    return undef if !$header;
    my $value = ($header =~ /(?:^|\s)\Q$key\E(?:=| )([^;]*)/)[0];
    $value = uri_unescape($value) if $value;
    return $value;
}
```

默认头名在 `AnyEvent.pm` 第 2129 行：`$self->{apitoken_name} //= 'PVEAPIToken';`

要点：

- 头名就是 `PVEAPIToken`，后面 `=` 或空格分隔都行（正则 `(?:=| )`），值截到 `;` 为止；
- 值会被 `uri_unescape`，token secret 是 UUID 本身无需转义，但严谨做法仍应转义；
- 认证**先看 Cookie 票，再看 Authorization 里的票，最后才看 API Token**（`AnyEvent.pm` 第 1599-1622 行）。面板只用 Token，就不会走 CSRF 分支。

### 1.2 Token vs Ticket 的差别【源码证实】

`pve-manager/PVE/HTTPServer.pm` 第 123-130 行：

```perl
# Skip CSRF check for file upload (difficult to pass CSRF header with native html forms).
# Also skip the check with API tokens, as one of the design goals of API tokens was to
# provide stateless API access without requiring round-trips to get such CSRF tokens.
if ($method ne 'GET' && !($api_token || $isUpload)) {
    my $euid = $>;
    PVE::AccessControl::verify_csrf_prevention_token($username, $token) if $euid != 0;
}
```

| 维度 | Ticket（账号密码登录） | API Token |
|---|---|---|
| 传输 | `Cookie: PVEAuthCookie=...` | `Authorization: PVEAPIToken=...` |
| 有效期 | RSA 签名票，受 `ticket_lifetime` 限制 | 不删、不设 `expire` 就**永久有效** |
| CSRF | **非 GET 必须带 `CSRFPreventionToken`** | **完全豁免** |
| 无状态 | 否（需先生成票） | 是（每请求独立） |
| 受限端点 | 全部可用 | `allowtoken=0` 的 5 个端点不可用 |

`allowtoken=0` 的端点只有 5 个（`pve-access-control` 的 `API2/AccessControl.pm`、`API2/TFA.pm`），全在「创建票 / 改密码 / 改 TFA」这类账号管理侧；`PVE/RESTHandler.pm` 第 271 行把未显式声明的端点默认置 `allowtoken = 1`，**所以只读监控端点全部都能用 Token 访问**。

> 结论：**只读面板全程 GET + Token 方式下永远不需要 CSRF**，也不需要为 `allowtoken` 做任何特判。

### 1.3 3 秒延迟：源码原文【关键证据】

出处：`pve-http-server` 仓库 `src/PVE/APIServer/AnyEvent.pm`（`https://github.com/proxmox/pve-http-server/blob/master/src/PVE/APIServer/AnyEvent.pm`）

**（A）认证失败分支 —— 常量 3 秒**（master 第 1640-1671 行）：

```perl
        if (my $err = $@) {
            # HACK: see Note 1
            Net::SSLeay::ERR_clear_error();
            # always delay unauthorized calls by 3 seconds
            my $delay = 3;

            if (ref($err) eq "PVE::Exception") {
                $err->{code} ||= HTTP_INTERNAL_SERVER_ERROR,
                    my $resp = HTTP::Response->new($err->{code}, $err->{msg});
                $self->response($reqstate, $resp, undef, 0, $delay);
            } elsif (my $formatter = PVE::APIServer::Formatter::get_login_formatter($format)) {
                ...
                $self->response($reqstate, $resp, undef, $nocomp, $delay);
            } else {
                my $resp = HTTP::Response->new(HTTP_UNAUTHORIZED, $err);
                $self->response($reqstate, $resp, undef, 0, $delay);
            }
            return;
        }
```

**（B）API2 处理阶段返回 401 时 —— 把总耗时补足到 3 秒**（master 第 1038-1043 行）：

```perl
        my $delay = 0;
        if ($res->{status} == HTTP_UNAUTHORIZED) {
            # always delay unauthorized calls by 3 seconds
            $delay = 3 - tv_interval($reqstate->{starttime});
            $delay = 0 if $delay < 0;
        }
```

**（C）延时是怎么「等」出来的**（第 388-398 行，`response()` 内）：

```perl
    } elsif ($delay && $delay > 0) {
        my $w;
        $w = AnyEvent->timer(
            after => $delay,
            cb => sub {
                undef $w; # delete reference
                return if !$reqstate->{hdl}; # already disconnected
                $reqstate->{hdl}->push_write($res);
                $self->finish_response($reqstate);
            },
        );
```

即：**响应体早就准备好了，故意挂一个 AnyEvent 定时器，等够 3 秒才 `push_write`**。这是事件循环里的非阻塞等待，不占进程，但客户端体感就是 3 秒。

### 1.4 结论：这个 3 秒「是什么」「不是什么」

| 判断 | 结论 | 依据 |
|---|---|---|
| 是否只对 401 生效 | **是**。全文只有这两处 `$delay = 3`，条件都是 401 / auth_handler die | 上面 (A)(B) |
| 认证成功后是否还有延迟 | **没有**。200/403/500 走 `$delay = 0` 分支 | (B) 的 if 条件 |
| 是否是限速 / 令牌桶 | **不是**。没有任何计数器、没有 per-IP 状态 | `pve-http-server` / `pve-manager` 的 Perl 代码里 `grep -i 'ratelimit\|too many\|lockout\|fail2ban'` **零命中**（只有 changelog 与 VM 带宽配置出现该词） |
| 是否是 fail2ban 式封禁 | **不是**。PVE 本体不带 fail2ban 集成；changelog 只是提醒「日志格式变了，用 fail2ban 的自己适配」 | `pve-manager/debian/changelog` 的 fix #417 条目 |
| 是否渐进退避 | **不是**。固定 3 秒，第 1 次与第 100 次完全一样 | (A) 常量 `3` |
| 是否有尝试次数上限 | **没有**。可无限次失败，只是每次都慢 3 秒 | 无任何计数逻辑 |
| 该行为存在多久 | 2014 年就有。`pve-manager 3.2-5` changelog：`* always delay unauthorized calls` | `debian/changelog` 第 8803 行 |
| PVE 7 / 8 / 9 是否一致 | **一致**。stable-6 / stable-7 / master 三份 `AnyEvent.pm` 都有同两句注释与 `$delay = 3`（行号 943/955/1040 与 1434/1499/1643） | 三份源码切片比对 |

> **给面板的直接推论**：报告第 7 节风险 1「单次 API 往返 ~3s」这个前提，**只在未认证时成立**。用有效 Token 后不存在该延迟，所以「10 个端点 = 30 秒」的推导不成立；并发抓取仍然值得做（减少 RTT 累积、改善首屏），但**理由不是躲延时**。
>
> 反过来，**配错 Token 会让整个面板「每个请求都慢 3 秒」**——这个现象很好用：面板实测首屏稳定卡在 3.0s 附近，几乎可以断定是认证没过（401），而不是网络或 PVE 性能问题。建议面板把「连续 401」做成显式告警。

---

## 二、温度：0 命中是真的，但「拿不到温度」是误读

### 2.1 官方 schema 的关键词命中（可复现）

对 `apidoc.js` 里的 `apiSchema` 反序列化后全文匹配（680 method / 437 叶子端点）：

| 关键词 | 命中数 | 说明 |
|---|---|---|
| `temperature` | **0** | 无任何字段或描述 |
| `sensor` | **0** | 同上 |
| `thermal` | **0** | 同上 |
| `fan` / `rpm` | **0** | 风扇转速完全没有 |
| `Temp` | 20 | **全部**落在 `notes-template` / `Template`（备份备注模板、建模板）里，与温度无关 |
| `health` | 15 | 磁盘健康、Ceph health 等 |
| `pressure` | 44 | PSI，见第三节 |

**所以报告「全 API 中 `sensor`/`temperature` 命中数为 0」成立。** 补两点：

1. `fan`/`rpm` 同样是 0——报告没提，但结论一样：**风扇转速拿不到**；
2. 这个「0 命中」只说明**没有名为温度/传感器的字段**，**不能**推出「PVE 拿不到温度」。

### 2.2 磁盘温度的确切取法【源码 + 官方测试固件双重证实】

端点：`GET /nodes/{node}/disks/smart?node={node}&disk=/dev/sdX`

返回结构（`pve-storage/src/PVE/API2/Disks.pm` 第 205-213 行）：

```perl
    returns => {
        type => 'object',
        properties => {
            health => { type => 'string' },
            type => { type => 'string', optional => 1 },
            attributes => { type => 'array', optional => 1 },
            text => { type => 'string', optional => 1 },
        },
    },
```

权限（同文件第 186-188 行）：

```perl
    permissions => {
        check => ['perm', '/', ['Sys.Audit']],
    },
```

> 注意：**这个端点检查的是 `/`（根）上的 `Sys.Audit`，不是 `/nodes/{node}`**。这是最容易踩的坑，见第四节。

**温度在哪个字段，取决于盘类型：**

| 盘类型 | `type` 值 | 温度位置 | 实测样例 |
|---|---|---|---|
| SATA / ATA HDD·SSD | `"ata"` | `attributes[]` 里 `id` 为 `"194"` 的项（`name` 通常 `Temperature_Celsius`）；部分盘用厂商属性（如 `id 190` `Airflow_Temperature_Cel`）。**当前温度 = `raw` 的第一个空白分隔 token** | `{"id":"194","name":"Temperature_Celsius","value":31,"worst":40,"threshold":0,"raw":"31 (0 17 0 0 0)"}` |
| NVMe | `"text"` | **只在 `text` 自由文本里**，没有任何结构化 `attributes` | `"Temperature: 32 Celsius"` |
| SAS | `"text"` | 同上 | `"Current Drive Temperature: 20 C"` / `"Drive Trip Temperature: 70 C"` |

证据是官方仓库自带的三组 SMART 测试固件：

- `pve-storage/src/test/disk_tests/hdd_smart/sda_smart_expected.json`：含 `{"name":"Airflow_Temperature_Cel","id":"190","raw":"31 (Min/Max 20/33)",...}` 与 `Temperature_Celsius` / `id "194"`；
- `nvme_smart/nvme0n1_smart_expected.json`：`{"text":"...Temperature: 32 Celsius...Percentage Used: 31%...","health":"PASSED","type":"text","wearout":69}`；
- `sas/sda_smart_expected.json`：`{"text":"...Current Drive Temperature: 20 C...","health":"OK","type":"text","wearout":100}`。

解析代码在 `pve-storage/src/PVE/Diskmanage.pm` 第 90-174 行（`get_smart_data`）：ATA 行按正则拆成 `id/name/flags/value/normalized/worst/threshold/fail/raw`；非 ATA 走 `$smartdata->{text}` 累加原始行。

**两个报告没提的点**：

- `wearout`（SSD 寿命剩余百分比）**不在 `returns` 声明里**，但代码会返回（`Diskmanage.pm` 第 596-605 行，测试固件里也有该键）。PVE **不校验 `returns` 之外的多余字段**，所以能拿到；
- `/nodes/{node}/disks/list` **完全没有温度**，只有 `health` 一个字符串（`"PASSED"` / `"OK"` / `"SMART Disabled"`）。报告的「SMART 摘要」措辞容易让人以为 list 里有温度，**实际没有**。

### 2.3 温度落地建议（修正报告第四节）

| 方案 | 评价 |
|---|---|
| **v1 只展示磁盘温度，按盘类型分别解析** | 推荐。ATA 取 `attributes[id==194].raw` 首 token；NVMe/SAS 从 `text` 正则 `(?:Temperature\|Drive Temperature)\s*:\s*(\d+)\s*C` 抠。**每块盘一次 `/disks/smart` 请求**（4 块盘 = 4 次），建议放进 5–10 分钟的低频轮询，不进首屏 |
| 想让 CPU/主板温度也进来 | 只能 SSH `sensors -j` 或装 exporter，**任何「纯 API」路径都拿不到**——报告这个判断正确 |

---

## 三、端点 → 字段 → 最小权限 → 版本差异（主表）

字段来源优先级：官方 schema 的 `returns` 声明 **+ 源码里实际赋值的字段**（PVE 不严格校验 `returns`，代码赋的值都会进响应，只看 schema 会漏字段——这是核对时最容易出错的地方）。

### 3.1 只读监控必需端点

| 端点 | 关键返回字段（实际） | 最小权限 | PVE 7 | PVE 8 | PVE 9 |
|---|---|---|---|---|---|
| `GET /version` | `version` `release` `repoid` `console` | 任意已认证用户（`user=>all`） | ✅ | ✅ | ✅ 多 `console` |
| `GET /cluster/status` | `type` `id` `name` `nodes` `version` `quorate` `online` | **`Sys.Audit` on `/`** | ✅ | ✅ | ✅ |
| `GET /cluster/resources` | `id` `type` `status` `name` `node` `storage` `pool` `cpu` `maxcpu` `mem` `maxmem` `uptime` `hastate` `disk` `maxdisk` `level` `vmid` `template` `tags` … | `user=>all`，但**逐条按权限过滤**（无权限的条目不返回） | 无 `netin/netout/diskread/diskwrite/tags/template` | +`netin netout diskread diskwrite lock tags template` | +`memhost shared sdn network network-type zone-type protocol host-arch` |
| `GET /nodes/{node}/status` | `uptime` `idle` `cpu` `wait` `loadavg` `memory{free,total,used,available}` `rootfs{free,total,used,avail}` `swap{free,total,used}` `ksm{shared}` `cpuinfo{cores,cpus,model,sockets}` `pveversion` `kversion` `current-kernel{sysname,release,version,machine}` `boot-info{mode,secureboot}` | `Sys.Audit` on `/nodes/{node}` | **无** `current-kernel`/`boot-info`（只有 `kversion` 字符串）；`memory` **无 `available`** | ✅ | ✅ |
| `GET /nodes/{node}/rrddata` | RRD 数据源名（见 3.2） | `Sys.Audit` on `/nodes/{node}` | `timeframe` = hour/day/week/month/year | 同 7 | **多 `decade`**；底层 RRD 目录 `pve2-node/` → `pve-node-9.0/` |
| `GET /nodes/{node}/qemu/{vmid}/status/current` | v9 共 **32** 个：`vmid` `status` `name` `pid` `qmpstatus` `uptime` `cpu` `cpus` `mem` `maxmem` `memhost` `diskread` `diskwrite` `netin` `netout` `maxdisk` `lock` `tags` `template` `serial` `running-machine` `running-qemu` `ha` `spice` `agent` `clipboard` + 6×`pressure*` | `VM.Audit` on `/vms/{vmid}` | `returns` 仅声明 13 个，缺 `cpu/mem/netin/netout/diskread/diskwrite/template/serial` 的声明（代码仍会返回这些键） | 无 `pressure*`、无 `memhost`，其余同 9 | **32 个（全量）** |
| `GET /nodes/{node}/lxc/{vmid}/status/current` | v9 共 **24** 个（含 `maxswap`、5×`pressure*`、`ha`） | `VM.Audit` on `/vms/{vmid}` | `returns` 仅声明 10 个（`vmid/status/maxmem/maxswap/maxdisk/name/uptime/cpus/lock/tags`） | 无 `pressure*`，共 19 | **24 个（全量）** |
| `GET /nodes/{node}/qemu\|{vmid}/rrddata` | VM RRD 数据源（见 3.2） | `VM.Audit` on `/vms/{vmid}` | ✅ | ✅ | ✅ |
| `GET /nodes/{node}/storage` | `storage` `type` `content` `enabled` `active` `shared` `total` `used` `avail` `used_fraction` | `user=>all` + **逐条按 `Datastore.Audit` 过滤** | ✅ | ✅ | ✅ 多 `select_existing format formats` |
| `GET /nodes/{node}/storage/{storage}/status` | `type` `content` `enabled` `active` `shared` `total` `used` `avail` | **`Datastore.Audit` 或 `Datastore.AllocateSpace` on `/storage/{storage}`** | ✅ | ✅ | ✅ |
| `GET /nodes/{node}/disks/list` | `devpath` `used` `gpt` `mounted` `size` `osdid` `osdid-list` `vendor` `model` `serial` `wwn` `health` `parent`（另实测返回未声明的 `type` `rpm` `wearout`） | `Sys.Audit` on `/` **或** `/nodes/{node}` | ✅ 权限还接受 **`Datastore.Audit`** 替代 | 无 `osdid-list`；权限**只认 `Sys.Audit`** | 同 8 |
| `GET /nodes/{node}/disks/smart` | `health` `type` `attributes[]` `text`（另有未声明的 `wearout`） | **`Sys.Audit` on `/`（写死根路径）** | ✅ 权限还接受 **`Datastore.Audit`** 替代 | **只认 `Sys.Audit` on `/`** | 同 8 |
| `GET /nodes/{node}/network` | **仅配置**：`iface` `type` `active` `address` `cidr` `gateway` `bridge-ports` `bridge-vlan-aware` `vlan-id` `bond-slaves` … | `user=>all`（bridge 再按 SDN 权限过滤） | ✅ | ✅ | ✅ |
| `GET /nodes/{node}/netstat` | 仅 `tap*/veth*`（即 VM/CT 虚拟网卡）的收发字节 | `Sys.Audit` on `/nodes/{node}` | ✅ | ✅ | ✅ |
| `GET /nodes/{node}/tasks` | `upid` `node` `pid` `pstart` `starttime` `type` `id` `user` `endtime` `status` | `user=>all`，**但默认只列自己的任务**；要看全部需 `Sys.Audit` on `/nodes/{node}` | ✅ | ✅ | ✅ |
| `GET /nodes/{node}/services` | `service` `name` `desc` `state` `active-state` `unit-state` `systemd` | **`Sys.Audit` on `/nodes/{node}`** | ✅ | ✅ | ✅ |
| `GET /nodes/{node}/subscription` | `status` `level` `key` `nextduedate` `regdate` `productname` `sockets` `signature` `serverid` `checktime` `message` `url` | **`user=>all`（不需要任何特殊权限）** | ✅ | ✅ | ✅ |

### 3.2 RRD 数据源名（`rrddata` 返回项里的键）

来源：`pve-cluster/src/pmxcfs/status.c` 第 1108-1200 行（RRD 文件定义）+ `pve-manager/PVE/Service/pvestatd.pm` 第 226-253、345-370 行（写入顺序）。

**节点（PVE 9 `pve-node-9.0`，19 个 DS）**
`loadavg` `maxcpu` `cpu` `iowait` `memtotal` `memused` `swaptotal` `swapused` `roottotal` `rootused` `netin` `netout` `memavailable` `arcsize` `pressurecpusome` `pressureiosome` `pressureiofull` `pressurememorysome` `pressurememoryfull`

**VM/CT（PVE 9 `pve-vm-9.0`，17 个 DS）**
`maxcpu` `cpu` `maxmem` `mem` `maxdisk` `disk` `netin` `netout` `diskread` `diskwrite` `memhost` `pressurecpusome` `pressurecpufull` `pressureiosome` `pressureiofull` `pressurememorysome` `pressurememoryfull`

**存储（`pve-storage-9.0`，2 个 DS）** `total` `used`

> **推翻报告**：报告第 3 节把节点 `rrddata` 写成「cpu / memused / netin / netout / **diskread / diskwrite** / iowait 等」。**节点级 RRD 里没有 `diskread`/`diskwrite`**，这两个只在 VM/CT 级。节点侧只能用 `iowait` 间接反映磁盘压力，或者去取各 VM 的 `diskread`/`diskwrite` 求和。
>
> 另外节点 RRD **没有 `memfree`**（PVE 9 的键是 `memavailable`）。`PVE/RRD.pm` 第 25-26 行那段「`memfree` → 兜底出 `memavailable`」只对**老的 `pve2-node/` 文件**生效；面板若按 `memfree` 写死会拿到空值。

**RRD 保留窗口**（`status.c` 第 1130-1138 行，step = 60s）：RRA = 1min×1440（1 天）/ 30min×1440（30 天）/ 6h×1440（360 天）/ 1 周×570（约 10 年）。所以 `hour/day/week/month/year` 全版本可用，`decade` 数据其实一直都有，**只是 PVE 9 才在 API 枚举里放开**。

### 3.3 PSI（`pressure*`）的准确边界

| 位置 | PVE 7 | PVE 8 | PVE 9 |
|---|---|---|---|
| VM `status/current` 的 6 个 `pressure*` | ❌ | ❌ | ✅ |
| CT `status/current` 的 5 个 `pressure*` | ❌ | ❌ | ✅ |
| VM/CT `rrddata` 的 `pressure*` DS | ❌ | ❌ | ✅ |
| 节点 `rrddata` 的 `pressure*` DS | ❌ | ❌ | ✅（节点只有 5 个，**没有 `pressurecpufull`**） |
| 节点 `status` 里的 `pressure*` | ❌ | ❌ | ❌ **任何版本都没有** |

依据：`QemuServer.pm` 的 `$vmstatus_return_properties` —— v7/v8 中 `grep pressure` 命中 **0**，v9 命中 19 处；`pve-container/src/PVE/LXC.pm` 同理（v9 的 props 里有 5 个压力字段）。

### 3.4 全 API 的规模（修正报告数字）

| 口径 | 数量 |
|---|---|
| 顶层节点 | 6（`cluster` `nodes` `storage` `access` `pools` `version`） |
| **method + path 条目**（同一 path 的 GET/POST/PUT/DELETE 分别计） | **680** |
| 其中 GET | 342 |
| **叶子端点（`leaf`）** | **437** |

报告的「454 个端点」与任何一种口径都对不上（可能是更早版本或不同统计方式）。**建议对外统一用「437 个叶子端点 / 680 个 method」，别再用 454。**

---

## 四、权限模型：只读最小权限集

### 4.1 `PVEAuditor` 到底含哪些权限【源码证实】

`pve-access-control/src/PVE/AccessControl.pm` 第 1101-1241 行：角色由 `$privgroups` 里每类的 `audit` 数组自动拼成，`PVEAuditor` = 所有类别 `audit` 项的并集：

| 类别 | `audit` 项 |
|---|---|
| VM | `VM.Audit`, `VM.GuestAgent.Audit` |
| Sys | `Sys.Audit` |
| Datastore | `Datastore.Audit` |
| SDN | `SDN.Audit` |
| Pool | `Pool.Audit` |
| Mapping | `Mapping.Audit` |
| User | 空 |

所以 **`PVEAuditor` 一个角色同时包含 `Sys.Audit` + `Datastore.Audit` + `VM.Audit` + `Pool.Audit`**，不需要再拼。报告「只读 `PVEAuditor` + Token 权限分离」方向完全正确，但要补两个关键限定。

### 4.2 必须授在 `/`，且开 propagate

| 端点 | 权限路径 | 只授在 `/nodes/x` 的后果 |
|---|---|---|
| `/cluster/status` | `/` 的 `Sys.Audit` | ❌ **403** |
| `/nodes/{node}/disks/smart` | `/` 的 `Sys.Audit`（写死根路径，不认 `/nodes/{node}`） | ❌ **403**（PVE 8/9） |
| `/nodes/{node}/disks/list` | `/` 或 `/nodes/{node}` 的 `Sys.Audit` | ✅ 可用 |
| 其余节点级端点 | `/nodes/{node}` | ✅ 可用 |

> 这是最容易踩的坑：**看着「够用」的子树授权，会让磁盘页和集群状态页直接 403。**

推荐授权（最小集）：

```bash
# 只读账号
pveum user add monitor@pve --comment "fnOS PVE panel (read-only)"
pveum acl modify / --users monitor@pve --roles PVEAuditor --propagate 1

# Token（默认 privsep=1，必须把 ACL 额外授给 token 本身）
pveum user token add monitor@pve dash --privsep 1 --expire <unix 秒> --comment "fnOS panel token"
pveum acl modify / --tokens 'monitor@pve!dash' --roles PVEAuditor --propagate 1
```

**明确不需要**的权限：`Sys.Modify`、`Sys.PowerMgmt`、`Sys.Console`、`Sys.Syslog`、`VM.PowerMgmt`、`VM.Config.*`、`VM.Console`、`Datastore.Allocate`、`Datastore.AllocateSpace`、`Datastore.AllocateTemplate`、`Permissions.Modify`。

### 4.3 Token 的 privilege separation 与过期【源码证实】

`pve-access-control/src/PVE/AccessControl.pm` 第 476-505 行（`verify_token`）：

```perl
    my $ctime = time();
    die "token '$token' access expired\n"
        if $token_info->{expire} && ($token_info->{expire} < $ctime);

    die "invalid token value!\n" if !PVE::Cluster::verify_token($tokenid, $value);
```

| 问题 | 答案 |
|---|---|
| 默认 `privsep` 是几 | **1（权限分离）**。`API2/User.pm` 第 813 行：`$token->{privsep} = defined($param->{privsep}) ? $param->{privsep} : 1;`；官方文档 `pveum.adoc` 第 100-102 行也写「*This is the default*」 |
| `privsep=1` 时有效权限 | **用户权限 ∩ token 自己的 ACL**（`AccessControl.pm` 第 1886 行；`RPCEnvironment.pm` 第 201-202 行 `return $user_perms if !$token_info->{privsep};`）→ **必须单独给 token 授 ACL**，只给用户授不够 |
| `privsep=1` 对只读的价值 | 即使 token 泄露，攻击者也拿不到该用户的其他权限，是纵深防御的关键一环。**只读面板必须保持 `privsep=1`** |
| Token 会不会过期 | **默认永不过期**。`expire` 是可选字段，不填（0/undef）永不失效；只有显式设了 `expire`（Unix 秒）才在到期后拒绝 |
| 建议 | 显式设 `expire`（如 1~2 年）并在设置页提示；Token 值**只在创建时显示一次**，无法再取（`pveum.adoc` 第 111-112 行） |
| Token 能调全部端点吗 | 不能：`allowtoken=0` 的 5 个端点（创建票、改密码、改 TFA）被 `HTTPServer.pm` 第 170-171 行拦掉，返回 **403**。**只读监控端点全部允许** |

### 4.4 401 与 403 必须分开处理

- **401 Unauthorized**：token 不存在 / 值错 / 已过期 / 用户被禁用 → `HTTPServer.pm` 第 88-91 行 `die "Authentication failed!"` → **被加 3 秒延时**；
- **403 Forbidden**：认证通过但权限不足 → `PVE::Exception` 的 `raise_perm_exc` 固定 `code => HTTP_FORBIDDEN`（`pve-common/src/PVE/Exception.pm` 第 53-68 行），**不加延时**。

> 面板文案必须区分：401 =「Token 无效或过期」（配置问题）；403 =「权限不足，请确认 `PVEAuditor` 授在 `/` 且 propagate」（权限问题）。混为一谈会让用户完全无从下手。

---

## 五、版本兼容矩阵与降级策略

### 5.1 汇总表

| 能力 | PVE 7 | PVE 8 | PVE 9 | 面板降级做法 |
|---|---|---|---|---|
| `node.status` `current-kernel` / `boot-info` | ❌（只有 `kversion` 字符串） | ✅ | ✅ | 优先读 `current-kernel.release`，缺则回退 `kversion` |
| `node.status` `memory.available` | ❌ | ✅ | ✅ | 优先 `available`，缺失用 `free` 兜底并标注「近似」 |
| VM/CT `pressure*` | ❌ | ❌ | ✅ | 键存在才显示 PSI 卡片，否则整块隐藏（**不要显示 0**） |
| VM/CT `memhost` | ❌ | ❌ | ✅ | 同上 |
| 节点 RRD `pressure*` DS | ❌ | ❌ | ✅ | 按 DS 存在性动态生成曲线图例 |
| `rrddata` `timeframe=decade` | ❌ | ❌ | ✅ | 先探测，不支持则不显示「十年」档 |
| `cluster/resources` 的 `netin/netout` | ❌ | ✅ | ✅ | 缺失时改用 VM `status/current` 聚合 |
| `cluster/resources` 的 `memhost/shared/network*` | ❌ | ❌ | ✅ | 按需 |
| `disks/list` 权限接受 `Datastore.Audit` | ✅ | ❌ | ❌ | 统一按 `Sys.Audit` on `/` 授权，跨版本都通 |
| `disks/list` `osdid-list` | ❌ | ✅ | ✅ | 缺则用 `osdid` |
| CT `status/current` 声明字段数 | 10 | 19 | 24 | 一律「有则用、无则隐」 |
| RRD 目录名 | `pve2-node/` | `pve2-node/` | `pve-node-9.0/` | API 层已封装，面板**不要碰文件路径** |

### 5.2 降级实现原则

1. **版本探测放最前面**：启动时打一次 `GET /version` 拿 `release`（`"9.2"` 形式），主版本号只作为「默认能力开关」；
2. **字段级判断优于版本判断**：不要 `if (major >= 9)`，而是「`data.pressurecpusome !== undefined` 才渲染 PSI」。这样自编译版本、未来 9.x 新字段都不会误判；
3. **`returns` schema 只当文档**：PVE 不校验多余字段（`memhost`/`wearout` 就是活例子），**不要**用 schema 生成白名单裁剪响应，否则会丢真实数据；
4. **401/403 分开处理**，文案不同（见 4.4）。

---

## 六、对参考报告的逐条修订清单

| 报告位置 | 原文 | 核对结论 |
|---|---|---|
| 第 2.4 节 / 第 7 节风险 1 | 「未认证请求固定 3 秒…符合防爆破固定延时或代理层节流特征」；「10 个端点 = 30 秒」 | **方向对、机制说错、推论错**。是 `AnyEvent.pm` 里硬编码的 3 秒定时器（`$delay = 3`），**只挂 401 分支**，无任何限速/失败计数。**认证成功后无延时**，故「必须并发否则 30 秒」不成立 |
| 第 3 节 · 节点历史曲线 | 「cpu / memused / netin / netout / **diskread / diskwrite** / iowait 等」 | **错**。节点 RRD 无 `diskread`/`diskwrite`（那是 VM 级） |
| 第 3 节 · VM 状态 | 「32 字段，含 … `pressurecpufull` 等 PSI 压力指标」 | **数字对（仅 PVE 9），但没限定版本**。PSI 是 9 才加的，7/8 一个都没有 |
| 第 3 节 · CT 状态 | 「24 字段，含 uptime tags maxswap」 | **数字对（仅 PVE 9）**。PVE 7 只有 10 个声明字段、PVE 8 有 19 个 |
| 第 3 节 · 网络接口 | 「网卡/桥/vlan **配置与统计**」 | **「统计」错**。`/network` 只有配置、无流量计数；全 API 也没有「单网卡吞吐」端点 |
| 第 3 节 · 物理磁盘 | 「型号/大小/健康/SMART 摘要」 | `health` 只是个状态字符串，**没有温度**；温度只在 `/disks/smart` |
| 第 3 节 · 磁盘 SMART | 「`attributes` `health` `text` `type`（磁盘温度在此）」 | **对，但太粗**。准确见 2.2：ATA 走 `attributes[id=="194"].raw` 首 token；NVMe/SAS 只能正则 `text`；另有未声明的 `wearout` |
| 第 3 节 · 温度行 | 「全 API 中 `sensor` / `temperature` 命中数为 0」 | **确认成立**（另 `thermal`/`fan`/`rpm` 也是 0；`Temp` 的 20 次命中全在 `notes-template`/`Template`，别误判）。但**不能推出「拿不到温度」** |
| 第 3 节开头 | 「从 PVE 官方 API 定义文件（**454 个端点**）中核对确认」 | **数字对不上**。官方 schema = 680 method / 437 叶子端点 |
| 第 7 节风险 4 | 「只读 `PVEAuditor` + Token 权限分离」 | **对但不完整**：必须授在 `/` 且 propagate（否则 `/cluster/status` 与 `disks/smart` 403）；`privsep=1` 时 ACL 要**额外授给 token 本身**；`expire` 不设 = 永不过期 |
| 第 5 节 v1 · 磁盘页 | 「物理盘型号、容量、SMART 健康、磁盘温度」 | 可实现，但**温度需按盘类型分别解析**，且每盘一次请求，应放低频轮询 |
| 第 5 节 v1 · 曲线页 | 「hour / day / week / month / year 五档（PVE RRD 固定窗口）」 | 对；**PVE 9 还有 `decade`**（RRD 里确实有约 10 年的 RRA），可作可选增强 |
| 第 1 节 · 结论先行 | 「唯一的硬边界：PVE 官方 REST API 不提供 CPU / 主板温度与风扇转速」 | **结论正确**（还应加上：也确实没有风扇转速字段 `fan`/`rpm`=0） |

---

## 七、给面板实现的硬约束（可直接抄进设计文档）

1. **认证**：`Authorization: PVEAPIToken=<user>@<realm>!<tokenid>=<uuid>`，值做 URI 转义；全程 GET，不需要 CSRF。
2. **超时**：单请求设 10s 超时。**若稳定在 3.0s 附近返回，先怀疑 401**（Token 问题），不是网络问题。
3. **错误分流**：401 → 「Token 无效/过期/用户被禁用」；403 → 「权限不足，请确认 `PVEAuditor` 授在 `/` 且 propagate」。
4. **必做端点（首屏）**：`/version`、`/cluster/resources`、`/nodes/{node}/status`、逐节点 `/nodes/{node}/storage`。
5. **按需端点**：`rrddata`（点开曲线才拉）、`qemu|lxc/{vmid}/status/current`（点开 VM 才拉）、`disks/smart`（**5–10 分钟低频**，每盘一次）。
6. **磁盘温度解析**（伪代码）：

```python
def extract_disk_temp(smart):
    if smart.get("type") == "ata":
        for a in smart.get("attributes") or []:
            if a.get("id") in ("194", "190") or "Temperature" in (a.get("name") or ""):
                m = re.match(r"\s*(\d+)", a.get("raw") or "")
                if m: return int(m.group(1))
    m = re.search(r"(?:Temperature|Drive Temperature)\s*:\s*(\d+)\s*C", smart.get("text") or "")
    return int(m.group(1)) if m else None
```

7. **不要**用 schema 的 `returns` 做字段白名单；**不要**依赖 `memfree`（PVE 9 节点 RRD 只有 `memavailable`）；**不要**假设节点 RRD 有 `diskread`/`diskwrite`。
8. **授权模板**（写进 README 给用户抄）：`pveum acl modify / --users monitor@pve --roles PVEAuditor --propagate 1` + `pveum acl modify / --tokens 'monitor@pve!dash' --roles PVEAuditor --propagate 1`（`privsep=1`），并建议设 `--expire`。

---

## 八、待补 / 未覆盖

1. **实测校准**：本文全部是「官方 schema + 源码」核对，**没有**在真实 PVE（192.168.1.3）上发认证请求。建议用真实 Token 对各表「关键字段」逐条抓一次响应对账；尤其 VM/CT 字段计数（7/8/9 版的 `returns` 声明数与实际返回数不一致）优先看代码赋值，不要只看 schema。
2. **未验证项**：RRD `cf=MAX` 的实际行为；各存储插件 `status` 返回差异（NFS/CIFS/ZFS/iSCSI 的 `avail` 是否恒有值）；Ceph 相关端点未覆盖。
3. **社区资料**：`https://pve.proxmox.com/wiki/Proxmox_VE_API` 本次未逐字引用，所有结论以 schema/源码为准，**未使用任何社区二手结论**。
4. **v2 启停控制**不属本文范围（需 `VM.PowerMgmt` / `Sys.PowerMgmt`；注意 POST 在 Token 方式下仍免 CSRF），权限面需重新评估。

---

## 附：本文引用的权威源

| 内容 | 来源 |
|---|---|
| 官方 API schema（680 method / 437 leaf） | `https://pve.proxmox.com/pve-docs/api-viewer/apidoc.js`（由 `index.html` 加载） |
| 3 秒延时两处源码 | `https://github.com/proxmox/pve-http-server/blob/master/src/PVE/APIServer/AnyEvent.pm`（L388-398、L1038-1043、L1640-1671、L2129） |
| 认证 / CSRF / allowtoken | `https://github.com/proxmox/pve-manager/blob/master/PVE/HTTPServer.pm`（L54-141、L170-171） |
| 节点状态 / 服务 / 任务端点 | `https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Nodes.pm`、`API2/Services.pm`、`API2/Tasks.pm` |
| `/cluster/resources` 权限过滤 | `https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Cluster.pm`（L262-660）+ `PVE/API2Tools.pm`（L38-145） |
| 磁盘 list / smart 端点 | `https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Disks.pm`（L81-226） |
| SMART 解析与温度 | `https://github.com/proxmox/pve-storage/blob/master/src/PVE/Diskmanage.pm`（L90-174、L560-631）+ `src/test/disk_tests/{hdd_smart,nvme_smart,sas}/` |
| 角色与权限位 | `https://github.com/proxmox/pve-access-control/blob/master/src/PVE/AccessControl.pm`（L1101-1248、L333-505） |
| Token 校验 / privsep 求交 | 同上 + `src/PVE/RPCEnvironment.pm`（L201-202、L624-641） |
| 403 异常构造 | `https://github.com/proxmox/pve-common/blob/master/src/PVE/Exception.pm`（L53-69） |
| allows-token 端点清单 | `pve-access-control/src/PVE/API2/AccessControl.pm`、`API2/TFA.pm`；默认值在 `pve-common/src/PVE/RESTHandler.pm`（L271） |
| RRD 数据源与 RRA | `https://github.com/proxmox/pve-cluster/blob/master/src/pmxcfs/status.c`（L1101-1200、L1291-1387）；`src/PVE/RRD.pm` |
| RRD 写入顺序 | `https://github.com/proxmox/pve-manager/blob/master/PVE/Service/pvestatd.pm`（L226-253、L345-370） |
| VM/CT 状态字段定义 | `qemu-server/src/PVE/QemuServer.pm` 的 `$vmstatus_return_properties`；`pve-container/src/PVE/LXC.pm` 同名变量 |
| Token 使用与 privsep 官方说明 | `https://pve.proxmox.com/pve-docs/chapter-pveum.html` / `pve-docs/pveum.adoc`（L84-116） |
