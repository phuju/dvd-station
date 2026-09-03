(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { entries: [], discBytes: 0, discType: "none" };
  const themeKey = "discstation-theme";
  const themeToggle = $("theme-toggle");

  function applyTheme(theme, persist = false) {
    document.documentElement.dataset.theme = theme;
    themeToggle.textContent = theme === "dark" ? "\u2600" : "\u263E";
    themeToggle.setAttribute("aria-label", theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
    if (persist) localStorage.setItem(themeKey, theme);
  }

  const savedTheme = localStorage.getItem(themeKey);
  const systemDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(savedTheme || (systemDark ? "dark" : "light"));
  themeToggle.addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
  });
  if (!savedTheme && window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (event) => applyTheme(event.matches ? "dark" : "light"));
  }

  const formatSize = (bytes) => {
    if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
    if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
    if (bytes >= 1e3) return `${Math.round(bytes / 1e3)} KB`;
    return `${bytes} B`;
  };

  const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[c]);

  const rootFor = (path) => path.includes("/") ? path.split("/")[0] : null;

  function setConnection(online) {
    const dot = $("connection-dot");
    const label = $("connection-label");
    dot.className = `connection-dot ${online ? "online" : "offline"}`;
    label.textContent = online ? "LINK: LIVE" : "LINK: OFFLINE";
  }

  function setLiveStatus(value) {
    const text = (value || "Idle").trim();
    $("live-status").textContent = text.toUpperCase();
  }

  function setProgress(phase, percent, active = true) {
    const panel = $("global-progress");
    const fill = $("progress-fill");
    panel.hidden = !active && percent < 0;
    $("progress-phase").textContent = (phase || "READY").toUpperCase();
    $("progress-value").textContent = percent >= 0 ? `${percent}%` : "...";
    fill.style.width = percent >= 0 ? `${Math.min(100, percent)}%` : "34%";
    fill.classList.toggle("indeterminate", percent < 0);
  }

  async function pollStatus() {
    try {
      const response = await fetch("/progress", { cache: "no-store" });
      if (!response.ok) throw new Error("status");
      const progress = await response.json();
      setConnection(true);
      setLiveStatus(progress.status);
      setProgress(progress.status, Number(progress.progress), progress.active);
    } catch (_) {
      setConnection(false);
      setLiveStatus("OFFLINE");
      setProgress("OFFLINE", -1, false);
    }
  }

  async function loadDiscInfo() {
    try {
      const response = await fetch("/disc-info", { cache: "no-store" });
      const info = await response.json();
      state.discBytes = Number(info.capacity_bytes || 0);
      state.discType = info.type || "none";
      renderSelection();
    } catch (_) {
      state.discBytes = 0;
      state.discType = "none";
    }
  }

  function renderSelection() {
    const list = $("selection-list");
    const total = state.entries.reduce((sum, entry) => sum + entry.file.size, 0);
    const roots = new Map();
    state.entries.forEach((entry, index) => {
      const root = rootFor(entry.path);
      if (!root) return;
      if (!roots.has(root)) roots.set(root, { indexes: [], bytes: 0 });
      const group = roots.get(root);
      group.indexes.push(index);
      group.bytes += entry.file.size;
    });

    if (!state.entries.length) {
      list.innerHTML = '<div class="empty-selection">NO MEDIA SELECTED</div>';
    } else {
      const grouped = new Set();
      const rows = [];
      roots.forEach((group, root) => {
        group.indexes.forEach((index) => grouped.add(index));
        rows.push(`<div class="selection-row"><span class="selection-name">[FOLDER] ${escapeHtml(root)}/</span><span class="selection-meta">${group.indexes.length} FILES // ${formatSize(group.bytes)}</span><button class="remove-selection" type="button" data-remove-group="${escapeHtml(root)}" aria-label="Remove folder">X</button></div>`);
      });
      state.entries.forEach((entry, index) => {
        if (!grouped.has(index)) rows.push(`<div class="selection-row"><span class="selection-name">${escapeHtml(entry.path)}</span><span class="selection-meta">${formatSize(entry.file.size)}</span><button class="remove-selection" type="button" data-remove-index="${index}" aria-label="Remove file">X</button></div>`);
      });
      list.innerHTML = rows.join("");
      list.querySelectorAll("[data-remove-index]").forEach((button) => {
        button.addEventListener("click", () => {
          state.entries.splice(Number(button.dataset.removeIndex), 1);
          renderSelection();
        });
      });
      list.querySelectorAll("[data-remove-group]").forEach((button) => {
        button.addEventListener("click", () => {
          const root = button.dataset.removeGroup;
          state.entries = state.entries.filter((entry) => rootFor(entry.path) !== root);
          renderSelection();
        });
      });
    }

    const count = state.entries.length;
    $("selection-summary").textContent = `${count} FILE${count === 1 ? "" : "S"} // ${roots.size} FOLDER${roots.size === 1 ? "" : "S"}`;
    $("upload-button").disabled = count === 0;
    const label = $("disc-label");
    if (!label.value && state.entries.length) label.value = rootFor(state.entries[0].path) || state.entries[0].file.name.replace(/\.[^.]+$/, "");

    const meter = $("disc-meter");
    if (state.discBytes && count) {
      const percent = Math.min(total / state.discBytes * 100, 100);
      meter.hidden = false;
      $("disc-type").textContent = `DISC: ${state.discType.toUpperCase()}`;
      $("disc-space").textContent = `${formatSize(total)} / ${formatSize(state.discBytes)}`;
      $("disc-fill").style.width = `${percent}%`;
    } else {
      meter.hidden = true;
    }
  }

  function addFiles(fileList) {
    Array.from(fileList).forEach((file) => {
      state.entries.push({ file, path: file.webkitRelativePath || file.name });
    });
    renderSelection();
  }

  function setMessage(text, ok) {
    const message = $("form-message");
    message.textContent = text;
    message.className = `form-message ${ok ? "ok" : "error"}`;
  }

  async function submitUrl(event) {
    event.preventDefault();
    const value = $("url-input").value.trim();
    if (!value) return;
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    button.textContent = "QUEUING //";
    try {
      const response = await fetch("/", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ url: value })
      });
      setMessage(await response.text(), response.ok);
    } catch (error) {
      setMessage(`Request failed: ${error.message}`, false);
    } finally {
      button.disabled = false;
      button.innerHTML = 'BURN TO DISC <span>//</span>';
    }
  }

  async function uploadSelection() {
    if (!state.entries.length) return;
    const button = $("upload-button");
    button.disabled = true;
    button.innerHTML = "UPLOADING //";
    setProgress("UPLOADING", 0, true);
    const label = $("disc-label").value.trim();
    try {
      if (label) {
        await fetch("/set-label", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ label })
        });
      }
      const form = new FormData();
      const paths = [];
      state.entries.forEach((entry) => {
        form.append("files", entry.file, entry.file.name);
        paths.push({ n: entry.file.name, p: entry.path });
      });
      form.append("_paths", JSON.stringify(paths));
      const result = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/");
        xhr.upload.addEventListener("progress", (event) => {
          if (event.lengthComputable) setProgress("UPLOADING", Math.round(event.loaded / event.total * 100), true);
        });
        xhr.onload = () => resolve({ ok: xhr.status >= 200 && xhr.status < 300, text: xhr.responseText });
        xhr.onerror = () => reject(new Error("network"));
        xhr.send(form);
      });
      setMessage(result.text, result.ok);
      if (result.ok) {
        state.entries = [];
        renderSelection();
        setProgress("UPLOAD READY", 100, false);
      }
    } catch (error) {
      setMessage(`Upload failed: ${error.message}`, false);
    } finally {
      button.disabled = state.entries.length === 0;
      button.innerHTML = 'UPLOAD TO DISC <span>//</span>';
    }
  }

  function setupTabs() {
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((item) => {
          const active = item === tab;
          item.classList.toggle("active", active);
          item.setAttribute("aria-selected", active ? "true" : "false");
        });
        document.querySelectorAll(".tab-panel").forEach((panel) => { panel.hidden = panel.id !== `tab-${tab.dataset.tab}`; panel.classList.toggle("active", !panel.hidden); });
      });
    });
  }

  $("file-picker").addEventListener("click", () => $("file-input").click());
  $("folder-picker").addEventListener("click", () => $("folder-input").click());
  $("file-input").addEventListener("change", (event) => { addFiles(event.target.files); event.target.value = ""; });
  $("folder-input").addEventListener("change", (event) => { addFiles(event.target.files); event.target.value = ""; });
  $("upload-button").addEventListener("click", uploadSelection);
  $("url-form").addEventListener("submit", submitUrl);
  const dropzone = $("dropzone");
  if (dropzone) {
    dropzone.addEventListener("dragover", (event) => { event.preventDefault(); event.currentTarget.classList.add("drag"); });
    dropzone.addEventListener("dragleave", (event) => event.currentTarget.classList.remove("drag"));
    dropzone.addEventListener("drop", (event) => { event.preventDefault(); event.currentTarget.classList.remove("drag"); addFiles(event.dataTransfer.files); });
  }
  setupTabs();
  loadDiscInfo();
  pollStatus();
  startEventStream();

  function startEventStream() {
    if (typeof EventSource === "undefined") { setInterval(pollStatus, 2000); return; }
    let es;
    try { es = new EventSource("/events"); }
    catch (_) { setInterval(pollStatus, 2000); return; }
    es.addEventListener("message", (ev) => {
      let d;
      try { d = JSON.parse(ev.data); } catch (_) { return; }
      if (d.type === "disc-changed") { loadDiscInfo(); return; }
      setConnection(true);
      setLiveStatus(d.status);
      setProgress(d.status, Number(d.progress), d.active);
    });
    es.addEventListener("open", () => { setConnection(true); loadDiscInfo(); });
    es.addEventListener("error", () => {
      // EventSource reconnects on its own; reflect the gap meanwhile.
      setConnection(false);
      setLiveStatus("OFFLINE");
      setProgress("OFFLINE", -1, false);
    });
    // Backstops: catch a zombie SSE connection, and refresh disc state slowly.
    setInterval(() => { if (!es || es.readyState !== 1) pollStatus(); }, 8000);
    setInterval(loadDiscInfo, 15000);
  }

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js?v=7").catch(() => {});
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    const button = document.createElement("button");
    button.className = "outline-button install-button";
    button.textContent = "INSTALL APP";
    $("install-slot").appendChild(button);
    button.addEventListener("click", async () => { event.prompt(); await event.userChoice; button.remove(); });
  });
})();
