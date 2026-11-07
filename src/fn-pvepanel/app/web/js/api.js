/* pvepanel - thin wrapper over the local panel API.
 *
 * Every call is same-origin and relative. That is not a style preference: the
 * desktop entry opens this page inside an iframe mounted at
 * /cgi/ThirdParty/fn-pvepanel/index.cgi/, so an absolute path would escape the
 * mount point and land on the NAS itself.
 */
(function () {
  "use strict";

  function req(path, opts) {
    opts = opts || {};
    const init = {
      method: opts.method || "GET",
      headers: { "Accept": "application/json" },
      cache: "no-store",
    };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (r) {
      return r.json().catch(function () {
        throw new Error("服务返回了非 JSON 内容（HTTP " + r.status + "）");
      }).then(function (doc) {
        if (!r.ok) throw new Error(doc.error || ("HTTP " + r.status));
        return doc;
      });
    });
  }

  function post(path, body) {
    return req(path, { method: "POST", body: body || {} });
  }

  /* Binary download that has to survive the WebView: the anchor's download
   * attribute is ignored by the fnOS phone app, so the blob goes through
   * createObjectURL. application/octet-stream (not json) is what makes the
   * Android WebView treat the response as a file instead of rendering it. */
  function download(path, filename) {
    return fetch(path, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.blob();
    }).then(function (blob) {
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      setTimeout(function () {
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      }, 0);
    });
  }

  window.API = {
    bootstrap: function (force, server, tf) {
      const q = [];
      if (force) q.push("force=1");
      if (server) q.push("server=" + encodeURIComponent(server));
      if (tf) q.push("tf=" + encodeURIComponent(tf));
      return req("./api/bootstrap" + (q.length ? "?" + q.join("&") : ""));
    },
    snapshot: function (force, tf) {
      return req("./api/snapshot" + (force ? "?force=1" : ""));
    },
    snapshotUrl: function (url) { return req(url); },
    settings: function () { return req("./api/settings"); },
    saveSettings: function (patch) { return post("./api/settings", patch); },
    saveServer: function (rec) { return post("./api/server", rec); },
    deleteServer: function (id) { return post("./api/server/delete", { id: id }); },
    setActive: function (id) { return post("./api/active", { id: id }); },
    testServer: function (payload) { return post("./api/server/test", payload); },
    guestAction: function (payload) { return post("./api/guest/action", payload); },
    notify: function () { return req("./api/notify"); },
    saveChannel: function (ch) { return post("./api/notify/channel", ch); },
    deleteChannel: function (id) { return post("./api/notify/channel", { id: id, _delete: true }); },
    testChannel: function (ch) { return post("./api/notify/test", ch); },

    checkUpdate: function (force) {
      return force ? post("./api/update/check", { force: true })
                   : req("./api/update/check");
    },
    installUpdate: function () { return post("./api/update/install", {}); },
    updateStatus: function () { return req("./api/update/status"); },

    storage: function () { return req("./api/storage"); },
    storageCleanup: function (target, keep) {
      return post("./api/storage/cleanup", { target: target, keep: keep });
    },

    backupStatus: function () { return req("./api/backup/status"); },
    backupExport: function () { return download("./api/backup/export", "pvepanel-backup.json"); },
    backupToFolder: function () { return post("./api/backup/to-folder", {}); },
    backupRun: function () { return post("./api/backup/run", {}); },
    backupRestore: function (name) { return post("./api/backup/restore", { name: name }); },
    backupDelete: function (name) { return post("./api/backup/delete", { name: name }); },
    backupImport: function (text) {
      return fetch("./api/backup/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: text,
      }).then(function (r) {
        return r.json().catch(function () { throw new Error("HTTP " + r.status); })
          .then(function (doc) {
            if (!r.ok || doc.ok === false) throw new Error(doc.error || ("HTTP " + r.status));
            return doc;
          });
      });
    },

    diagnose: function (server) {
      return req("./api/diagnose" + (server ? "?server=" + encodeURIComponent(server) : ""));
    },
    log: function (lines) { return req("./api/log?lines=" + (lines || 200)); },
    refresh: function (server, tf) {
      const q = [];
      if (server) q.push("server=" + encodeURIComponent(server));
      if (tf) q.push("tf=" + encodeURIComponent(tf));
      return post("./api/refresh" + (q.length ? "?" + q.join("&") : ""), {});
    },
  };
})();
