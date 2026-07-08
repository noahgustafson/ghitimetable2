/* GHI-TIME offline capture module — the ONLY offline surface.
 *
 * Outbox lives in IndexedDB; the device is a transmission buffer, never an
 * authority. The client generates the UUID. Editing a not-yet-synced draft
 * mutates the outbox payload in place (still version 1 — server-side
 * versioning starts at first successful sync). Sync runs on page open, on
 * connectivity regain, and via the manual button. Sync results:
 *   accepted / duplicate -> remove from outbox (server has it)
 *   conflict / rejected  -> keep, marked, until dismissed (server wins)
 */
(function () {
  "use strict";
  var BOOT = window.GHITIME_BOOT || {};
  var WARN_AT = 20; // resolved question 4: eviction warning threshold

  // --- tiny IndexedDB helpers ------------------------------------------------
  var dbp = new Promise(function (resolve, reject) {
    var req = indexedDB.open("ghitime", 1);
    req.onupgradeneeded = function () {
      var db = req.result;
      if (!db.objectStoreNames.contains("outbox"))
        db.createObjectStore("outbox", { keyPath: "uuid" });
      if (!db.objectStoreNames.contains("meta"))
        db.createObjectStore("meta", { keyPath: "key" });
    };
    req.onsuccess = function () { resolve(req.result); };
    req.onerror = function () { reject(req.error); };
  });
  function tx(store, mode, fn) {
    return dbp.then(function (db) {
      return new Promise(function (resolve, reject) {
        var t = db.transaction(store, mode);
        var s = t.objectStore(store);
        var out = fn(s);
        t.oncomplete = function () { resolve(out && out.result !== undefined ? out.result : out); };
        t.onerror = function () { reject(t.error); };
      });
    });
  }
  function allOutbox() {
    return tx("outbox", "readonly", function (s) { return s.getAll(); });
  }
  function putOutbox(item) { return tx("outbox", "readwrite", function (s) { s.put(item); }); }
  function delOutbox(uuid) { return tx("outbox", "readwrite", function (s) { s.delete(uuid); }); }
  function putMeta(key, value) {
    return tx("meta", "readwrite", function (s) { s.put({ key: key, value: value }); });
  }
  function getMeta(key) {
    return tx("meta", "readonly", function (s) { return s.get(key); });
  }

  function deviceId() {
    var id = localStorage.getItem("ghitime_device");
    if (!id) {
      id = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2));
      localStorage.setItem("ghitime_device", id);
    }
    return id;
  }

  // --- job cache (with as-of timestamp) --------------------------------------
  function renderJobs(jobs, asOf) {
    var sel = document.getElementById("f-job");
    sel.innerHTML = "";
    jobs.forEach(function (j) {
      var o = document.createElement("option");
      o.value = j.id; o.textContent = j.code + " — " + j.name;
      sel.appendChild(o);
    });
    document.getElementById("jobs-asof").textContent = "job list as of " + asOf;
  }
  function loadCachedJobs() {
    return getMeta("jobs").then(function (m) {
      if (m && m.value) renderJobs(m.value.jobs, m.value.as_of + " (cached)");
      else if (BOOT.jobsFull) renderJobs(BOOT.jobsFull, BOOT.jobsAsOf + " (page)");
    });
  }
  function refreshJobs() {
    return fetch("/api/jobs", { headers: { "X-GHITIME": "1" } })
      .then(function (r) { if (!r.ok) throw 0; return r.json(); })
      .then(function (data) {
        putMeta("jobs", data);
        renderJobs(data.jobs, data.as_of);
      })
      .catch(function () { /* offline: cached list stays */ });
  }

  // --- outbox rendering -------------------------------------------------------
  function render() {
    allOutbox().then(function (items) {
      items.sort(function (a, b) { return a.device_created_at < b.device_created_at ? 1 : -1; });
      var pendingCount = items.filter(function (i) { return i.state === "pending"; }).length;
      document.getElementById("pending-count").textContent = pendingCount;
      var warn = document.getElementById("outbox-warning");
      warn.hidden = pendingCount < WARN_AT;
      document.getElementById("pending-big").textContent = pendingCount;

      var ul = document.getElementById("outbox-list");
      ul.innerHTML = "";
      items.forEach(function (i) {
        var li = document.createElement("li");
        var label = i.work_date + " " + i.start_time + "–" + i.end_time +
          " (break " + i.break_minutes + "m) ";
        var state = document.createElement("span");
        state.className = "sync-" + i.state;
        state.textContent = "[" + i.state + (i.reason ? ": " + i.reason : "") + "]";
        li.textContent = label;
        li.appendChild(state);
        if (i.state === "pending") {
          var edit = document.createElement("button");
          edit.textContent = "edit";
          edit.onclick = function () { loadIntoForm(i); };
          li.appendChild(document.createTextNode(" "));
          li.appendChild(edit);
        }
        if (i.state === "conflict" || i.state === "rejected") {
          var dis = document.createElement("button");
          dis.textContent = "dismiss (server copy wins)";
          dis.onclick = function () { delOutbox(i.uuid).then(render); };
          li.appendChild(document.createTextNode(" "));
          li.appendChild(dis);
        }
        ul.appendChild(li);
      });
    });
  }

  // --- form ---------------------------------------------------------------
  function loadIntoForm(i) {
    document.getElementById("edit-uuid").value = i.uuid;
    document.getElementById("f-date").value = i.work_date;
    document.getElementById("f-job").value = i.job_id;
    document.getElementById("f-start").value = i.start_time;
    document.getElementById("f-end").value = i.end_time;
    document.getElementById("f-break").value = i.break_minutes;
    document.getElementById("f-note").value = i.note || "";
    document.getElementById("f-cancel").hidden = false;
  }
  function clearForm() {
    document.getElementById("edit-uuid").value = "";
    document.getElementById("capture-form").reset();
    document.getElementById("f-date").value = BOOT.today;
    document.getElementById("f-cancel").hidden = true;
  }
  document.getElementById("f-cancel").onclick = clearForm;

  document.getElementById("capture-form").onsubmit = function (ev) {
    ev.preventDefault();
    var wd = document.getElementById("f-date").value;
    if (wd > BOOT.today) { alert("Future dates are blocked."); return; }
    var brk = document.getElementById("f-break").value;
    if (brk === "") { alert("Enter the break in minutes — 0 if none. It is never assumed."); return; }
    var uuid = document.getElementById("edit-uuid").value ||
      (crypto.randomUUID ? crypto.randomUUID() : null);
    if (!uuid) { alert("This browser cannot generate entry ids."); return; }
    var item = {
      uuid: uuid,
      version_no: 1,
      job_id: parseInt(document.getElementById("f-job").value, 10),
      work_date: wd,
      start_time: document.getElementById("f-start").value,
      end_time: document.getElementById("f-end").value,
      break_minutes: parseInt(brk, 10),
      note: document.getElementById("f-note").value || null,
      device_created_at: new Date().toISOString(),
      state: "pending",
      reason: null
    };
    putOutbox(item).then(function () { clearForm(); render(); syncNow(); });
  };

  // --- sync ---------------------------------------------------------------
  var syncing = false;
  function setNet() {
    document.getElementById("net-state").textContent =
      navigator.onLine ? "online" : "offline — entries wait in the outbox";
  }
  function syncNow() {
    if (syncing || !navigator.onLine) return Promise.resolve();
    syncing = true;
    return allOutbox().then(function (items) {
      var pending = items.filter(function (i) { return i.state === "pending"; });
      if (!pending.length) { syncing = false; return; }
      var payload = {
        device_id: deviceId(),
        client_info: navigator.userAgent.slice(0, 180),
        entries: pending.map(function (i) {
          return {
            uuid: i.uuid, version_no: 1, job_id: i.job_id, work_date: i.work_date,
            start_time: i.start_time, end_time: i.end_time,
            break_minutes: i.break_minutes, note: i.note,
            device_created_at: i.device_created_at
          };
        })
      };
      return fetch("/api/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-GHITIME": "1" },
        body: JSON.stringify(payload)
      }).then(function (r) {
        if (r.status === 401) throw new Error("login required — open GHI-TIME online once");
        if (!r.ok) throw new Error("sync failed (" + r.status + ")");
        return r.json();
      }).then(function (data) {
        var ops = data.results.map(function (res) {
          if (res.result === "accepted" || res.result === "duplicate")
            return delOutbox(res.uuid);
          return allOutbox().then(function (all) {
            var item = all.find(function (i) { return i.uuid === res.uuid; });
            if (item) {
              item.state = res.result;
              item.reason = res.reason || null;
              return putOutbox(item);
            }
          });
        });
        return Promise.all(ops);
      });
    }).catch(function (e) {
      console.warn("sync:", e.message);
    }).then(function () { syncing = false; render(); });
  }

  document.getElementById("sync-now").onclick = function () { refreshJobs(); syncNow(); };
  window.addEventListener("online", function () { setNet(); refreshJobs(); syncNow(); });
  window.addEventListener("offline", setNet);

  // boot: render cache, then refresh + sync if online (sync-on-open — iOS has
  // no background sync, so opening the app IS the sync trigger)
  setNet();
  loadCachedJobs().then(function () { render(); refreshJobs(); syncNow(); });
})();
