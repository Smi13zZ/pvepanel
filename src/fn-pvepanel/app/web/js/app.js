/* pvepanel - Vue application shell */
(function () {
  "use strict";

  const { createApp } = Vue;
  const t = function (k, v) { return window.I18N.t(k, v); };

  function svg(body, size) {
    return '<svg viewBox="0 0 24 24" width="' + (size || 18) + '" height="' + (size || 18) +
      '" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" ' +
      'stroke-linejoin="round">' + body + "</svg>";
  }

  const ICONS = {
    dash: svg('<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>'),
    server: svg('<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>'),
    cluster: svg('<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>'),
    disk: svg('<rect x="2.5" y="5" width="19" height="6" rx="2"/><rect x="2.5" y="13" width="19" height="6" rx="2"/><path d="M6.5 8h.01M6.5 16h.01"/><path d="M17 8h2.5M17 16h2.5"/>'),
    storage: svg('<path d="M3 7c0-1.7 4-3 9-3s9 1.3 9 3-4 3-9 3-9-1.3-9-3z"/><path d="M3 7v5c0 1.7 4 3 9 3s9-1.3 9-3V7"/><path d="M3 12v5c0 1.7 4 3 9 3s9-1.3 9-3v-5"/>'),
    list: svg('<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>'),
    gear: svg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>'),
    cpu: svg('<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>'),
    mem: svg('<rect x="2.5" y="7" width="19" height="10" rx="2"/><path d="M6 17v3M10 17v3M14 17v3M18 17v3"/><path d="M6.5 11h3M12 11h3"/>'),
    gauge: svg('<path d="M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4z"/><path d="M13.4 10.6 19 5"/><path d="M4.5 18a9 9 0 1 1 15 0"/>'),
    refresh: svg('<path d="M20 11A8 8 0 0 0 6.3 6.3L4 8.5"/><path d="M4 4v4.5h4.5"/><path d="M4 13a8 8 0 0 0 13.7 4.7L20 15.5"/><path d="M20 20v-4.5h-4.5"/>'),
    menu: svg('<path d="M3 6h18M3 12h18M3 18h18"/>', 18),
    sun: svg('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
    moon: svg('<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>'),
    monitor: svg('<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>'),
    lang: svg('<path d="m5 8 6 6"/><path d="m4 14 6-6 2-3"/><path d="M2 5h12"/><path d="M7 2h1"/><path d="m22 22-5-10-5 10"/><path d="M14 18h6"/>'),
    globe: svg('<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18z"/>', 14),
    collapse: svg('<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/><path d="m16 15-3-3 3-3"/>'),
    expand: svg('<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/><path d="m13 9 3 3-3 3"/>'),
    alert: svg('<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>', 16),
    info: svg('<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>', 16),
    check: svg('<path d="M20 6L9 17l-5-5"/>', 16),
    close: svg('<path d="M18 6L6 18M6 6l12 12"/>', 16),
    shield: svg('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>', 40),
    bell: svg('<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>'),
    play: svg('<path d="M6 4l14 8-14 8z"/>', 15),
    reboot: svg('<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/>', 15),
    power: svg('<path d="M12 3v9"/><path d="M6.4 6.4a8 8 0 1 0 11.2 0"/>', 15),
    square: svg('<rect x="6" y="6" width="12" height="12" rx="1.5" fill="currentColor" stroke="none"/>', 15),
    plus: svg('<path d="M12 5v14M5 12h14"/>', 16),
    download: svg('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>', 16),
    folder: svg('<path d="M4 20a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2z"/>', 16),
    arrowRight: svg('<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>', 20),
    spark: svg('<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M18 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/>', 20),
  };

  const THEMES = ["auto", "light", "dark"];
  const THEME_ICON = { auto: "monitor", light: "sun", dark: "moon" };
  const THEME_KEY = { auto: "side.theme.auto", light: "side.theme.light", dark: "side.theme.dark" };
  const TASK_KEYS = {
    vzdump: "task.backup", qmstart: "task.startvm", qmstop: "task.stopvm",
    qmshutdown: "task.shutdownvm", qmcreate: "task.createvm", qmclone: "task.clonevm",
    qmmigrate: "task.migrate", qmsnapshot: "task.snapshot", qmdelsnapshot: "task.delsnapshot",
    vzstart: "task.startct", vzstop: "task.stopct", vzshutdown: "task.shutdownct",
    aptupdate: "task.aptupdate", pveupdate: "task.pveupdate",
    startall: "task.startall", stopall: "task.stopall",
  };

  /* Rows per page in the task table. The list is capped so the card cannot
     grow to a screenful; tasks beyond this are reachable through the pager. */
  const TASKS_PER_PAGE = 10;

  const app = createApp({
    data() {
      return {
        loaded: false, busy: false, saving: false,
        view: "dash", setTab: "servers",
        collapsed: false, navOpen: false,
        theme: "auto", lang: "zh",
        appVersion: "0.0.0",
        settings: { notify_events: {} },
        servers: [], activeId: "",
        notify: { types: {}, channels: [], available: false },
        updateInfo: {}, updateChecking: false,
        snapshot: null,
        FMT: window.FMT,
        toasts: [], diag: {}, logLines: [],
        lastError: "", errorAt: 0,
        gfilter: "all", gsearch: "",
        acting: null, testingServer: null,
        serverForm: null, channelForm: null, confirmState: null,
        pickerOpen: null,
        testing: null,
        timer: null,
        timeframe: "hour",
        taskTab: "tasks", taskPage: 1,

        // ---- one-click update ----
        updateInstalling: false,
        updateInstallVersion: "",
        updateInstallDetail: "",
        updateProgressDone: 0,
        updateProgressTotal: 0,
        updateError: "",
        updatePollTimer: null,
        updateBaseline: "",
        updatePollFails: 0,

        updatePeakPct: 0,

        // ---- maintenance ----
        maint: { ok: true, directory: "", enabled: false, interval_hours: 24,
                 retention_days: 7, last_success: 0, next_run: 0,
                 last_error: "", backups: [] },
        autoForm: { auto_backup_enabled: false, auto_backup_interval_hours: 24, auto_backup_retention_days: 7 },
        maintBusy: false,
        backupFile: null,

        // ---- storage ----
        // items/total/updates come straight from the API; the page never computes
        // disk usage itself, it only decides how to label what it was handed.
        storage: { ok: true, items: [], total: 0, var_dir: "", updates: [],
                   updates_keep: 2, current_version: "" },
        storageLoading: false,
        storageBusy: "",
      };
    },

    computed: {
      navItems() {
        return [
          { id: "dash", key: "nav.dash", icon: "dash" },
          { id: "guests", key: "nav.guests", icon: "server" },
          { id: "storage", key: "nav.storage", icon: "storage" },
          { id: "disks", key: "nav.disks", icon: "disk" },
          { id: "tasks", key: "nav.tasks", icon: "list" },
          { id: "settings", key: "nav.settings", icon: "gear" },
        ];
      },
      pageTitle() {
        const item = this.navItems.find((n) => n.id === this.view);
        return item ? t(item.key) : "PVEpanel";
      },
      setSections() {
        return [
          { id: "servers", label: t("set.servers") },
          { id: "notify", label: t("set.notify") },
          { id: "maint", label: t("maint.title") },
          { id: "storage", label: t("storage.tab") },
          { id: "update", label: t("update.title") },
          { id: "ui", label: t("set.theme") },
        ];
      },
      themeOptions() {
        return THEMES.map((v) => ({ v: v, label: t(THEME_KEY[v]) }));
      },
      themeIcon() { return THEME_ICON[this.theme] || "monitor"; },
      themeTooltip() { return t("side.theme") + ": " + t(THEME_KEY[this.theme]); },
      activeServer() {
        return this.servers.find((s) => s.id === this.activeId) || null;
      },
      activeServerName() {
        const s = this.activeServer;
        return s ? (s.name || s.host) : t("top.noServer");
      },
      activeAddr() {
        const s = this.activeServer;
        return s ? (s.host + ":" + s.port) : "";
      },
      meta() { return (this.snapshot && this.snapshot.meta) || {}; },
      authFailed() { return !!(this.meta && this.meta.auth_failed); },
      stale() { return !!(this.snapshot && this.snapshot.stale); },
      staleAge() {
        const ts = (this.meta && this.meta.fetched_at) || 0;
        return ts ? window.FMT.fmtAgo(ts) : "";
      },
      cluster() {
        const c = (this.snapshot && this.snapshot.cluster) || { nodes: [], quorate: null, name: "" };
        const online = (c.nodes || []).filter(function (n) { return n.online; }).length;
        return {
          name: c.name, quorate: c.quorate === undefined ? null : c.quorate,
          onlineCount: online, total: (c.nodes || []).length,
        };
      },
      nodes() { return (this.snapshot && this.snapshot.nodes) || []; },
      node() { return this.nodes.length ? this.nodes[0] : null; },
      updateNew() { return !!(this.updateInfo && this.updateInfo.has_update); },
      /* The updater reports a machine-readable reason; the panel turns it into
         a sentence. "not_found" must not read as "up to date" - those mean
         different things to whoever is looking at the page. */
      updateHeadline() {
        const info = this.updateInfo || {};
        if (info.has_update) return t("update.newTitle");
        const table = window.I18N.STRINGS[this.lang] || {};
        if (info.reason && table["update.reason." + info.reason] !== undefined) {
          return t("update.reason." + info.reason);
        }
        return info.checked ? t("update.upTitle") : t("update.neverChecked");
      },
      updateDetail() {
        const info = this.updateInfo || {};
        const table = window.I18N.STRINGS[this.lang] || {};
        if (info.has_update) return t("update.newSub");
        if (info.reason) {
          const key = "update.detail." + info.reason;
          if (table[key] !== undefined) return t(key);
          return info.detail || info.error || "";
        }
        if (info.checked) return t("update.detail.ok");
        return "";
      },
      updateProgressKnown() {
        return this.updateProgressTotal > 0;
      },
      /* The server reports an instantaneous snapshot, so the computed value can
         go backwards between two polls (a retry restarts the counter). The
         watcher below latches the peak; the bar only ever grows. */
      updateProgressPct() {
        const total = Number(this.updateProgressTotal) || 0;
        if (!total) return 0;
        return Math.min(100, Math.round((Number(this.updateProgressDone) || 0) / total * 100));
      },
      /* Labels come from i18n, not from the API: the server sends a key plus a
         Chinese fallback, so an English UI stays English. A key with no
         translation yet falls back to whatever the server called it. */
      storageItems() {
        const self = this;
        return ((this.storage && this.storage.items) || []).map(function (it) {
          const key = "storage.item." + it.key;
          const translated = window.I18N.STRINGS[self.lang] || {};
          return Object.assign({}, it, {
            label: translated[key] !== undefined ? t(key) : (it.label || it.key),
          });
        });
      },
      storageUpdates() { return (this.storage && this.storage.updates) || []; },
      /* How much a given cleanup would actually free. The running version's
         package is protected by the server, so counting it here would promise
         space that never comes back. */
      storageReclaim() {
        const self = this;
        const byKey = {};
        const items = (self.storage && self.storage.items) || [];
        items.forEach(function (it) { byKey[it.key] = Number(it.bytes) || 0; });
        let updates = byKey.updates || 0;
        const cur = (self.storage && self.storage.current_version) || "";
        if (cur) {
          ((self.storage && self.storage.updates) || []).forEach(function (u) {
            if (u && !u.partial && String(u.name || "").indexOf(cur) >= 0) {
              updates = Math.max(0, updates - (Number(u.bytes) || 0));
            }
          });
        }
        const logs = (byKey.panel_log || 0) + (byKey.panel_log1 || 0) + (byKey.update_log || 0);
        return { updates: updates, logs: logs, all: updates + logs };
      },
      guests() { return (this.snapshot && this.snapshot.guests) || []; },
      guestCount() { return this.guests.filter(function (g) { return !g.template; }).length; },
      runningCount() { return this.guests.filter(function (g) { return g.status === "running"; }).length; },
      filteredGuests() {
        const q = (this.gsearch || "").trim().toLowerCase();
        return this.guests.filter((g) => {
          if (this.gfilter === "running" && g.status !== "running") return false;
          if (this.gfilter === "stopped" && g.status === "running") return false;
          if (!q) return true;
          return String(g.name || "").toLowerCase().indexOf(q) >= 0 || String(g.vmid).indexOf(q) >= 0;
        });
      },
      storages() { return (this.snapshot && this.snapshot.storages) || []; },
      disks() {
        const smart = (this.snapshot && this.snapshot.smart) || [];
        const list = (this.snapshot && this.snapshot.disks) || [];
        return list.map(function (d) {
          const s = smart.find(function (x) { return x.devpath === d.devpath && x.node === d.node; });
          if (!s) return d;
          return Object.assign({}, d, {
            temp: s.temp, health: s.health || d.health,
            wearout: s.wearout !== undefined && s.wearout !== null ? s.wearout : d.wearout,
          });
        });
      },
      tasks() { return (this.snapshot && this.snapshot.tasks) || []; },
      taskPages() {
        return Math.max(1, Math.ceil(this.tasks.length / TASKS_PER_PAGE));
      },
      pagedTasks() {
        const start = (this.taskPage - 1) * TASKS_PER_PAGE;
        return this.tasks.slice(start, start + TASKS_PER_PAGE);
      },
      services() { return (this.snapshot && this.snapshot.services) || []; },
      alerts() { return (this.snapshot && this.snapshot.alerts) || []; },
      alertErrors() { return this.alerts.filter(function (a) { return a.level === "err"; }).length; },
      backupGaps() { return (this.snapshot && this.snapshot.backup_gaps) || []; },
      subs() { return (this.snapshot && this.snapshot.subs) || {}; },
      pveVersion() {
        const fp = (this.snapshot && this.snapshot.fingerprint) || {};
        return fp.version || "";
      },
      haSummary() {
        const ha = (this.snapshot && this.snapshot.ha) || {};
        const res = ha.resources || [];
        if (!res.length) return ha.quorate === null || ha.quorate === undefined
          ? t("dash.noHa") : t("dash.haNone");
        const up = res.filter(function (x) { return x.status === "started"; }).length;
        return up + "/" + res.length;
      },
      bootText() {
        const n = this.node;
        if (!n) return "—";
        const mode = n.bootmode === "efi" ? "UEFI" : (n.bootmode || "—");
        const sb = n.secureboot === 1 ? " · SecureBoot" : "";
        return mode + sb;
      },
      loadOne() {
        const n = this.node;
        if (!n || !n.loadavg || !n.loadavg.length) return "—";
        return Number(n.loadavg[0]).toFixed(2);
      },
      loadText() {
        const n = this.node;
        if (!n || !n.loadavg || !n.loadavg.length) return "—";
        return n.loadavg.map(function (x) { return Number(x).toFixed(2); }).join(" / ");
      },
      loadPct() {
        const n = this.node;
        if (!n || !n.loadavg || !n.loadavg.length || !n.maxcpu) return 0;
        return Math.min(100, (Number(n.loadavg[0]) / Number(n.maxcpu)) * 100);
      },
      loadTone() {
        const n = this.node;
        if (!n || !n.maxcpu) return "";
        const r = Number((n.loadavg || [0])[0]) / Number(n.maxcpu);
        const warn = Number(this.settings.load_warn) || 0.85;
        return r >= warn * 1.15 ? "err" : r >= warn ? "warn" : "";
      },
      timeframes() {
        return [
          { v: "hour", label: t("common.hour") }, { v: "day", label: t("common.day") },
          { v: "week", label: t("common.week") }, { v: "month", label: t("common.month") },
          { v: "year", label: t("common.year") },
        ];
      },
      tfSpanText() { return t("dash.chartSpan." + this.timeframe); },
      nodeSeries() {
        return [
          { key: "cpu", label: t("dash.cpu"), color: CHART.cssVar("--accent-bar", "#2563eb") },
          { key: "memused", label: t("dash.mem"), color: CHART.cssVar("--ul", "#d97706") },
        ];
      },
      channels() { return (this.notify && this.notify.channels) || []; },
      channelTypes() { return (this.notify && this.notify.types) || {}; },
      channelFields() {
        const form = this.channelForm;
        if (!form) return [];
        const meta = this.channelTypes[form.type] || {};
        const names = meta.fields || [];
        const secrets = meta.secrets || [];
        const lang = this.lang;
        const labels = meta["labels_i18n"] || {};
        const hints = meta["hints_i18n"] || {};
        return names.map(function (name) {
          const lab = labels[name] || {};
          const hint = hints[name] || {};
          return {
            key: name,
            label: lab[lang] || lab.zh || name,
            hint: hint[lang] || hint.zh || "",
            secret: secrets.indexOf(name) >= 0,
          };
        });
      },
      connTone() {
        if (!this.servers.length) return "";
        if (this.authFailed) return "err";
        if (this.meta && this.meta.reason) return "err";
        return this.snapshot && this.snapshot.ok ? "on" : "warn";
      },
      connText() {
        if (!this.servers.length) return t("top.noServer");
        if (this.authFailed) return t("top.authFailed");
        if (this.meta && this.meta.reason) return t("top.disconnected");
        return this.snapshot && this.snapshot.ok ? t("top.connected") : t("top.connecting");
      },
      connTitle() {
        if (!this.servers.length) return t("top.noServer");
        if (this.authFailed) return t("top.authFailed");
        const errs = (this.snapshot && this.snapshot.errors) || [];
        return errs.length ? errs.join("; ") : t("top.connected");
      },
      isMobile() {
        return typeof window !== "undefined" && window.innerWidth <= 900;
      },
    },

    methods: {
      ic(name) { return ICONS[name] || ""; },
      t(key, vars) { return t(key, vars); },
      ratio(a, b) { return window.FMT.pct((Number(a) || 0) / (Number(b) || 1)); },
      toneFor(a, b) {
        const r = (Number(a) || 0) / (Number(b) || 1);
        return r >= 0.9 ? "err" : r >= 0.85 ? "warn" : "";
      },
      stateText(s) {
        return { running: t("guest.stateRunning"), stopped: t("guest.stateStopped"),
                 paused: t("guest.statePaused") }[s] || t("guest.stateUnknown");
      },
      /* PVE reports "N/A" for a disk it cannot probe (typical for virtio and
         for controllers without SMART passthrough). Printing that raw gives
         "N/A%" or a bare sentinel next to a real percentage; a dash plus a
         tooltip is honest and does not look like a rendering bug. */
      wearText(v) {
        if (v === null || v === undefined || v === '') return "—";
        const s = String(v).trim();
        if (/^(n\/a|na|unknown|-)$/i.test(s)) return "—";
        const n = Number(s);
        if (!isFinite(n)) return "—";
        return n + "%";
      },
      alertTitle(al) {
        if (al.key && window.I18N.STRINGS[this.lang]["alert.title." + al.key] !== undefined) {
          return t("alert.title." + al.key);
        }
        return al.title || "";
      },
      alertText(al) {
        if (al.key) {
          const key = "alert." + al.key;
          const table = window.I18N.STRINGS[this.lang] || {};
          if (table[key] !== undefined) return t(key, al.params || {});
        }
        return al.detail || "";
      },
      taskLabel(type) {
        const key = TASK_KEYS[type];
        return key ? t(key) : (type || t("task.generic"));
      },
      taskStateText(tk) {
        const st = window.FMT.taskState(tk);
        return st.tone === "ok" ? t("task.ok") : st.tone === "err" ? t("task.fail") : t("task.running");
      },
      subText(s) {
        if (!s) return "—";
        const map = { active: t("task.valid"), expired: t("task.expired"),
                      notfound: t("task.noSubState"), new: t("task.newSub") };
        const label = map[String(s.status || "").toLowerCase()] || s.status || "—";
        return label + " · " + t("task.due") + " " + (s.nextduedate || "—");
      },
      chTypeLabel(type) {
        const meta = this.channelTypes[type] || {};
        return (meta.label && (meta.label[this.lang] || meta.label.zh)) || type;
      },
      backupLabel(name) {
        // pvepanel-backup-20261106-021500.json -> 2026-11-06 02:15:00
        const m = /(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(String(name || ""));
        if (!m) return name;
        return m[1] + "-" + m[2] + "-" + m[3] + " " + m[4] + ":" + m[5] + ":" + m[6];
      },

      // ---- navigation / theme / language ----

      go(id) {
        this.view = id;
        this.navOpen = false;
        // Canvas elements are recreated when a view is re-shown (the templates
        // sit behind v-if), so the cached chart must be re-bound and repainted.
        this.$nextTick(() => { this.drawNodeChart(); this.drawRings(); });
        if (id === "settings") {
          this.loadLog();
          this.checkUpdate(false);
          if (this.setTab === "maint") this.loadMaint();
          if (this.setTab === "storage") this.loadStorage();
        }
        if (id === "dash") this.$nextTick(this.drawNodeChart);
        if (id === "storage") this.$nextTick(this.drawRings);
      },

      setTaskTab(id) {
        this.taskTab = id;
        this.taskPage = 1;
      },

      cycleTheme() {
        const i = THEMES.indexOf(this.theme);
        this.setTheme(THEMES[(i + 1) % THEMES.length]);
      },

      setTheme(v) {
        this.theme = THEMES.indexOf(v) >= 0 ? v : "auto";
        this.applyTheme();
        try { localStorage.setItem("pvepanel.theme", this.theme); } catch (e) {}
        this.saveSettings({ theme: this.theme }, true);
        this.$nextTick(() => { this.drawNodeChart(); this.drawRings(); });
      },

      applyTheme() {
        let th = this.theme;
        if (th === "auto") {
          th = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
            ? "dark" : "light";
        }
        document.documentElement.setAttribute("data-theme", th);
      },

      toggleLang() {
        this.setLang(this.lang === "zh" ? "en" : "zh");
      },

      setLang(lang) {
        this.lang = lang === "en" ? "en" : "zh";
        window.I18N.setLang(this.lang);
        try { localStorage.setItem("pvepanel.lang", this.lang); } catch (e) {}
        this.saveSettings({ lang: this.lang }, true);
        this.$nextTick(() => { this.drawNodeChart(); this.drawRings(); });
      },

      toast(msg, tone) {
        const item = { msg: msg, tone: tone || "" };
        this.toasts.push(item);
        setTimeout(() => {
          const i = this.toasts.indexOf(item);
          if (i >= 0) this.toasts.splice(i, 1);
        }, 3600);
      },

      // ---- data ----

      async bootstrap(force) {
        try {
          const r = await API.bootstrap(force, this.activeId, this.timeframe);
          this.appVersion = r.app.version;
          this.settings = Object.assign(this.settings, r.settings || {});
          this.servers = r.servers || [];
          this.activeId = (r.settings && r.settings.active) || (this.servers[0] && this.servers[0].id) || "";
          if (r.notify) this.notify = r.notify;
          if (r.update) this.updateInfo = r.update;
          this.applySnapshot(r.snapshot);
          this.loaded = true;
          // The first paint happens here, not in refresh(): bootstrap fills the
          // data but never touched the canvas, so the chart stayed blank until
          // something else (a timeframe click, a resize) happened to redraw it.
          this.$nextTick(() => { this.drawNodeChart(); this.drawRings(); });
        } catch (e) {
          this.toast(String(e.message || e), "err");
          this.loaded = true;
        }
      },

      async refresh(force) {
        if (this.busy) return;
        this.busy = true;
        try {
          const r = force
            ? await API.refresh(this.activeId, this.timeframe)
            : await API.snapshotUrl("./api/snapshot?tf=" + encodeURIComponent(this.timeframe) +
                (this.activeId ? "&server=" + encodeURIComponent(this.activeId) : ""));
          this.applySnapshot(r.snapshot || r);
        } catch (e) {
          this.toast(String(e.message || e), "err");
        } finally {
          this.busy = false;
          this.$nextTick(() => { this.drawNodeChart(); this.drawRings(); });
        }
      },

      applySnapshot(snap) {
        if (!snap) return;
        // A failed poll must not wipe the screen. Previously every response
        // replaced the snapshot wholesale, so one transient failure showed an
        // empty panel until the next poll succeeded - the data appeared to
        // "vanish and come back". Keep rendering the last good payload and
        // only surface the error, unless there never was a good payload.
        const failed = snap.ok === false;
        const havePrev = !!(this.snapshot && this.snapshot.ok !== false);
        if (failed && havePrev && !snap.stale) {
          this.lastError = (snap.errors && snap.errors[0]) || String((snap.meta || {}).reason || "");
          this.errorAt = Date.now();
          if (this.meta && this.meta.auth_failed) this.stopPolling();
          else this.startPolling();
          return;
        }
        const prevLen = this.tasks.length;
        this.snapshot = snap;
        if (snap.timeframe) this.timeframe = snap.timeframe;
        // keep the pager inside range when the task list shrinks
        if (this.taskPage > this.taskPages) this.taskPage = this.taskPages;
        if (prevLen !== this.tasks.length) this.taskPage = 1;
        this.$nextTick(() => this.drawNodeChart());
        if (!failed) { this.lastError = ""; this.errorAt = 0; }
        if (this.meta && this.meta.auth_failed) this.stopPolling();
        else this.startPolling();
      },

      startPolling() {
        this.stopPolling();
        const iv = Math.max(5, Number(this.settings.refresh) || 10) * 1000;
        this.timer = setInterval(() => this.refresh(false), iv);
      },

      stopPolling() {
        if (this.timer) { clearInterval(this.timer); this.timer = null; }
      },

      pickTf(v) {
        this.timeframe = v;
        this.refresh(true);
      },

      // ---- servers ----

      switchServer(id) {
        if (!id || id === this.activeId) { this.pickerOpen = null; return; }
        this.activeId = id;
        this.pickerOpen = null;
        API.setActive(id).catch(function () {});
        this.snapshot = null;
        this.refresh(true);
      },

      pickServer() { this.pickerOpen = true; },

      openServerForm(srv) {
        this.serverForm = srv
          ? { id: srv.id, name: srv.name, host: srv.host, port: srv.port,
              token: "", verify_tls: !!srv.verify_tls, token_set: !!srv.token_set }
          : { id: "", name: "", host: "", port: 8006, token: "",
              verify_tls: false, token_set: false };
      },

      /* Test one endpoint without touching the active one: the settings page
         has to be able to check a server before switching to it. */
      async testServer(srv) {
        if (this.testingServer) return;
        this.testingServer = srv.id;
        try {
          const r = await API.testServer({ id: srv.id });
          this.toast(r.ok ? t("set.testOk") : t("set.testFail", { msg: r.error || "?" }),
                     r.ok ? "" : "err");
        } catch (e) {
          this.toast(t("set.testFail", { msg: String(e.message || e) }), "err");
        } finally {
          this.testingServer = null;
        }
      },

      async saveServer() {
        const form = this.serverForm;
        if (!form || !form.host) { this.toast(t("set.addr"), "err"); return; }
        this.saving = true;
        try {
          // Only fields that were actually edited are sent: the token input
          // starts empty, and an empty token means "keep the stored one".
          const patch = {
            id: form.id || undefined, name: form.name, host: form.host,
            port: form.port, verify_tls: form.verify_tls,
          };
          if (form.token) patch.token = form.token;
          const r = await API.saveServer(patch);
          this.servers = r.servers || [];
          if (!form.id && r.server) {
            this.activeId = r.server.id;
            API.setActive(r.server.id).catch(function () {});
          }
          this.serverForm = null;
          this.toast(t("common.saved"));
          await this.refresh(true);
        } catch (e) {
          this.toast(String(e.message || e), "err");
        } finally {
          this.saving = false;
        }
      },

      removeServer(srv) {
        this.confirmState = {
          title: t("set.delete"),
          body: t("set.deleteConfirm", { name: srv.name || srv.host }),
          danger: true,
          ok: async () => {
            this.confirmState = null;
            try {
              const r = await API.deleteServer(srv.id);
              this.servers = r.servers || [];
              this.activeId = (r.settings && r.settings.active) ||
                (this.servers[0] && this.servers[0].id) || "";
              this.toast(t("common.deleted"));
              this.snapshot = null;
              await this.refresh(true);
            } catch (e) {
              this.toast(String(e.message || e), "err");
            }
          },
        };
      },

      // ---- guest actions ----

      guestAction(g, action) {
        if (this.acting) return;
        const run = async () => {
          this.confirmState = null;
          this.acting = g.vmid;
          try {
            await API.guestAction({
              server: this.activeId, node: g.node, vmid: g.vmid,
              type: g.type, action: action,
            });
            this.toast(t("guest.done", { action: t("guest." + action), name: g.name }));
            // A power action takes a moment on the PVE side (qemu stop/shutdown
            // can hold for several seconds). Refresh twice so the row settles
            // on the real state instead of flashing "still running".
            setTimeout(() => this.refresh(true), 2500);
            setTimeout(() => this.refresh(true), 8000);
          } catch (e) {
            const msg = String(e.message || e);
            this.toast(msg.indexOf("403") >= 0 || msg.indexOf("permission") >= 0
              ? t("guest.noPerm") : t("guest.failed", { msg: msg }), "err");
          } finally {
            this.acting = null;
          }
        };
        const isStop = action === "stop";
        this.confirmState = {
          title: t("guest." + action),
          body: t("guest.confirm", { name: g.name, action: t("guest." + action) }) +
                (isStop ? " " + t("guest.confirmStop") : ""),
          danger: isStop,
          ok: run,
        };
      },

      // ---- settings ----

      async saveSettings(patch, silent) {
        const body = patch || {
          storage_warn: this.settings.storage_warn,
          temp_warn: this.settings.temp_warn,
          temp_crit: this.settings.temp_crit,
          load_warn: this.settings.load_warn,
          mem_warn: this.settings.mem_warn,
          refresh: this.settings.refresh,
          notify_enabled: this.settings.notify_enabled,
          notify_events: this.settings.notify_events,
          update_check: this.settings.update_check,
        };
        this.saving = true;
        try {
          const r = await API.saveSettings(body);
          this.settings = Object.assign(this.settings, r.settings || {});
          if (r.servers) this.servers = r.servers;
          if (!silent) this.toast(t("common.saved"));
          if (patch && patch.refresh) this.startPolling();
        } catch (e) {
          this.toast(String(e.message || e), "err");
        } finally {
          this.saving = false;
        }
      },

      async runDiagnose() {
        this.diag = {};
        try {
          const r = await API.diagnose(this.activeId);
          this.diag = r.result || {};
        } catch (e) {
          this.diag = { steps: [{ name: t("set.conn"), ok: false, detail: String(e.message || e) }] };
        }
      },

      async loadLog() {
        try {
          const r = await API.log(200);
          this.logLines = r.lines || [];
        } catch (e) { /* log is best-effort */ }
      },

      // ---- notifications ----

      openChannelForm(ch) {
        if (ch) {
          this.channelForm = {
            id: ch.id, type: ch.type, name: ch.name,
            enabled: ch.enabled !== false,
            config: Object.assign({}, ch.config || {}),
          };
        } else {
          const first = Object.keys(this.channelTypes)[0] || "bark";
          this.channelForm = {
            id: "", type: first, name: this.chTypeLabel(first),
            enabled: true, config: {},
          };
        }
        this.onChannelType();
      },

      onChannelType() {
        const form = this.channelForm;
        if (!form) return;
        const meta = this.channelTypes[form.type] || {};
        if (!form.name) form.name = this.chTypeLabel(form.type);
        if (!form.config) form.config = {};
        (meta.fields || []).forEach(function (name) {
          if (form.config[name] === undefined) form.config[name] = "";
        });
      },

      async saveChannel() {
        const form = this.channelForm;
        if (!form) return;
        this.saving = true;
        try {
          // Secrets arrive masked; the server merges them field by field, so a
          // blank input keeps whatever is already stored.
          const r = await API.saveChannel({
            id: form.id || undefined, type: form.type, name: form.name,
            enabled: form.enabled !== false, config: form.config || {},
          });
          if (r.notify) this.notify = r.notify;
          this.channelForm = null;
          this.toast(t("common.saved"));
        } catch (e) {
          this.toast(String(e.message || e), "err");
        } finally {
          this.saving = false;
        }
      },

      removeChannel(ch) {
        this.confirmState = {
          title: t("set.delete"),
          body: t("set.deleteConfirm", { name: ch.name || ch.type }),
          danger: true,
          ok: async () => {
            this.confirmState = null;
            try {
              const r = await API.deleteChannel(ch.id);
              if (r.notify) this.notify = r.notify;
              this.toast(t("common.deleted"));
            } catch (e) {
              this.toast(String(e.message || e), "err");
            }
          },
        };
      },

      async testChannel(ch) {
        if (this.testing) return;
        this.testing = ch.id;
        try {
          const r = await API.testChannel(ch);
          this.toast(r.ok ? t("common.saved") : (r.detail || r.error || t("guest.failed", { msg: "?" })),
                     r.ok ? "" : "err");
        } catch (e) {
          this.toast(String(e.message || e), "err");
        } finally {
          this.testing = null;
        }
      },

      // ---- update ----

      async checkUpdate(force) {
        if (force) this.updateChecking = true;
        try {
          const r = await API.checkUpdate(force);
          this.updateInfo = r || {};
          if (r && r.has_update) this.updateBaseline = this.appVersion;
          if (force && r) {
            this.toast(r.has_update ? t("update.found", { v: r.latest }) : t("update.none"));
          }
        } catch (e) {
          if (force) this.toast(t("update.fail", { msg: String(e.message || e) }), "err");
        } finally {
          this.updateChecking = false;
        }
      },

      /* One-click update.
       *
       * The download happens inside the POST, so the progress poll MUST already
       * be running before that request is awaited - otherwise the bar sits at
       * zero for the whole download. The overlay stays up afterwards because
       * the install stops the very service serving this page. */
      async installUpdate() {
        if (this.updateInstalling) return;
        this.updateError = "";
        this.updateInstallVersion = (this.updateInfo && this.updateInfo.latest) || "";
        this.updateInstallDetail = "";
        this.updateProgressDone = 0;
        this.updateProgressTotal = 0;
        this.updatePeakPct = 0;
        this.updateInstalling = true;
        this.updateBaseline = this.appVersion;

        this.pollUpdateProgress();
        try {
          const r = await API.installUpdate();
          if (!r || r.ok === false) {
            throw new Error((r && (r.error || r.detail)) || "?");
          }
          this.toast(t("update.installStarted", { v: r.version || this.updateInstallVersion }));
          this.waitForUpgrade();
        } catch (e) {
          this.updateInstalling = false;
          this.stopUpdatePolling();
          this.updateError = t("update.installFailed", { msg: String(e.message || e) });
          this.toast(this.updateError, "err");
        }
      },

      pollUpdateProgress() {
        this.stopUpdatePolling();
        const tick = async () => {
          try {
            const s = await API.updateStatus();
            if (!s) return;
            // only ever grow the counters: the server reports an instantaneous
            // snapshot and a stalled read must not make the bar go backwards
            const done = Number(s.done) || 0;
            const total = Number(s.total) || 0;
            if (done > this.updateProgressDone) this.updateProgressDone = done;
            if (total > this.updateProgressTotal) this.updateProgressTotal = total;
            if (s.detail) this.updateInstallDetail = s.detail;
            if (s.phase === "failed" && s.detail) {
              this.updateError = s.detail;
            }
          } catch (e) {
            // the service is restarting; keep the overlay, just stop polling
          }
        };
        tick();
        this.updatePollTimer = setInterval(tick, 900);
      },

      stopUpdatePolling() {
        if (this.updatePollTimer) {
          clearInterval(this.updatePollTimer);
          this.updatePollTimer = null;
        }
      },

      /* Stay on the overlay until the panel reports a version different from
       * the one we started on. During the reinstall the API is simply gone, so
       * failures are expected and must not end the wait early. */
      async waitForUpgrade() {
        this.updateInstallDetail = t("update.installing");
        let fails = 0;
        for (let i = 0; i < 240; i++) {
          await new Promise((r) => setTimeout(r, 1500));
          try {
            const h = await fetch("./api/health", { cache: "no-store" }).then((r) => r.json());
            fails = 0;
            const v = (h && h.version) || "";
            if (v && v !== this.updateBaseline && v !== "0.0.0") {
              this.updateInstalling = false;
              this.stopUpdatePolling();
              this.toast(t("update.found", { v: v }));
              window.location.reload();
              return;
            }
            this.updateInstallDetail = t("update.installing");
          } catch (e) {
            fails++;
            // the uninstall→install window is the service being gone; that is
            // the plan, not a failure, so say so rather than looking stuck
            this.updateInstallDetail = t("update.progressUnknown");
          }
        }
        // never leave the operator staring at a frozen overlay
        this.updateInstalling = false;
        this.stopUpdatePolling();
        this.updateError = t("update.installFailed", { msg: "timeout" });
      },

      // ---- maintenance ----

      async loadMaint() {
        try {
          const r = await API.backupStatus();
          this.maint = Object.assign(this.maint, r || {});
          if (!this.maint.backups) this.maint.backups = [];
          this.autoForm.auto_backup_enabled = !!this.maint.enabled;
          this.autoForm.auto_backup_interval_hours = Number(this.maint.interval_hours) || 24;
          this.autoForm.auto_backup_retention_days = Number(this.maint.retention_days) || 7;
        } catch (e) {
          this.toast(String(e.message || e), "err");
        }
      },

      /* The downloaded copy is redacted: it leaves the device, so it must not
         carry the PVE tokens. The on-device automatic backups are the ones that
         keep them, because a restore that needed every token retyped would not
         be a backup. */
      async exportBackup() {
        try {
          await API.backupExport();
          this.toast(t("backup.exported"));
        } catch (e) {
          this.toast(String(e.message || e), "err");
        }
      },

      async exportToFolder() {
        if (this.maintBusy) return;
        this.maintBusy = true;
        try {
          const r = await API.backupToFolder();
          this.toast(t("backup.exportedTo", { path: r.path || "?" }));
          await this.loadMaint();
        } catch (e) {
          this.toast(String(e.message || e), "err");
        } finally {
          this.maintBusy = false;
        }
      },

      onPickBackup(ev) {
        const f = (ev.target.files || [])[0];
        this.backupFile = f || null;
      },

      doImport() {
        const file = this.backupFile;
        if (!file) return;
        this.confirmState = {
          title: t("backup.import"),
          body: t("backup.importWarning"),
          danger: true,
          ok: async () => {
            this.confirmState = null;
            this.maintBusy = true;
            try {
              const text = await file.text();
              const r = await API.backupImport(text);
              this.toast(t("backup.done", { n: (r.restored && r.restored.servers) || 0 }));
              this.backupFile = null;
              await this.bootstrap(true);
              await this.loadMaint();
            } catch (e) {
              this.toast(t("backup.importFail", { msg: String(e.message || e) }), "err");
            } finally {
              this.maintBusy = false;
            }
          },
        };
      },

      async saveAuto() {
        if (this.maintBusy) return;
        this.maintBusy = true;
        try {
          await this.saveSettings({
            auto_backup_enabled: !!this.autoForm.auto_backup_enabled,
            auto_backup_interval_hours: Number(this.autoForm.auto_backup_interval_hours) || 24,
            auto_backup_retention_days: Number(this.autoForm.auto_backup_retention_days) || 7,
          }, true);
          this.toast(t("common.saved"));
          await this.loadMaint();
        } catch (e) {
          this.toast(String(e.message || e), "err");
        } finally {
          this.maintBusy = false;
        }
      },

      async runBackupNow() {
        if (this.maintBusy) return;
        this.maintBusy = true;
        try {
          const r = await API.backupRun();
          this.toast(t("backup.runDone", { name: r.name || "?" }));
          await this.loadMaint();
        } catch (e) {
          this.toast(String(e.message || e), "err");
        } finally {
          this.maintBusy = false;
        }
      },

      askRestore(b) {
        this.confirmState = {
          title: t("backup.restore"),
          body: t("backup.restoreConfirm"),
          danger: true,
          ok: async () => {
            this.confirmState = null;
            this.maintBusy = true;
            try {
              const r = await API.backupRestore(b.name);
              this.toast(t("backup.done", { n: (r.restored && r.restored.servers) || 0 }));
              await this.bootstrap(true);
              await this.loadMaint();
            } catch (e) {
              this.toast(t("backup.importFail", { msg: String(e.message || e) }), "err");
            } finally {
              this.maintBusy = false;
            }
          },
        };
      },

      askDeleteBackup(b) {
        this.confirmState = {
          title: t("backup.delete"),
          body: t("backup.deleteConfirm", { name: b.name }),
          danger: true,
          ok: async () => {
            this.confirmState = null;
            try {
              await API.backupDelete(b.name);
              this.toast(t("backup.deleted"));
              await this.loadMaint();
            } catch (e) {
              this.toast(String(e.message || e), "err");
            }
          },
        };
      },

      // ---- storage ----

      async loadStorage() {
        this.storageLoading = true;
        try {
          const r = await API.storage();
          this.storage = Object.assign(this.storage, r || {});
          if (!Array.isArray(this.storage.items)) this.storage.items = [];
          if (!Array.isArray(this.storage.updates)) this.storage.updates = [];
        } catch (e) {
          this.toast(String(e.message || e), "err");
        } finally {
          this.storageLoading = false;
        }
      },

      /* Confirm before deleting anything, and say how much it would free.
         target = updates (cached packages) / logs / all. */
      askCleanup(target) {
        const self = this;
        const titles = {
          updates: "storage.cleanUpdates",
          logs: "storage.cleanLogs",
          all: "storage.cleanAll",
        };
        const bodies = {
          updates: "storage.cleanUpdatesText",
          logs: "storage.cleanLogsText",
          all: "storage.cleanAllText",
        };
        const freed = (this.storageReclaim[target] || 0);
        this.confirmState = {
          title: t(titles[target] || titles.all),
          body: t(bodies[target] || bodies.all, { v: window.FMT.fmtBytes(freed) }),
          danger: target === "all",
          ok: async () => {
            self.confirmState = null;
            await self.runCleanup(target);
          },
        };
      },

      async runCleanup(target) {
        if (this.storageBusy) return;
        this.storageBusy = target;
        try {
          const keep = Number(this.storage && this.storage.updates_keep) || 2;
          const r = await API.storageCleanup(target, keep);
          this.toast(t("storage.cleaned", { v: window.FMT.fmtBytes((r && r.freed) || 0) }));
          await this.loadStorage();
          // the log page caches what it fetched; those files just changed
          this.logLines = [];
          this.loadLog();
        } catch (e) {
          // 409 means a download is running: that is a state, not a failure
          this.toast(t("storage.cleanFailed", { msg: String(e.message || e) }), "err");
        } finally {
          this.storageBusy = "";
        }
      },

      // ---- canvas ----

      drawNodeChart() {
        const el = this.$refs.nodeChart;
        if (!el || !el.isConnected) return;
        if (!this._chart) this._chart = new CHART.LineChart(el);
        this._chart.attach(el);
        const pts = (this.snapshot && this.snapshot.rrd) || [];
        this._chart.setData(pts, this.nodeSeries, function (v) {
          return (Number(v) || 0).toFixed(1) + "%";
        }, this.timeframe, t("common.chartEmpty"));
      },

      drawRings() {
        if (this.view !== "storage") return;
        const self = this;
        this.storages.forEach(function (s) {
          const key = "ring-" + s.node + "-" + s.storage;
          const el = self.$refs[key];
          const canvas = Array.isArray(el) ? el[0] : el;
          if (!canvas || !canvas.isConnected) return;
          CHART.drawRing(canvas, s.frac, "",
                         s.frac >= 0.9 ? "err" : s.frac >= 0.85 ? "warn" : "");
        });
      },
    },

    watch: {
      setTab(id) {
        if (id === "maint") this.loadMaint();
        if (id === "storage") this.loadStorage();
        if (id === "update") this.checkUpdate(false);
      },
      updateProgressPct(v) {
        if (v > this.updatePeakPct) this.updatePeakPct = v;
      },
    },

    mounted() {
      try {
        const th = localStorage.getItem("pvepanel.theme");
        if (th && THEMES.indexOf(th) >= 0) this.theme = th;
        const lg = localStorage.getItem("pvepanel.lang");
        if (lg === "en" || lg === "zh") this.lang = lg;
      } catch (e) {}
      window.I18N.setLang(this.lang);
      this.applyTheme();
      this.navOpen = false;

      // The view can be chosen from the URL so screenshots and deep links land
      // on a specific page; the panel itself has no router.
      try {
        const q = new URLSearchParams(window.location.search);
        const v = q.get("view");
        if (v && this.navItems.some(function (n) { return n.id === v; })) this.view = v;
        const tab = q.get("tab");
        if (tab && ["servers", "notify", "maint", "storage", "update", "ui"].indexOf(tab) >= 0) {
          this.view = "settings";
          this.setTab = tab;
        }
        const th = q.get("theme");
        if (th && THEMES.indexOf(th) >= 0) { this.theme = th; this.applyTheme(); }
        const lg = q.get("lang");
        if (lg === "en" || lg === "zh") { this.lang = lg; window.I18N.setLang(lg); }
        const s = q.get("server");
        if (s) this.activeId = s;
        // timeframe is deep-linkable too: the chart's tick spacing differs per
        // range, so a bug report about the axis needs to name the range.
        const tf = q.get("tf");
        if (tf && ["hour", "day", "week", "month", "year"].indexOf(tf) >= 0) this.timeframe = tf;
      } catch (e) {}

      this.bootstrap(false);
      if (this.view === "settings") {
        this.loadLog();
        if (this.setTab === "maint") this.loadMaint();
        if (this.setTab === "storage") this.loadStorage();
      }

      if (window.matchMedia) {
        const mq = window.matchMedia("(prefers-color-scheme: dark)");
        if (mq.addEventListener) {
          mq.addEventListener("change", () => {
            if (this.theme === "auto") { this.applyTheme(); this.drawNodeChart(); }
          });
        }
      }
      window.addEventListener("resize", () => { this.drawNodeChart(); this.drawRings(); });
    },
  });

  app.mount("#app");
})();