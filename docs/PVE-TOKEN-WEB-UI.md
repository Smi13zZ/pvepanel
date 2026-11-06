# root 用户在网页上创建「能控制虚拟机」的 API Token

全程在 PVE 网页界面 (`https://<PVE 地址>:8006`) 用 `root@pam` 操作，不需要命令行。

控制虚拟机启停/暂停需要 **两个能力**：读状态（`PVEAuditor`）和改电源（`PVEVMAdmin`）。
因为 PVE 的 Token 默认开启「特权分离」，有效权限是 **用户权限 ∩ 令牌权限**，
所以下面每个角色都要**给用户授一遍、再给令牌授一遍**，一共四条。

---

## 第 1 步：打开「数据中心 → 权限」

用 `root@pam` 登录后，左侧点 **数据中心**，再点左侧导航树里的 **权限**。
展开后能看到 **用户 / API 令牌 / 群组 / 池 / 角色 / 领域** 这些子项。

![数据中心 → 权限](01-datacenter-permissions.png)

---

## 第 2 步：建一个专用用户

点左侧 **用户** → 表格上方 **添加**：

| 字段 | 填什么 |
|---|---|
| 用户名称 | `pvepanel`（**不带** `@pve`，领域在下一栏选） |
| 领域 | `Proxmox VE authentication server`（就是 `@pve`，与 Linux 系统账号隔离） |
| 密码 / 确认密码 | 至少 8 位。面板用令牌访问，不依赖这个密码，但用户必须先能建出来 |
| 备注 | 例如 `PVEpanel 监控面板` |

![添加用户](02-add-user.png)

点 **添加** 保存。

---

## 第 3 步：给用户授只读权限（`/` + `PVEAuditor`）

回到左侧 **权限**，点 **添加** → 选 **用户权限**：

| 字段 | 填什么 |
|---|---|
| 路径 | `/` |
| 用户 | `pvepanel@pve` |
| 角色 | `PVEAuditor` |
| 继承（Propagate） | **保持勾选** |

![用户权限 PVEAuditor](03-user-permission.png)

> **为什么必须是 `/`**：只授在 `/nodes/<节点>` 的话，`/cluster/status` 和
> `/nodes/<节点>/disks/smart` 会 403——这两个端点查的是根路径权限。

---

## 第 4 步：再给用户授电源权限（`/vms` + `PVEVMAdmin`）

同样在 **权限** → **添加** → **用户权限**：

| 字段 | 填什么 |
|---|---|
| 路径 | `/vms` |
| 用户 | `pvepanel@pve` |
| 角色 | `PVEVMAdmin` |
| 继承 | **保持勾选** |

`PVEVMAdmin` 包含 `VM.PowerMgmt`，启停/暂停靠的就是它。

---

## 第 5 步：创建 API 令牌

点左侧 **API 令牌** → 表格上方 **添加**：

| 字段 | 填什么 |
|---|---|
| 用户 | `pvepanel@pve` |
| 令牌 ID | `panel` |
| 特权分离（Privilege Separation） | **保持勾选** |
| 备注 | 例如 `PVEpanel 控制用` |

![添加 API 令牌](04-add-token.png)

---

## 第 6 步：立刻保存密钥

点 **添加** 后立刻弹出一个 **令牌秘钥** 窗口，里面只有两行：

- **令牌 ID**：`pvepanel@pve!panel`
- **密钥**：一串 uuid

窗口上有 **复制密钥值** 按钮。**这个密钥只显示这一次**，关掉就再也看不到，丢了只能
「重新生成密钥」（旧密钥立即失效）。

![令牌密钥只显示一次](05-token-secret.png)

把两段用 `=` 连起来，就是面板里要填的完整令牌：

```
pvepanel@pve!panel=d67a399c-ff93-4bba-b04a-50c52d813b74
```

> 上面这枚是演示令牌，写完文档已经作废，别拿去用。

---

