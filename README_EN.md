<div align="center">

<img src="src/fn-pvepanel/app/web/images/icon.png" width="104" alt="PVEpanel">

# PVEpanel

**A native Proxmox VE monitoring panel for fnOS** · Overview / Guests / Storage / Disks / Tasks / Notifications

Pure Python 3 stdlib backend · Vue 3 SPA front-end · zero third-party dependencies · desktop and mobile friendly

[Features](#features) · [Screenshots](#screenshots) · [Install](#install) · [Usage](#usage) · [FAQ](#faq) · [Changelog](CHANGELOG.md) · [中文说明](README.md)

</div>

---

## What it is

PVEpanel turns the state of a Proxmox VE cluster into a **native fnOS application** (`.fpk`) that opens
straight from the fnOS desktop. It is not a "wrapped webpage" — and **the browser never talks to PVE**:

- **The panel ships its own local service**; the browser only talks to that, and every request is relayed on your behalf.
- **Cluster addresses and API tokens live on the device only** — never rendered into a page, never sent to an external server.
- **Zero dependencies.** The backend uses only the Python 3 standard library; the Vue 3 build is bundled locally, so everything works offline.
- **One package for both x86 and ARM** fnOS boxes (nothing in the app is architecture-specific).

It is for the questions you end up asking every day: is memory about to run out, did a guest stop by
itself last night, is a disk's temperature creeping up, and is there a VM that no backup job covers at all?
Those should be one glance away, not five clicks deep in a Proxmox console.

> PVEpanel is a **monitoring panel**, not a Proxmox management console. The only write actions are
> start / shutdown / force-stop / reboot of a guest; everything else is read-only.

---

## Features

| Area | What you get |
|---|---|
| **Overview** | CPU, memory and load each get their own card so one bad number cannot hide behind another; cluster quorum and HA state, uptime, kernel and boot mode; **warning summary**; resource history (hour / day / week / month / year, with a real time axis); storage usage |
| **Guests** | VMs and containers in one list; filter by all / running / stopped; search by name or VMID; CPU and memory use, uptime, template marker; **start / shutdown / force-stop / reboot** with confirmation (force-stop warns about data loss) |
| **Storage** | One row per storage: usage ring, used / total, available, content types, node, shared flag, online state |
| **Disks** | Physical drives with model, size, health, **temperature** and SSD wear; plus the list of guests **not covered by any backup job** |
| **Tasks** | Backup / migration / power tasks with result, paginated 10 per page; subscription status; system service health |
| **Settings · Servers** | Manage several PVE endpoints as cards; add / edit / delete; **test an endpoint without switching to it**; set as current |
| **Settings · Notifications** | Nine push channels; twelve event types toggled individually; per-event cooldown so one alert cannot spam you |
| **Settings · Maintenance** | Export / import configuration, export to the on-device backup folder, automatic backups (interval + retention), restore or delete any backup |
| **Settings · Update** | Reads the latest GitHub release and compares it with the running build; **one-click update** (package verified before uninstalling, automatic rollback if it fails to start) |
| **Settings · Appearance** | Dark / light / follow-system, Chinese and English, refresh interval |
| **Mobile** | Drawer sidebar, card layouts, horizontally scrollable tables, no horizontal overflow — works inside the fnOS phone app's embedded browser |
| **Deep links** | `?view=` `?tab=` `?tf=` `?lang=` `?theme=` `?server=` to open a page, a chart range, or force a theme |

### What the warnings cover

Node offline · cluster lost quorum · storage above threshold · **guest without backup** · backup job failed ·
disk SMART error · **disk temperature above threshold** · high load · memory pressure · subscription expiring ·
permission denied · token invalid.

Every one of them can be pushed to your phone through the nine notification channels.

### Supported notification channels

Bark / Server Chan / PushPlus / WxPusher / Telegram / WeCom bot / DingTalk bot / Slack / custom webhook.

---

## Screenshots

Desktop (1440px):

| Overview · light | Overview · dark |
|---|---|
| ![Overview light](docs/images/dash-light.png) | ![Overview dark](docs/images/dash-dark.png) |

| Guests | Storage |
|---|---|
| ![Guests](docs/images/guests.png) | ![Storage](docs/images/storage.png) |

| Disks | Tasks |
|---|---|
| ![Disks](docs/images/disks.png) | ![Tasks](docs/images/tasks.png) |

| Servers | Update |
|---|---|
| ![Servers](docs/images/servers.png) | ![Update](docs/images/update.png) |

Mobile (390px):

| Overview | Guests | Settings |
|---|---|---|
| ![Mobile overview](docs/images/mobile-dash.png) | ![Mobile guests](docs/images/mobile-guests.png) | ![Mobile settings](docs/images/mobile-settings.png) |

---

## Install

### Requirements

- An fnOS device (x86 or ARM) — fnOS ships Python 3;
- A reachable Proxmox VE (7 / 8 / 9) and a read-only API token (see below).

### Option 1: install from App Center (recommended)

1. Download the latest `fn-pvepanel-<version>-fnos-all.fpk` from [Releases](../../releases);
2. fnOS → **App Center** → **Manual install** → pick the file;
3. The wizard already shows port 39205 pre-filled — accept it;
4. A **PVEpanel** icon appears on the fnOS desktop.

### Option 2: install from the command line

```bash
appcenter-cli install-fpk /path/to/fn-pvepanel-26.11.14-fnos-all.fpk
```

> `appcenter-cli` requires the environment file (`-e`) and **will not overwrite an installed app** —
> uninstall first. If you are already running an older version, use the in-panel one-click update instead.

### Where things live

| Content | Path |
|---|---|
| Program | `/usr/local/apps/@appcenter/fn-pvepanel` |
| Config, tokens, logs, backups | `/usr/local/apps/@appdata/fn-pvepanel` |

---

## Prepare a PVE token

PVE authenticates over the API with a token. For read-only use a single `PVEAuditor` role is
enough, but it has to be granted on `/` with **Propagate** enabled.

There are two ways to create it. **The web UI is quicker**, so start there.
A screenshot-by-screenshot version lives in [docs/PVE-TOKEN-WEB-UI.md](docs/PVE-TOKEN-WEB-UI.md)
(Chinese; the field names are the same in an English UI).

### Option 1: the web UI (recommended)

Open `https://<pve-host>:8006` and log in as `root@pam`. Select **Datacenter** in the left tree,
then click **Permissions** in the navigation tree on the left. It expands to **Users / API Tokens /
Groups / Pools / Roles / Realms**, and all four steps below happen inside that group.

#### 1. Create a dedicated user

Click **Users** on the left, then **Add** above the table:

| Field | Value |
|---|---|
| User name | `monitor` — **no** `@pve` here, the realm comes from the next field |
| Realm | `Proxmox VE authentication server` — that is the `@pve` realm, separate from Linux accounts |
| Password / Confirm password | Anything, at least 8 characters. The panel uses the token and never this password, but the user has to exist |
| Comment | e.g. `PVEpanel read-only` |

#### 2. Grant the user read-only access

Click **Permissions** itself on the left, click **Add**, and choose **User Permission**:

| Field | Value |
|---|---|
| Path | `/` |
| User | `monitor@pve` |
| Role | `PVEAuditor` |
| Propagate | **leave it checked** — it is checked by default, do not clear it |

#### 3. Create the API token

Click **API Tokens** on the left, then **Add** above the table:

| Field | Value |
|---|---|
| User | `monitor@pve` |
| Token ID | `panel` |
| Privilege Separation | **leave it checked** (see step 4) |
| Comment | e.g. `PVEpanel` |

After you confirm, a window titled **Token Secret** appears with two fields:

- **Token ID**: `monitor@pve!panel`
- **Secret**: a uuid

There is also a **Copy Secret Value** button. **This secret is shown exactly once** — close the
window and it is gone; if you lose it you have to regenerate it. Join the two fields with `=`:

```
monitor@pve!panel=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

That whole string is what goes into the panel's **API Token** field.

#### 4. Grant the token as well

With Privilege Separation on, a token's effective permissions are **user ACL ∩ token ACL**, so both
sides need the role. Go back to **Permissions** on the left, click **Add**, and choose
**API Token Permission**:

| Field | Value |
|---|---|
| Path | `/` |
| API Token | `monitor@pve!panel` |
| Role | `PVEAuditor` |
| Propagate | **leave it checked** |

> **Want one grant instead of two?** Clear Privilege Separation in step 3 and the token simply
> inherits the user's permissions — step 4 becomes unnecessary. The trade-off is that the token now
> carries **everything** that user has, forever, so adding permissions to the user later also widens
> the token. For a dedicated read-only account that is fine; when you run several tokens with
> different scopes, keep the separation and grant each one separately.

### Option 2: the command line

The four steps above are equivalent to four commands, which is handy in automation:

```bash
pveum user add monitor@pve --comment "PVEpanel read-only"
pveum acl modify / --users  monitor@pve        --roles PVEAuditor --propagate 1
pveum user token add monitor@pve panel --comment "PVEpanel"
pveum acl modify / --tokens 'monitor@pve!panel' --roles PVEAuditor --propagate 1
```

The uuid printed by `token add` is the secret — also shown only once.

> `pveum` lives in `/usr/sbin`. If `ssh root@pve "pveum ..."` says command not found, use the full
> path or `bash -lc 'pveum ...'`.

### Why the root path matters

Granted only on `/nodes/<node>`, the endpoints `/cluster/status` and `/nodes/<node>/disks/smart`
return 403 — they check permissions on the **root** path. The panel reports this as
"permission denied — check that PVEAuditor is granted on / with Propagate enabled" instead of a vague
"connection failed".

### Allowing guest power actions

**A read-only token cannot start or stop anything.** `PVEAuditor` is strictly read-only and does
not include `VM.PowerMgmt` — calling a power endpoint with it is rejected outright:

```
POST /nodes/pve9/qemu/300/status/shutdown
 -> 403 {"message":"Permission check failed (/vms/300, VM.PowerMgmt)"}
```

Grant `PVEVMAdmin` (which contains `VM.PowerMgmt`) on `/vms`, **to both the user and the token**
— with Privilege Separation on, the effective permissions are the intersection.

In the web UI that is **Datacenter → Permissions → Add**, twice (illustrated in
[docs/PVE-TOKEN-WEB-UI.md](docs/PVE-TOKEN-WEB-UI.md)):

| # | Add type | Path | Subject | Role | Propagate |
|---|---|---|---|---|---|
| 1 | User Permission | `/vms` | `monitor@pve` | `PVEVMAdmin` | checked |
| 2 | API Token Permission | `/vms` | `monitor@pve!panel` | `PVEVMAdmin` | checked |

The command-line equivalent:

```bash
pveum acl modify /vms --users  monitor@pve         --roles PVEVMAdmin --propagate 1
pveum acl modify /vms --tokens 'monitor@pve!panel' --roles PVEVMAdmin --propagate 1
```

The complete set — read-only plus power control — is four ACL entries:

```
/     user  monitor@pve        PVEAuditor   propagate=1
/     token monitor@pve!panel  PVEAuditor   propagate=1
/vms  user  monitor@pve        PVEVMAdmin   propagate=1
/vms  token monitor@pve!panel  PVEVMAdmin   propagate=1
```

The two root-path `PVEAuditor` grants are not optional: with only `/vms` you can power a guest on
and off but `/cluster/status` and `/nodes/<node>/disks/smart` return 403, and half the panel stays
empty.

> **Read-only is fine for most people.** Drop the last two lines and keep just the `PVEAuditor`
> pair. The power buttons stay visible and answer "permission denied" when clicked, which is easier
> to diagnose than hiding them.

```bash
# Check what the token can actually do
TOKEN='monitor@pve!panel=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
curl -sk -H "Authorization: PVEAPIToken=$TOKEN" \
     https://pve:8006/api2/json/cluster/status | head -c 120          # read-only must work
curl -sk -X POST -H "Authorization: PVEAPIToken=$TOKEN" \
     https://pve:8006/api2/json/nodes/<node>/qemu/<vmid>/status/start
```

An `UPID:...` reply means power control is in place; a 403 that mentions `VM.PowerMgmt` means
`PVEVMAdmin` is still missing.

---

## Usage

### 1. Add your PVE

First run shows "no server yet". Click **Add server**:

| Field | Notes |
|---|---|
| Name | Anything; shown in the top bar |
| Address | **As reachable from the fnOS device** — host or IP only, no `https://` and no port |
| Port | Default `8006` |
| API token | The whole `user@realm!tokenid=uuid` string |
| Verify TLS | PVE ships a self-signed certificate; usually off |

Multiple PVE endpoints can be managed together, but **the overview only ever shows the current one** —
numbers from two clusters are never mixed into one chart.

Once stored, the browser only ever sees the `__SET__` mask; leave the token field empty when editing
to keep the stored value.

### 2. Overview

Three cards for CPU, memory and load — **kept separate** so one out-of-range number cannot hide behind
another. Below them: cluster summary (quorum, HA, kernel, boot mode, uptime), the warning list,
resource history and storage usage.

The history chart has five ranges, with a real time axis:

| Range | Window | Tick spacing |
|---|---|---|
| Hour | last hour | 10 minutes |
| Day | last day | 2 hours |
| Week | last week | 1 day |
| Month | last month | 5 days |
| Year | last year | 2 months |

### 3. Guests

VMs and containers share one table. Filter all / running / stopped, search by name or VMID.

Row actions: **reboot / shutdown / force-stop** while running, **start** while stopped.
Force-stop is red and adds a second line warning that it is like pulling the power cord.
The table scrolls horizontally when needed — every column stays available.

### 4. Storage / Disks / Tasks

- **Storage**: one row each, usage ring plus available, content types, node, shared flag;
- **Disks**: device, model, size, type, health, **temperature**, SSD wear; the guests without any
  backup coverage are listed separately underneath;
- **Tasks**: three tabs — tasks / subscriptions / services. The task list shows 10 rows per page.

### 5. Notifications

Add channels under **Settings → Notifications**; twelve event types toggle individually.
The push rule is exactly the warning list you see on screen — there is no case where the panel shows
red but nothing is pushed, or the other way round.

Each event carries its own cooldown (storage 6h, load 30min, subscription 24h, …), so a recurring alert
does not repeat. Every channel can be tested on its own; secrets stay local and are never echoed back.

### 6. Maintenance and updates

**Maintenance**: the configuration can be exported and imported as a whole. The two kinds of backup
differ on purpose —

- **the downloaded copy carries no API tokens** (the file leaves the device);
- **the on-device automatic backups do keep them**, otherwise a restore would produce a panel that
  cannot connect and every token would have to be retyped.

Automatic backups take an interval and a retention period; retention only prunes automatic backups,
never the ones you exported by hand.

**Update**: checked hourly in the background, whether or not a browser is open. When a release exists,
one click downloads it, verifies the package, then uninstalls and installs — rolling back automatically
if the new build fails to start. The page waits for the new version to come up.

### 7. Mobile and deep links

Opening the panel inside the fnOS app switches to the mobile layout automatically; in a normal browser
the same layout kicks in below 640px.

| Parameter | Values | Effect |
|---|---|---|
| `view` | `dash` / `guests` / `storage` / `disks` / `tasks` / `settings` | Open a page directly |
| `tab` | `servers` / `notify` / `maint` / `update` / `ui` | Open a settings section |
| `tf` | `hour` / `day` / `week` / `month` / `year` | History chart range |
| `lang` | `zh` / `en` | Force the UI language |
| `theme` | `dark` / `light` / `auto` | Force a theme |
| `server` | server id | Switch to a specific PVE endpoint |

---

## FAQ

**Cannot connect / "token invalid"**
Use **Settings → Servers → Test connection**. It answers in four separate steps — reachability,
authentication realm, token, permissions — which is far more useful than one generic failure message.

**"Permission denied" even though the token looks right**
`PVEAuditor` must be granted on `/` with `--propagate 1`. Granted only on a node subtree,
`/cluster/status` returns 403.

**Why does the power button say permission denied?**
`PVEAuditor` does not include `VM.PowerMgmt`; grant `PVEVMAdmin` on `/vms` in addition.

**No CPU / motherboard temperature or fan speed?**
Proxmox exposes no such endpoints (none of the 437 leaf endpoints mention temperature, sensors or fans).
**Disk temperature is available** — ATA, NVMe and SAS are parsed through different branches.

**The panel loads but everything is empty**
Check that PVE is reachable and the token has not expired. When data cannot be fetched the panel keeps
the last good snapshot and marks it "frozen" rather than blanking every number — during an incident you
want to see what it looked like before.

**Where is the data stored?**
Configuration, tokens, logs and backups all live in `/usr/local/apps/@appdata/fn-pvepanel`.

---

## Security

- PVE addresses and API tokens are stored on the device only; nothing is rendered into the page and
  nothing is sent to an external service;
- **The panel listens on `127.0.0.1`** and is reached through the fnOS desktop entry's CGI, which is
  behind the fnOS login. If you make it listen externally, anyone who can reach that port can read the
  whole cluster — add authentication of your own first;
- Treat exported backups (no tokens) and on-device automatic backups (with tokens) accordingly.

---

## Known limitations

- **No self-healing**: the service starts with the system, but nothing restarts it after a `kill -9`.
  Start it again from App Center;
- **Write actions are limited to guest power state** — snapshots, triggering backups, creating or
  deleting guests are not implemented;
- **No alert history**: the panel shows current state and history charts, but there is no alert timeline;
- On a multi-node cluster the history chart reads RRD from the first node only.

---

## Build from source

```
src/fn-pvepanel/
├── manifest                 # package name, version, port, platform
├── ICON.PNG / ICON_256.PNG  # application icons (64 / 256)
├── config/                  # run-as identity (root), shared-folder declaration
├── wizard/                  # install wizard (port 39205 pre-filled, editable there)
├── cmd/main                 # lifecycle: start / stop / status, resolves the actual port
└── app/
    ├── server/              # pvepanel.py (HTTP + routes), collector.py (collection + cache),
    │                        # pveclient.py (connection pool), store.py (config + tokens),
    │                        # notify.py (nine channels), update.py, installer.py, maint.py
    ├── web/                 # index.html + style.css + js/ + lib/vue.global.prod.js
    └── ui/                  # desktop entry (index.cgi reverse proxy) and nine icon sizes
```

`.github/workflows/build-fpk.yml` builds, verifies and publishes a Release when a `v*` tag is pushed.
Packaging **must happen on Linux** (Windows loses the executable bits and the install fails with 10111);
`tools/build_fpk.py` does it over SSH with the official `fnpack`.

```bash
python tools/build_fpk.py     # structure check, then package
python tools/deploy.py        # uninstall, install, start, probe
python tools/preflight.py     # run every CI gate locally
```

### Port

The panel listens on **39205** by default.

The install wizard shows this number pre-filled — just accept it. The port is bound to
`127.0.0.1` only; the desktop entry reaches the panel through the fnOS login-gated CGI, so
nothing external can connect to it directly.

| What you want | How |
|---|---|
| Use the default | Nothing to do at install time |
| Pick another during install | Edit the port field in the wizard |
| Change it afterwards | Uninstall + reinstall, or change the port in the app settings (restarts the service) |
| A reproducible deployment | `python tools/deploy.py --env BACKEND_PORT=25000` |

> `deploy.py` and `shot.py` read the port out of the package themselves — no need to pass it.

---

## License

[MIT](LICENSE) © 2026 Smi13zZ

Not affiliated with Proxmox Server Solutions GmbH or the fnOS project.
Proxmox VE is a trademark of Proxmox Server Solutions GmbH.
