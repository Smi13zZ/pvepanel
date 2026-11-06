# 测试环境：fnos-test 上的自建 PVE

后续开发都在这里验，不再连生产 PVE。

## 一句话说明

`fnos-test` 上跑着**两套真实的 Proxmox VE**：

| 虚拟机 | 版本 | 地址 | 里面有什么 | 覆盖什么 |
|---|---|---|---|---|
| `pve-test01` | PVE 8.4.21（Debian 12） | 192.168.122.50 | 1 台 VM | 8.x 字段集：**没有** PSI / memhost / decade |
| `pve9` | PVE 9.2.25（Debian 13） | 192.168.122.60 | 2 VM + 2 CT | 9.x 字段集：PSI / memhost / decade **都有** |

面板把两台都当成普通 PVE 服务器连（真实 API、真实认证、真实启停），
所以所有改动都能在这里真跑，不需要 Mock，也不用碰生产机。
两套并存的价值在于：字段降级逻辑可以同时被两边验证，不会只在一侧通过。

```text
Windows ──ssh──> fnos-test (192.168.1.13)
                    ├── 面板 fn-pvepanel  (127.0.0.1:8899)
                    └── libvirt NAT 192.168.122.0/24
                          ├── pve-test01 (Debian 12 + PVE 8.4.21)  192.168.122.50:8006
                          │       └── VM 200 demo-vm
                          └── pve9       (Debian 13 + PVE 9.2.25)  192.168.122.60:8006
                                  ├── VM 300 pve9-vm-a (running)
                                  ├── VM 301 pve9-vm-b (running)
                                  ├── CT 400 pve9-ct-a (stopped)
                                  └── CT 401 pve9-ct-b (running)
```

## 连它

PVE 在 libvirt 的 NAT 网络里，**只有 fnos-test 能直接访问**。从 Windows 走两跳：

```powershell
# 一层：进 NAS
ssh fnos-test

# 二层：NAS 进 PVE 虚拟机
ssh -i ~/.ssh/id_ed25519_pvetest root@192.168.122.50
```

一步到位的写法：

```powershell
ssh fnos-test "ssh -i ~/.ssh/id_ed25519_pvetest -o StrictHostKeyChecking=no root@192.168.122.50 qm list"
```

或者用 `tools/pve-test.py`：

```powershell
& $PY tools\pve-test.py "pveversion -v"
& $PY tools\pve-test.py "qm list"
& $PY tools\pve-test.py --shell            # 交互式
```

## 关键凭据

| 用途 | 值 |
|---|---|
| PVE Web / API（8） | `https://192.168.122.50:8006` |
| PVE Web / API（9） | `https://192.168.122.60:8006` |
| API Token（面板用） | `monitor@pve!panel`，已配在面板里，不用手输 |
| 虚拟机 root | 只用 SSH 密钥 `~/.ssh/id_ed25519_pvetest`（在 fnos-test 上），两台共用，无密码 |
| PVE Web 登录 | `root` + 虚拟机 root 的密码（开发用，未写入任何文件） |

> Token 明文只在创建时出现一次，已写进面板的 `servers.json`（root-only 600）。
> 需要重建时：`pveum user token add monitor@pve panel --privsep 1` 再重新登记。

## 资源占用

| 项 | 值 |
|---|---|
| 虚拟机规格 | 两台各 4 vCPU / 2 GB / 30 GB qcow2（稀疏） |
| 硬件加速 | 是（`domain type=kvm` + `host-passthrough`） |
| 嵌套虚拟化 | 可用（虚拟机里能看到 `/dev/kvm`，能再开 VM） |
| NAS 内存占用 | 两台合计常驻约 4 GB（总共 7.7 GB） |

## 自启动

已配好，NAS 重启后自动拉起：

- `libvirtd` 开机启用
- `default` 网络（`virbr0` / 192.168.122.1）标记 autostart
- `pve-test01` 标记 autostart
- PVE 里的 `vmbr0` 由一个一次性 systemd 单元 `vmbr0.service` 创建
  （这个镜像的 `networking.service` 是关的，ifupdown 不管桥，所以单独补了）

验证：

```powershell
ssh fnos-test "sudo virsh list --all --autostart; sudo virsh net-list --all; systemctl is-enabled libvirtd"
```

预期输出：`pve-test01` 与 `pve9` 都 running、`default active yes`、`enabled`。
另外 PVE 里的 `vmbr0.service` 也要是 enabled（在虚拟机里查，不是在 NAS 上）：

```powershell
& $PY tools\pve-test.py "systemctl is-enabled vmbr0.service"
```

## 里面有什么