## 第 7 步：给令牌授只读权限（`/` + `PVEAuditor`）

回到 **权限** → **添加** → 选 **API 令牌权限**：

| 字段 | 填什么 |
|---|---|
| 路径 | `/` |
| API 令牌 | `pvepanel@pve!panel` |
| 角色 | `PVEAuditor` |
| 继承 | **保持勾选** |

![令牌权限 PVEAuditor](07-token-readonly.png)

---

## 第 8 步：给令牌授电源权限（`/vms` + `PVEVMAdmin`）

**权限** → **添加** → **API 令牌权限**：

| 字段 | 填什么 |
|---|---|
| 路径 | `/vms` |
| API 令牌 | `pvepanel@pve!panel` |
| 角色 | `PVEVMAdmin` |
| 继承 | **保持勾选** |

完成后权限列表里应该有这四条：

![四条 ACL 全貌](08-final-acls.png)

```
/     user  pvepanel@pve        PVEAuditor   propagate=1
/     token pvepanel@pve!panel  PVEAuditor   propagate=1
/vms  user  pvepanel@pve        PVEVMAdmin   propagate=1
/vms  token pvepanel@pve!panel  PVEVMAdmin   propagate=1
```

---

## 验证

拿到令牌后直接试：

```bash
TOKEN='pvepanel@pve!panel=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
PVE=https://192.168.1.3:8006/api2/json

# 只读要通
curl -sk -H "Authorization: PVEAPIToken=$TOKEN" $PVE/cluster/status | head -c 80

# 停止虚拟机：返回 UPID 就是权限到位
curl -sk -X POST -H "Authorization: PVEAPIToken=$TOKEN" $PVE/nodes/<节点>/qemu/<vmid>/status/stop

# 启动 / 暂停 / 恢复同理
curl -sk -X POST -H "Authorization: PVEAPIToken=$TOKEN" $PVE/nodes/<节点>/qemu/<vmid>/status/start
curl -sk -X POST -H "Authorization: PVEAPIToken=$TOKEN" $PVE/nodes/<节点>/qemu/<vmid>/status/suspend
curl -sk -X POST -H "Authorization: PVEAPIToken=$TOKEN" $PVE/nodes/<节点>/qemu/<vmid>/status/resume
```

本次实测（PVE 9.2.25，VM 300）：

| 操作 | 返回 | 结果 |
|---|---|---|
| 只读 `cluster/status` | 200 | 通 |
| `status/stop` | `UPID:...qmstop:300:pvepanel@pve!panel:` | 2 次轮询后 stopped |
| `status/start` | `UPID:...qmstart:300:pvepanel@pve!panel:` | 2 次轮询后 running |
| `status/suspend` | `UPID:...qmpause:300:pvepanel@pve!panel:` | 进入 paused |
| `status/resume` | `UPID:...qmresume:300:pvepanel@pve!panel:` | 回到 running |

**注意 `shutdown` 和 `stop` 的区别**：`shutdown` 是发 ACPI 信号请客户机自己关机，
客户机没装/没响应 ACPI 时它会一直等，直到超时——本次实测 VM 300 就属于这种，
请求返回 200 带 UPID，但虚拟机仍是 running。要保证关掉用 `stop`（相当于拔电源）。

---

## 常见问题

**点了 `status/stop` 却 403，报文里带 `VM.PowerMgmt`**
`PVEVMAdmin` 那条没授全。注意要给**用户和令牌各一条**，少一条都会被交集吃掉。

**只授了 `/vms` 的 `PVEVMAdmin`，能关机但面板一半数据是空的**
根路径的 `PVEAuditor` 省不得，`/cluster/status`、磁盘 SMART 都查根路径权限。

**特权分离取消勾选可以少授两条吗**
可以。取消后令牌直接继承用户权限。代价是这枚令牌从此拥有该用户的全部权限，
以后给别人加权限等于同时给它加权限——专用只读账号无所谓，多令牌分权时建议保持勾选。
