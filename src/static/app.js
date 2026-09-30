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
    // Clamp here, the one place this renders - a browser's own upload
    // "progress" event isn't guaranteed to stop exactly at loaded===total
    // (seen reading past 100%), and this is the single choke point every
    // caller (upload, burn/rip progress, SSE) goes through either way.
    const shown = percent >= 0 ? Math.min(100, Math.max(0, Math.round(percent))) : -1;
    panel.hidden = !active && shown < 0;
    $("progress-phase").textContent = (phase || "READY").toUpperCase();
    $("progress-value").textContent = shown >= 0 ? `${shown}%` : "...";
    fill.style.width = shown >= 0 ? `${shown}%` : "34%";
    fill.classList.toggle("indeterminate", shown < 0);
  }

  async function pollStatus() {
    try {
      const response = await fetch("/progress", { cache: "no-store" });
      if (!response.ok) throw new Error("status");
      const progress = await response.json();
      setConnection(true);
      setLiveStatus(progress.status);
      setProgress(progress.status, Number(progress.progress), progress.active);
      applyRemoteState(progress);
    } catch (_) {
      setConnection(false);
      setLiveStatus("OFFLINE");
      setProgress("OFFLINE", -1, false);
    }
  }

  // --- On-screen remote: the exact same text commands the ESP32 sends -----
  // POSTed to /remote/button, which feeds a VirtualSerial standing in for a
  // real appliance. Only usable when no hardware remote is attached.
  const remoteKey = "discstation-remote-visible";

  function setRemoteVisible(visible, persist = false) {
    $("remote-panel").hidden = !visible;
    $("brand-link").classList.toggle("remote-active", visible);
    if (persist) localStorage.setItem(remoteKey, visible ? "1" : "0");
  }

  function toggleRemotePanel() {
    setRemoteVisible($("remote-panel").hidden, true);
  }

  function applyRemoteState(progress) {
    applyFirmwareState(progress);
    const hardware = progress.appliance === "hardware";
    if (hardware && !$("remote-panel").hidden) setRemoteVisible(false, true);
    // Software mode has no other control surface at all - don't make it
    // opt-in-via-logo-click to discover the only way to actually use the
    // appliance. Only forces it open (never closes it back on the user).
    if (!hardware && $("remote-panel").hidden) setRemoteVisible(true, true);
    $("remote-note").textContent = hardware
      ? "A physical remote is attached — on-screen controls are disabled."
      : "No physical remote detected — control DiscStation from here.";
    $("remote-controls").querySelectorAll("button, input").forEach((el) => { el.disabled = hardware; });
    $("remote-transport").hidden = !progress.playing;
    const ejectBtn = $("remote-eject-btn");
    const open = !!progress.tray_open;
    ejectBtn.textContent = open ? "CLOSE TRAY" : "EJECT";
    ejectBtn.dataset.cmd = open ? "CONFIRM" : "EJECT";
    applyBlueprintState(progress, hardware, open);
  }

  // ---- Schematic remote (SVG blueprint, drawn from remote-blueprint.json) ---
  // Same shape data the mobile app renders via react-native-svg, so the two
  // stay visually identical - see the plan's "shared layout" note. Additive:
  // the plain button grid above still works, this is just another skin on
  // top of the exact same /remote/button protocol.
  const SVG_NS = "http://www.w3.org/2000/svg";
  const blueprintViewKey = "discstation-remote-view";
  let bpOledStatusEl = null;
  let bpVuBarEls = [];
  let bpEjectRect = null;

  function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const key in attrs) el.setAttribute(key, attrs[key]);
    return el;
  }

  function setSchematicView(schematic, persist = false) {
    $("remote-blueprint").hidden = !schematic;
    $("remote-controls").hidden = schematic;
    $("remote-view-toggle").textContent = schematic ? "BUTTON VIEW" : "SCHEMATIC VIEW";
    if (persist) localStorage.setItem(blueprintViewKey, schematic ? "1" : "0");
  }

  async function loadBlueprint() {
    let data;
    try {
      data = await fetch("/static/remote-blueprint.json").then((r) => r.json());
    } catch (_) {
      return; // no schematic data - the plain button grid still works fine
    }
    renderBlueprint(data);
    $("remote-view-toggle").hidden = false;
    $("remote-view-toggle").addEventListener("click", () => {
      setSchematicView($("remote-blueprint").hidden, true);
    });
    setSchematicView(localStorage.getItem(blueprintViewKey) === "1");
  }

  function renderBlueprint(data) {
    const { board, header, buttons, knob, oled } = data;
    const svg = svgEl("svg", { viewBox: data.viewBox });

    svg.appendChild(svgEl("rect", { class: "bp-board", x: board.x, y: board.y, width: board.w, height: board.h, rx: board.rx }));

    const headerText = svgEl("text", { class: "bp-header-text", x: header.x, y: header.y, "text-anchor": "middle", "font-size": 20 });
    headerText.textContent = header.text;
    svg.appendChild(headerText);
    svg.appendChild(svgEl("line", { class: "bp-rule", x1: board.x + 16, y1: header.ruleY, x2: board.x + board.w - 16, y2: header.ruleY }));

    buttons.forEach((b) => {
      const rect = svgEl("rect", { class: "bp-btn", x: b.x, y: b.y, width: b.w, height: b.h, rx: b.rx, "data-cmd": b.cmd });
      const glyph = svgEl("text", { class: "bp-btn-glyph", x: b.x + b.w / 2, y: b.y + b.h / 2 + 9, "text-anchor": "middle" });
      glyph.textContent = b.glyph;
      const label = svgEl("text", { class: "bp-btn-label", x: b.x + b.w / 2, y: b.y + b.h + 14, "text-anchor": "middle" });
      label.textContent = b.label;
      svg.appendChild(rect);
      svg.appendChild(glyph);
      svg.appendChild(label);
      if (b.id === "eject") bpEjectRect = rect;
    });

    // Volume knob: a static ring, a draggable disc, and an indicator mark -
    // pointer-down + drag mapped to an angle, throttled the same way the old
    // linear slider debounced POT: (see wireKnob below).
    svg.appendChild(svgEl("circle", { class: "bp-knob-ring", cx: knob.cx, cy: knob.cy, r: knob.r + 6 }));
    const knobCircle = svgEl("circle", { class: "bp-knob", cx: knob.cx, cy: knob.cy, r: knob.r });
    svg.appendChild(knobCircle);
    const knobMark = svgEl("line", { class: "bp-knob-mark" });
    svg.appendChild(knobMark);
    const knobLabel = svgEl("text", { class: "bp-knob-label", x: knob.cx, y: knob.cy + knob.r + 20 });
    knobLabel.textContent = knob.label;
    svg.appendChild(knobLabel);

    svg.appendChild(svgEl("rect", { class: "bp-oled", x: oled.x, y: oled.y, width: oled.w, height: oled.h, rx: oled.rx }));
    const oledLabel = svgEl("text", { class: "bp-oled-label", x: oled.x + oled.w / 2, y: oled.y - 6 });
    oledLabel.textContent = oled.label;
    svg.appendChild(oledLabel);
    bpOledStatusEl = svgEl("text", { class: "bp-oled-status", x: oled.x + oled.w / 2, y: oled.y + 20 });
    svg.appendChild(bpOledStatusEl);

    // 16 VU bars, matching the OLED's own VU_BARS - empty until the first
    // "vu" SSE frame (see renderVuBars), same numbers the physical OLED shows.
    const barCount = 16, barGap = 3;
    const barW = (oled.w - 16 - barGap * (barCount - 1)) / barCount;
    bpVuBarEls = [];
    for (let i = 0; i < barCount; i++) {
      const baseY = oled.y + oled.h - 8; // bars grow upward from here
      const bar = svgEl("rect", { class: "bp-vu-bar", x: oled.x + 8 + i * (barW + barGap), y: baseY, width: barW, height: 0 });
      bar.dataset.baseY = baseY;
      svg.appendChild(bar);
      bpVuBarEls.push(bar);
    }

    svg.addEventListener("click", (event) => {
      const target = event.target.closest("[data-cmd]");
      if (target && !target.classList.contains("disabled")) sendRemoteCmd(target.dataset.cmd);
    });
    wireKnob(knobCircle, knobMark, knob);

    const container = $("remote-blueprint");
    container.innerHTML = "";
    container.appendChild(svg);
  }

  function wireKnob(knobCircle, knobMark, knob) {
    let dragging = false;
    let volumeTimer;
    function angleFor(clientX, clientY) {
      const svg = knobCircle.ownerSVGElement;
      const rect = svg.getBoundingClientRect();
      // Uniform scale: the page CSS keeps width:100%/height:auto, so the
      // rendered box always matches the viewBox's own aspect ratio.
      const scale = rect.width / svg.viewBox.baseVal.width;
      const cx = rect.left + knob.cx * scale;
      const cy = rect.top + knob.cy * scale;
      return Math.atan2(clientY - cy, clientX - cx);
    }
    function setFromAngle(angle) {
      // Map the knob's usable arc (-225deg..+45deg, a 270deg sweep starting
      // bottom-left) to 0-100, same range a physical pot covers.
      let deg = (angle * 180) / Math.PI;
      deg = ((deg - 135 + 360) % 360); // 0 at the sweep's start
      const pct = Math.max(0, Math.min(100, Math.round((deg / 270) * 100)));
      const markAngle = ((135 + (pct / 100) * 270) * Math.PI) / 180;
      knobMark.setAttribute("x1", knob.cx + Math.cos(markAngle) * (knob.r * 0.3));
      knobMark.setAttribute("y1", knob.cy + Math.sin(markAngle) * (knob.r * 0.3));
      knobMark.setAttribute("x2", knob.cx + Math.cos(markAngle) * (knob.r * 0.85));
      knobMark.setAttribute("y2", knob.cy + Math.sin(markAngle) * (knob.r * 0.85));
      clearTimeout(volumeTimer);
      volumeTimer = setTimeout(() => sendRemoteCmd(`POT:${pct}`), 150);
    }
    setFromAngle((135 * Math.PI) / 180); // draw the mark at rest before any input
    knobCircle.addEventListener("pointerdown", (event) => {
      dragging = true;
      knobCircle.setPointerCapture(event.pointerId);
      setFromAngle(angleFor(event.clientX, event.clientY));
    });
    knobCircle.addEventListener("pointermove", (event) => {
      if (dragging) setFromAngle(angleFor(event.clientX, event.clientY));
    });
    knobCircle.addEventListener("pointerup", () => { dragging = false; });
    knobCircle.addEventListener("pointercancel", () => { dragging = false; });
  }

  function applyBlueprintState(progress, hardware, trayOpen) {
    $("remote-blueprint").querySelectorAll(".bp-btn").forEach((el) => {
      el.classList.toggle("disabled", hardware);
    });
    if (bpEjectRect) bpEjectRect.dataset.cmd = trayOpen ? "CONFIRM" : "EJECT";
    if (bpOledStatusEl) bpOledStatusEl.textContent = (progress.status || "READY").toUpperCase().slice(0, 20);
  }

  function renderVuBars(levels) {
    if (!bpVuBarEls.length || !levels) return;
    const maxH = 56; // matches oled.h - top/bottom margin in remote-blueprint.json
    levels.forEach((level, i) => {
      const bar = bpVuBarEls[i];
      if (!bar) return;
      const h = Math.max(0, Math.min(maxH, (level / 63) * maxH));
      bar.setAttribute("height", h);
      bar.setAttribute("y", Number(bar.dataset.baseY) - h);
    });
  }

  // Firmware row: outside the remote panel on purpose - a hardware remote hides
  // that panel, and hardware is exactly when there is a remote to update.
  function applyFirmwareState(progress) {
    const state = progress.remote_update;
    const msg = progress.remote_update_msg;
    const text = $("fw-update-text");
    const btn = $("fw-update-btn");
    const updating = progress.active && /^UPDATING REMOTE/.test(progress.status || "");
    let line = "";
    if (state === "available") line = `Remote firmware ${progress.remote_fw} — ${progress.remote_fw_latest} is available.`;
    else if (state === "unknown") line = "This remote's firmware can't update itself yet — flash it once with a USB cable, then future updates are one click.";
    if (msg) line += ` Update failed (${msg}).`;
    text.textContent = line;
    btn.hidden = state !== "available";
    btn.disabled = !!updating;
    $("fw-update").hidden = !line;
  }

  $("fw-update-btn").addEventListener("click", async () => {
    $("fw-update-btn").disabled = true;
    try {
      const r = await fetch("/remote/update", { method: "POST" });
      if (!r.ok) $("fw-update-text").textContent = await r.text();
    } catch (_) { /* SSE/poll shows the real state */ }
  });

  let lastCmdKey = "", lastCmdAt = 0;
  async function sendRemoteCmd(cmd) {
    // Guard against a duplicate dispatch of the identical command landing
    // within the same instant (e.g. a click/pointer pair both bubbling to
    // the same handler) - a real user's next distinct press is unaffected,
    // this only catches an exact repeat inside a tiny window.
    const now = Date.now();
    if (cmd === lastCmdKey && now - lastCmdAt < 250) return;
    lastCmdKey = cmd;
    lastCmdAt = now;
    try {
      await fetch("/remote/button", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ cmd })
      });
    } catch (_) { /* next poll/SSE tick reflects reality */ }
  }

  $("brand-link").addEventListener("click", (event) => {
    event.preventDefault();
    toggleRemotePanel();
  });
  setRemoteVisible(localStorage.getItem(remoteKey) === "1");

  $("remote-controls").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-cmd]");
    if (!button || button.disabled) return;
    const cmd = button.dataset.cmd;
    // One-click burn: the server queues START right behind SELECT:BURN* in the
    // same request now (was two separate POSTs from here, which raced against
    // the server clearing stale input the instant it read SELECT and could
    // silently drop the START - see _handle_remote_button in discstation.py).
    await sendRemoteCmd(cmd);
  });
  let volumeTimer;
  $("remote-volume").addEventListener("input", (event) => {
    clearTimeout(volumeTimer);
    const value = event.target.value;
    volumeTimer = setTimeout(() => sendRemoteCmd(`POT:${value}`), 150);
  });

  // Mode-select buttons this server-computed list governs - CANCEL/EJECT are
  // controls, not modes, and stay available regardless (same as hardware).
  const MENU_MODES = ["BURN", "BURN DATA", "BURN AUDIO", "RIP", "PLAY"];

  function applyMenuItems(items) {
    if (!items) return;                    // unknown (fetch error, etc.) - leave as-is
    const allowed = new Set(items);
    MENU_MODES.forEach((mode) => {
      const btn = document.querySelector(`#remote-controls [data-cmd="SELECT:${mode}"]`);
      if (btn) btn.hidden = !allowed.has(mode);
    });
  }

  async function loadDiscInfo() {
    try {
      const response = await fetch("/disc-info", { cache: "no-store" });
      const info = await response.json();
      renderDiscStatus(info);
      applyMenuItems(info.menu_items);
      if (info.busy) return;               // burn/rip in progress — keep current
      state.discBytes = Number(info.capacity_bytes || 0);
      state.discType = info.type || "none";
      renderSelection();
    } catch (_) {
      state.discBytes = 0;
      state.discType = "none";
      renderDiscStatus(null);
    }
  }

  function renderDiscStatus(info) {
    const el = $("remote-disc-status");
    if (!el) return;
    if (!info) { el.textContent = "DISC: UNKNOWN"; return; }
    if (info.busy) { el.textContent = "DISC: BUSY"; return; }
    if (!info.disc_present) { el.textContent = "DISC: NONE"; return; }
    const kind = (info.type || info.kind || "unknown").toUpperCase();
    const label = info.label ? ` "${info.label}"` : "";
    const size = info.capacity_gb ? ` // ${info.capacity_gb}GB` : "";
    el.textContent = `DISC: ${kind}${label}${size}`;
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
  loadBlueprint();

  function startEventStream() {
    if (typeof EventSource === "undefined") { setInterval(pollStatus, 2000); return; }
    let es;
    try { es = new EventSource("/events"); }
    catch (_) { setInterval(pollStatus, 2000); return; }
    es.addEventListener("message", (ev) => {
      let d;
      try { d = JSON.parse(ev.data); } catch (_) { return; }
      if (d.type === "disc-changed") { loadDiscInfo(); return; }
      if (d.type === "vu") { renderVuBars(d.levels); return; }
      setConnection(true);
      setLiveStatus(d.status);
      setProgress(d.status, Number(d.progress), d.active);
      applyRemoteState(d);
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

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js?v=8").catch(() => {});
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    const button = document.createElement("button");
    button.className = "outline-button install-button";
    button.textContent = "INSTALL APP";
    $("install-slot").appendChild(button);
    button.addEventListener("click", async () => { event.prompt(); await event.userChoice; button.remove(); });
  });
})();
