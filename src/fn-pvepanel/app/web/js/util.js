/* pvepanel - formatting helpers */
(function () {
  "use strict";

  const UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];

  function fmtBytes(n, digits) {
    n = Number(n) || 0;
    if (n <= 0) return "0 B";
    let i = 0;
    while (n >= 1024 && i < UNITS.length - 1) { n /= 1024; i++; }
    const d = digits === undefined ? (i === 0 ? 0 : n >= 100 ? 0 : n >= 10 ? 1 : 2) : digits;
    return n.toFixed(d) + " " + UNITS[i];
  }

  /* PVE reports cpu and memory as fractions of the whole node, so the panel
     shows them as percentages almost everywhere. */
  function fmtPercent(p, digits) {
    const v = (Number(p) || 0) * 100;
    return v.toFixed(digits === undefined ? 1 : digits) + "%";
  }

  function pct(p) { return Math.max(0, Math.min(100, (Number(p) || 0) * 100)); }

  function fmtDuration(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    if (!sec) return "—";
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d > 0) return d + " 天 " + h + " 小时";
    if (h > 0) return h + " 小时 " + m + " 分";
    if (m > 0) return m + " 分";
    return sec + " 秒";
  }

  function fmtDate(unix) {
    const v = Number(unix) || 0;
    if (v <= 0) return "—";
    const d = new Date(v * 1000);
    const p = (x) => String(x).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function fmtTime(unix) {
    const v = Number(unix) || 0;
    if (v <= 0) return "—";
    const d = new Date(v * 1000);
    const p = (x) => String(x).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  /* "3 分钟前" - task rows care about recency, not the absolute clock. */
  function fmtAgo(unix) {
    const v = Number(unix) || 0;
    if (v <= 0) return "—";
    const s = Math.max(0, Math.floor(Date.now() / 1000 - v));
    if (s < 60) return s + " 秒前";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    return Math.floor(s / 86400) + " 天前";
  }

  /* PVE's task status is the literal string "OK" on success, otherwise the
     error text. Anything else (including an empty string, which means the task
     is still running) is not a result yet. */
  function taskState(t) {
    const s = String(t.status || "");
    if (!s) return { label: "运行中", tone: "warn" };
    if (s === "OK") return { label: "成功", tone: "ok" };
    return { label: "失败", tone: "err" };
  }

  const TASK_LABEL = {
    vzdump: "备份",
    qmstart: "启动虚拟机",
    qmstop: "停止虚拟机",
    qmshutdown: "关闭虚拟机",
    qmcreate: "创建虚拟机",
    qmclone: "克隆虚拟机",
    qmmigrate: "迁移虚拟机",
    qmsnapshot: "创建快照",
    qmdelsnapshot: "删除快照",
    vzstart: "启动容器",
    vzstop: "停止容器",
    vzshutdown: "关闭容器",
    vzmigrate: "迁移容器",
    vzdump: "备份",
    aptupdate: "更新软件源",
    pveupdate: "刷新订阅状态",
    startall: "全部启动",
    stopall: "全部停止",
    diskinit: "初始化磁盘",
    imgcopy: "复制镜像",
  };

  function taskLabel(type) {
    if (!type) return "任务";
    return TASK_LABEL[type] || type;
  }

  /* Disk health arrives as PASSED / OK / SMART Disabled / an error string. */
  function healthTone(h) {
    const s = String(h || "").toUpperCase();
    if (!s) return "idle";
    if (s === "PASSED" || s === "OK") return "ok";
    if (s.indexOf("DISABLED") >= 0 || s.indexOf("UNKNOWN") >= 0) return "idle";
    return "err";
  }

  function tempTone(t, warn, crit) {
    if (t === null || t === undefined) return "idle";
    const w = warn === undefined ? 55 : warn;
    const c = crit === undefined ? 65 : crit;
    if (t >= c) return "err";
    if (t >= w) return "warn";
    return "ok";
  }

  /* Storage content lists are comma separated type names; only the ones that
     matter for a monitor are worth a chip. */
  function contentChips(content) {
    const out = [];
    String(content || "").split(",").forEach(function (c) {
      c = c.trim();
      if (!c) return;
      out.push({ images: "磁盘镜像", rootdir: "容器", iso: "ISO", vztmpl: "模板",
                 backup: "备份", snippets: "片段", dump: "备份" }[c] || c);
    });
    return out.slice(0, 3);
  }

  window.FMT = {
    fmtBytes: fmtBytes, fmtPercent: fmtPercent, pct: pct,
    fmtDuration: fmtDuration, fmtDate: fmtDate, fmtTime: fmtTime, fmtAgo: fmtAgo,
    taskState: taskState, taskLabel: taskLabel,
    healthTone: healthTone, tempTone: tempTone, contentChips: contentChips,
  };
})();