| 对象 | PVE 8（pvetest） | PVE 9（pve9） |
|---|---|---|
| 规格 | 4 核 / 2 GB / 30 GB | 4 核 / 2 GB / 30 GB |
| 存储 | `local` 39 GB | `local` 30 GB |
| Guest | VM 200 | VM 300/301、CT 400/401 |
| 用户 | `monitor@pve` + PVEAuditor | 同左 |
| Token | `monitor@pve!panel`（privsep=1） | 同左，值为另一个 UUID |

> PVE 8 和 9 的字段差异正好能在这里验：8 没有 `pressure*`（PSI）、没有 `memhost`、
> `rrddata` 也没有 `decade`。面板的降级逻辑就是这样验出来的。

## 重建

这台虚拟机彻底搞坏了，从零重来大约 15 分钟：

```sh
# 1. 拆掉旧的
ssh fnos-test "sudo virsh destroy pve-test01; sudo virsh undefine pve-test01 --remove-all-storage"

# 2. 重铺云镜像
#    qcow2 overlay on base-debian12.qcow2 -> 离线挂载写 authorized_keys /
#    interfaces / 生成 ssh host key -> virt-install --import

# 3. 装 PVE（用清华镜像，官方源在这里只有 ~20 KB/s）
ssh fnos-test "ssh -i ~/.ssh/id_ed25519_pvetest root@192.168.122.50 bash /tmp/pve-i5.sh"
```

## 已知的坑（都踩过）

1. **cloud image 没有 SSH host key** —— cloud-init 被禁用后 sshd 起不来，
   日志是 `sshd: no hostkeys available -- exiting`，要离线 `ssh-keygen` 补。
2. **没有 `vmbr0`** —— apt 装的 PVE 不建桥，嵌套 VM 一开机就报
   `bridge vmbr0 does not exist`。
3. **`networking.service` 是关的** —— 这个镜像用 systemd-networkd 管网络，
   ifupdown 不生效，所以桥得手工建（已做成 `vmbr0.service`）。
4. **`privsep=1` 的 Token 要两侧都授权** —— 只授 Token 侧会 403，这是最容易卡住的地方。
5. **启停任务有延迟** —— `qmstop` 返回 UPID 后要几秒状态才变，面板刷新间隔是 10 秒，
   刚点完立刻截图可能还是旧状态。任务列表里能看到 `qmstop OK`。
   （面板 26.11.2 起操作后会在 2.5s 与 8s 各刷一次，并且 `force` 真的绕过缓存了。）
6. **PVE 9 的源签名密钥不在任何镜像站上** —— `proxmox-release-trixie.gpg` 在清华/中科大/官方
   都是 404。要从公网 keyserver 按 ID 取：`gpg --keyserver keyserver.ubuntu.com --recv-keys A7BCD1420BFE778E`，
   再 `gpg --export` 出来放进 `/etc/apt/trusted.gpg.d/`。
7. **PVE 9 装在 EFI 机器上会卡在 grub-pc 配置** —— 报 `installed grub-pc package post-installation
   script subprocess returned error`。这是 dpkg 在给没有 BIOS grub 的 EFI 机器装 BIOS grub。设
   `echo "grub-pc hold" | dpkg --set-selections` 再 `dpkg --configure -a` 即可，PVE 本体已经装好了。
8. **Debian 13 cloud image 必须用 UEFI 启动** —— 用 `--boot uefi` 或 OVMF，否则内核一个字节都不输出，
   串口控制台只看到 grub 横幅无限循环。
9. **容器模板的官方 CDN 在这里不可达** —— `download.proxmox.com/images/system/` 连不上，`pveam download`
   会一直挂着。直接从清华镜像 `mirrors.tuna.tsinghua.edu.cn/proxmox/images/system/` 下到
   `/var/lib/vz/template/cache/` 即可，`pveam list local` 会认。

## 和面板的关系

面板里现在配了两台：

| 名称 | 地址 | 用途 |
|---|---|---|
| Chin | 192.168.1.13:18006 | 行为仿真的 Mock（PVE 9.2.21 字段集），可切故障模式 |
| PVE 测试机 | 192.168.122.50:8006 | 真实 PVE 8.4.21 |

顶栏切换。日常开发对着**真实 PVE**验，Mock 只在需要复现故障态时切过去。

Mock 的故障模式切换：

```bash
curl "http://192.168.1.13:18007/__mock/mode?value=401"   # 401 + 3 秒惩罚
curl "http://192.168.1.13:18007/__mock/mode?value=403"   # 403，不延时
curl "http://192.168.1.13:18007/__mock/mode?value=offline"
curl "http://192.168.1.13:18007/__mock/mode?value=ok"    # 切回
```
