/* pvepanel - dependency-free canvas charts (RRD line, usage ring, sparkline) */
(function () {
  "use strict";

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  /* devicePixelRatio handling has to run on every draw: the canvas is sized by
     CSS, and a theme switch or a window resize changes that. */
  function fitCanvas(canvas, minH) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height || minH || 160));
    if (canvas.width !== Math.floor(w * dpr) || canvas.height !== Math.floor(h * dpr)) {
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx: ctx, w: w, h: h };
  }

  function hexA(color, a) {
    const m = /^#([0-9a-f]{6})$/i.exec(color);
    if (!m) return color;
    const n = parseInt(m[1], 16);
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }

  /* Pick a round axis ceiling so the gridlines land on readable numbers rather
     than on 37.42%. */
  function niceCeil(v) {
    if (!(v > 0)) return 1;
    const exp = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / exp;
    const steps = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    for (let i = 0; i < steps.length; i++) {
      if (n <= steps[i] + 1e-9) return steps[i] * exp;
    }
    return 10 * exp;
  }

  function fmtAxis(v, fmt) {
    if (fmt) return fmt(v);
    if (v >= 1000) return (v / 1000).toFixed(v >= 10000 ? 0 : 1) + "k";
    return Math.abs(v - Math.round(v)) < 0.05 ? String(Math.round(v)) : v.toFixed(1);
  }

  /* ------------------------------------------------------------------ *
   * Time axis
   *
   * RRD answers in fixed buckets per timeframe - one minute for "hour",
   * one hour for "day", one day for "week", and so on. Labelling only the
   * first/middle/last sample left the axis unreadable at a glance, so the
   * grid is generated from the calendar instead of from the sample count:
   * the tick positions land on round clock boundaries whatever the data
   * density happens to be.
   *
   * The requested spacing is the one a person reading the chart expects:
   *   hour  -> 10 minutes      day   -> 2 hours
   *   week  -> 1 day           month -> 5 days
   *   year  -> 2 months
   * ------------------------------------------------------------------ */
  const TF_TICK = {
    hour: { kind: "clock", step: 600, label: "hm" },
    day: { kind: "clock", step: 7200, label: "hm" },
    week: { kind: "day", step: 1, label: "md" },
    month: { kind: "day", step: 5, label: "md" },
    year: { kind: "month", step: 2, label: "ym" },
  };

  /* Side padding inside the plot, so the first and last tick are not glued to
     the grid edges. Scaled with the canvas but capped, so a wide desktop chart
     does not waste half its width. */
  const TICK_INSET = 22;

  /* How many ticks fit, given the plot width and the label's own resolution.

     Width alone is not enough: a 7-day window split into 10 ticks puts two
     ticks inside the same calendar day, so the axis printed "11-01" twice in a
     row. The labels have a resolution (a minute, a day, a month) and the tick
     spacing must never be finer than that. */
  const TF_UNIT = { hour: 60, day: 3600, week: 86400, month: 86400, year: 31 * 86400 };

  function tickCount(gw, tf, spanSec) {
    const per = tf === "year" ? 84 : 66;
    let wanted = Math.max(2, Math.min(10, Math.floor(gw / per)));
    const unit = TF_UNIT[tf] || 3600;
    if (spanSec > 0) {
      // One label per whole unit at most. A 7-day window can only show 7
      // different dates, so asking for 8 ticks prints one of them twice.
      const distinct = Math.max(2, Math.floor(spanSec / unit));
      wanted = Math.max(2, Math.min(wanted, distinct));
    }
    return wanted;
  }

  function pad2(n) { return String(n).padStart(2, "0"); }

  function tickLabel(ts, kind) {
    const d = new Date(ts * 1000);
    if (kind === "hm") return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
    if (kind === "md") return pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1);
  }

  /* Calendar-correct tick timestamps inside [t0, t1]. Stepping by a fixed
     number of seconds would drift across DST changes and across months of
     unequal length; walking a Date does not. */
  function tickTimes(tf, t0, t1) {
    const cfg = TF_TICK[tf] || TF_TICK.hour;
    const out = [];
    const push = function (d) {
      const ts = Math.floor(d.getTime() / 1000);
      if (ts >= t0 - 1 && ts <= t1 + 1) out.push(ts);
      return ts;
    };
    const cur = new Date(t0 * 1000);
    if (cfg.kind === "clock") {
      cur.setSeconds(0, 0);
      const stepMs = cfg.step * 1000;
      cur.setTime(Math.ceil(cur.getTime() / stepMs) * stepMs);
      for (let i = 0; i < 400 && cur.getTime() / 1000 <= t1; i++) {
        push(cur);
        cur.setTime(cur.getTime() + stepMs);
      }
    } else if (cfg.kind === "day") {
      cur.setHours(0, 0, 0, 0);
      if (cur.getTime() / 1000 < t0) cur.setDate(cur.getDate() + 1);
      for (let i = 0; i < 400 && cur.getTime() / 1000 <= t1; i++) {
        push(cur);
        cur.setDate(cur.getDate() + cfg.step);
      }
    } else {
      cur.setDate(1);
      cur.setHours(0, 0, 0, 0);
      // land on an even month so a one-year span shows Jan/Mar/... consistently
      if (cur.getMonth() % cfg.step) cur.setMonth(cur.getMonth() + (cfg.step - (cur.getMonth() % cfg.step)));
      if (cur.getTime() / 1000 < t0) cur.setMonth(cur.getMonth() + cfg.step);
      for (let i = 0; i < 100 && cur.getTime() / 1000 <= t1; i++) {
        push(cur);
        cur.setMonth(cur.getMonth() + cfg.step);
      }
    }
    return out;
  }

  /* ------------------------------------------------------------------ *
   * RRD line chart
   *
   * PVE hands back rrddata as [{time, ...series}]; every series has its own
   * unit, but a node's cpu / mem / net all live on different scales, so a
   * caller groups them into one chart per unit instead of normalising.
   * ------------------------------------------------------------------ */
  function LineChart(canvas) {
    this.canvas = canvas;
    this.points = [];
    this.series = [];
    this.fmt = null;
    this.tf = "hour";
    this.emptyLabel = "";
  }

  /* The canvas element is recreated whenever the view switches (templates sit
     behind v-if), while the chart object is cached for the app's lifetime.
     Without re-binding, draw() keeps painting the detached element. */
  LineChart.prototype.attach = function (canvas) {
    if (canvas && canvas !== this.canvas) this.canvas = canvas;
    this.draw();
  };

  LineChart.prototype.setData = function (points, series, fmt, tf, emptyLabel) {
    this.points = points || [];
    this.series = series || [];
    this.fmt = fmt || null;
    if (tf) this.tf = tf;
    this.emptyLabel = emptyLabel || "";
    this.draw();
  };

  LineChart.prototype.draw = function () {
    if (!this.canvas || !this.canvas.isConnected) return;
    const c = fitCanvas(this.canvas);
    const ctx = c.ctx, W = c.w, H = c.h;
    if (!W || !H) return;

    const padL = 46, padR = 12, padT = 12, padB = 24;
    const gw = Math.max(1, W - padL - padR);
    const gh = Math.max(1, H - padT - padB);

    const border = cssVar("--border-soft", "#efefef");
    const faint = cssVar("--faint", "#a3a3a3");
    const mono = cssVar("--mono", "monospace");

    const n = this.points.length;
    let peak = 0;
    const self = this;
    this.series.forEach(function (s) {
      self.points.forEach(function (p) {
        const v = Number(p[s.key]);
        if (isFinite(v) && v > peak) peak = v;
      });
    });
    const top = niceCeil(peak || 1);

    // The x axis is TIME, not sample index: ticks then land on clock
    // boundaries and a gap in the data reads as a gap instead of being
    // silently closed up.
    const t0 = n ? Number(this.points[0].time) : 0;
    const t1 = n ? Number(this.points[n - 1].time) : 0;
    const span = Math.max(1, t1 - t0);
    const xOf = function (t) { return padL + ((Number(t) - t0) / span) * gw; };

    ctx.font = "10px " + mono;
    ctx.textBaseline = "middle";

    for (let i = 0; i <= 4; i++) {
      const y = padT + (gh * i) / 4;
      ctx.strokeStyle = border;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, Math.round(y) + 0.5);
      ctx.lineTo(padL + gw, Math.round(y) + 0.5);
      ctx.stroke();
      ctx.fillStyle = faint;
      ctx.textAlign = "right";
      ctx.fillText(fmtAxis(top * (1 - i / 4), self.fmt), padL - 8, y);
    }

    /* Ticks are placed at evenly spaced fractions of the plot width, not at the
       first calendar boundary inside the window.

       Why: anchoring to the calendar put the first tick hard against the left
       edge and the last one against the right edge, so the axis read as a
       cropped photo - the leftmost label sat half outside the grid, and the
       spacing between labels was uneven because the first and last gaps were
       whatever the window happened to cut off. Distributing the ticks across
       the plot makes the gaps identical and leaves equal breathing room at
       both ends.

       The timestamp behind each tick is still a real moment in the window
       (interpolated across t0..t1), so the labels stay truthful; only their
       positions are regularised. */
    if (n > 1) {
      const cfg = TF_TICK[this.tf] || TF_TICK.hour;
      const wanted = tickCount(gw, this.tf, t1 - t0);
      if (wanted > 1) {
        const inner = gw - 2 * TICK_INSET;
        const step = inner / (wanted - 1);
        let lastRight = -1e9;
        let lastLabel = "";
        for (let i = 0; i < wanted; i++) {
          const x = padL + TICK_INSET + i * step;
          const frac = (x - padL) / gw;
          const t = t0 + frac * (t1 - t0);
          const label = tickLabel(t, cfg.label);
          const tw = ctx.measureText(label).width;
          // Labels that would touch each other are dropped; the gridlines stay,
          // so the rhythm of the axis survives even when a label does not fit.
          // A repeat of the previous label is dropped too - the resolution cap
          // above makes that unlikely, but printing "11-01" twice reads as a bug
          // even when the maths is right.
          const showLabel = x - tw / 2 >= lastRight + 10 && label !== lastLabel;
          if (showLabel) { lastRight = x + tw / 2; lastLabel = label; }
          ctx.strokeStyle = border;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(Math.round(x) + 0.5, padT);
          ctx.lineTo(Math.round(x) + 0.5, padT + gh);
          ctx.stroke();
          if (showLabel) {
            ctx.fillStyle = faint;
            // every label is centred on its own gridline - that alignment is
            // the thing that makes an axis readable
            ctx.textAlign = "center";
            ctx.fillText(label, x, H - 9);
          }
        }
      }
    }

    if (n < 2) {
      ctx.fillStyle = faint;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.font = "12px " + cssVar("--sans", "sans-serif");
      ctx.fillText(this.emptyLabel || "", padL + gw / 2, padT + gh / 2);
      return;
    }

    this.series.forEach(function (s) {
      const color = s.color;
      const xy = [];
      self.points.forEach(function (p) {
        const v = Number(p[s.key]);
        if (!isFinite(v)) return;
        xy.push([xOf(p.time), padT + gh - Math.min(1, v / top) * gh]);
      });
      if (xy.length < 2) return;

      ctx.beginPath();
      ctx.moveTo(xy[0][0], xy[0][1]);
      for (let i = 1; i < xy.length; i++) {
        const p0 = xy[i - 1], p1 = xy[i];
        const cx = (p0[0] + p1[0]) / 2;
        ctx.bezierCurveTo(cx, p0[1], cx, p1[1], p1[0], p1[1]);
      }
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.6;
      ctx.lineJoin = "round";
      ctx.stroke();

      ctx.lineTo(xy[xy.length - 1][0], padT + gh);
      ctx.lineTo(xy[0][0], padT + gh);
      ctx.closePath();
      const grad = ctx.createLinearGradient(0, padT, 0, padT + gh);
      grad.addColorStop(0, hexA(color, 0.26));
      grad.addColorStop(1, hexA(color, 0.01));
      ctx.fillStyle = grad;
      ctx.fill();
    });
  };

  /* ------------------------------------------------------------------ *
   * Usage ring - one value, 0..1. The number sits in the middle.
   * ------------------------------------------------------------------ */
  function drawRing(canvas, value, label, tone) {
    if (!canvas || !canvas.isConnected) return;
    const c = fitCanvas(canvas);
    const ctx = c.ctx, W = c.w, H = c.h;
    const cx = W / 2, cy = H / 2;
    const rOut = Math.max(12, Math.min(W, H) / 2 - 3);
    const width = Math.max(6, rOut * 0.22);
    const rMid = rOut - width / 2;

    ctx.beginPath();
    ctx.arc(cx, cy, rMid, 0, Math.PI * 2);
    ctx.strokeStyle = cssVar("--muted", "#f5f5f5");
    ctx.lineWidth = width;
    ctx.stroke();

    const v = Math.max(0, Math.min(1, Number(value) || 0));
    if (v > 0) {
      const color = tone === "err" ? cssVar("--danger", "#e7000b")
        : tone === "warn" ? cssVar("--warn", "#f59e0b")
        : cssVar("--accent-bar", "#2563eb");
      ctx.beginPath();
      ctx.arc(cx, cy, rMid, -Math.PI / 2, -Math.PI / 2 + v * Math.PI * 2);
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.lineCap = "round";
      ctx.stroke();
    }

    ctx.fillStyle = cssVar("--text", "#0a0a0a");
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = "600 " + Math.max(13, Math.round(rOut / 2.6)) + "px " + cssVar("--sans", "sans-serif");
    ctx.fillText(Math.round(v * 100) + "%", cx, cy - (label ? 5 : 0));
    if (label) {
      ctx.fillStyle = cssVar("--faint", "#a3a3a3");
      ctx.font = "10px " + cssVar("--sans", "sans-serif");
      ctx.fillText(label, cx, cy + Math.max(9, Math.round(rOut / 4)));
    }
  }

  window.CHART = {
    LineChart: LineChart, drawRing: drawRing, cssVar: cssVar, hexA: hexA,
    tickTimes: tickTimes, tickLabel: tickLabel, TF_TICK: TF_TICK,
  };
})();