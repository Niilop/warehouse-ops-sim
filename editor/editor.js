"use strict";

const API_BASE = "http://localhost:8000";

const CT = { EMPTY: 0, RACK: 1, AISLE: 2, PACK: 3, DOCK: 4 };
const CELL_CLASS = { 0: "cell-empty", 1: "cell-rack", 2: "cell-aisle", 3: "cell-pack", 4: "cell-dock" };

// Canvas cell colors (sim viewer)
const CANVAS_COLORS   = { 0: "#2a2a2a", 1: "#b06010", 2: "#181818", 3: "#276027", 4: "#8a6500" };
const AGENT_IDLE      = ["#1a6fb5", "#a04000", "#1a7a5a", "#6a2090"];
const AGENT_CARRY     = ["#4a9fd4", "#f07020", "#30c890", "#b060e0"];

// ─────────────────────────────────────────────────────────────────
// GridEditor — drawable editor table
// ─────────────────────────────────────────────────────────────────
class GridEditor {
  constructor(tableEl) {
    this.tableEl = tableEl;
    this.rows = 0; this.cols = 0;
    this.cells = [];
    this.brush = CT.RACK;
    this.painting = false;
    this.packPos = null;
    this.dockPos = null;
    document.addEventListener("mouseup", () => { this.painting = false; });
  }

  build(rows, cols) {
    this.rows = rows; this.cols = cols;
    this.cells = Array.from({ length: rows }, () => new Array(cols).fill(CT.AISLE));
    this.packPos = null;
    this.dockPos = null;
    this._render();
  }

  loadFromDict(dict) {
    this.rows = dict.rows; this.cols = dict.cols;
    this.cells = dict.grid.map(row => [...row]);
    this.packPos = dict.pack_station_pos ? [...dict.pack_station_pos] : null;
    this.dockPos = dict.dock_pos ? [...dict.dock_pos] : null;
    this._render();
  }

  toDict() {
    const d = {
      rows: this.rows, cols: this.cols,
      grid: this.cells.map(row => [...row]),
      pack_station_pos: this.packPos ? [...this.packPos] : [0, 0],
    };
    if (this.dockPos) d.dock_pos = [...this.dockPos];
    return d;
  }

  setBrush(type) { this.brush = type; }

  setCellType(row, col, type) {
    if (row < 0 || row >= this.rows || col < 0 || col >= this.cols) return;
    if (type === CT.PACK) {
      if (this.packPos) {
        const [pr, pc] = this.packPos;
        this.cells[pr][pc] = CT.AISLE;
        this._updateTd(pr, pc);
      }
      this.packPos = [row, col];
    } else if (this.packPos && this.packPos[0] === row && this.packPos[1] === col) {
      this.packPos = null;
    }
    if (type === CT.DOCK) {
      if (this.dockPos) {
        const [dr, dc] = this.dockPos;
        this.cells[dr][dc] = CT.AISLE;
        this._updateTd(dr, dc);
      }
      this.dockPos = [row, col];
    } else if (this.dockPos && this.dockPos[0] === row && this.dockPos[1] === col) {
      this.dockPos = null;
    }
    this.cells[row][col] = type;
    this._updateTd(row, col);
  }

  validateLocal() {
    const errors = [];
    if (!this.packPos) errors.push("Place exactly one Pack Station.");
    if (!this.cells.flat().some(c => c === CT.RACK)) errors.push("Add at least one Rack cell.");
    return errors.length ? errors : null;
  }

  _render() {
    this.tableEl.innerHTML = "";
    for (let r = 0; r < this.rows; r++) {
      const tr = document.createElement("tr");
      for (let c = 0; c < this.cols; c++) {
        const td = document.createElement("td");
        td.dataset.row = r; td.dataset.col = c;
        td.className = CELL_CLASS[this.cells[r][c]];
        td.addEventListener("mousedown", (e) => { e.preventDefault(); this.painting = true; this.setCellType(r, c, this.brush); });
        td.addEventListener("mouseenter", () => { if (this.painting) this.setCellType(r, c, this.brush); });
        tr.appendChild(td);
      }
      this.tableEl.appendChild(tr);
    }
  }

  _updateTd(row, col) {
    const td = this.tableEl.rows[row]?.cells[col];
    if (td) td.className = CELL_CLASS[this.cells[row][col]];
  }
}

// ─────────────────────────────────────────────────────────────────
// SimulationViewer — canvas-based with zoom + auto-fit
// ─────────────────────────────────────────────────────────────────
class SimulationViewer {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.dict = null;
    this.agents = [];
    this._zoom = 1.0;
    this._heatmapData = null;  // normalized "r,c" -> 0–1 intensity

    // Wheel zoom — zoom toward cursor
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
      this._zoom = Math.max(0.1, Math.min(12, this._zoom * factor));
      this._draw();
    }, { passive: false });

    // Resize observer keeps canvas pixel-perfect
    const ro = new ResizeObserver(() => { this._syncSize(); this._draw(); });
    ro.observe(canvas.parentElement);
    this._syncSize();
  }

  // ── Public API ──────────────────────────────────────

  build(dict, resetZoom = true) {
    this.dict = dict;
    this.agents = [];
    if (resetZoom) this._zoom = 0.78;
    this._draw();
  }

  applyTick(frame) {
    this.agents = frame.agents ||
      [{ ...frame.agent, id: "A1", active_batch: frame.active_batch }];
    this._draw();
    this._updateHUD(frame);
  }

  resetZoom() { this._zoom = 0.78; this._draw(); }
  zoomBy(f)   { this._zoom = Math.max(0.1, Math.min(12, this._zoom * f)); this._draw(); }

  setHeatmap(rawData) {
    if (!rawData || !Object.keys(rawData).length) { this._heatmapData = null; this._draw(); return; }
    const maxVal = Math.max(...Object.values(rawData), 1e-9);
    this._heatmapData = {};
    for (const [k, v] of Object.entries(rawData)) this._heatmapData[k] = v / maxVal;
    this._draw();
  }

  clearHeatmap() { this._heatmapData = null; this._draw(); }

  // ── Internal ────────────────────────────────────────

  _syncSize() {
    const dpr = window.devicePixelRatio || 1;
    const el  = this.canvas.parentElement;
    const w   = el.clientWidth;
    const h   = el.clientHeight;
    this.canvas.width  = w * dpr;
    this.canvas.height = h * dpr;
    this.canvas.style.width  = w + "px";
    this.canvas.style.height = h + "px";
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  _logical() {
    const dpr = window.devicePixelRatio || 1;
    return { w: this.canvas.width / dpr, h: this.canvas.height / dpr };
  }

  _layout() {
    const { w, h } = this._logical();
    const padX = Math.max(48, w * 0.08);
    const padY = Math.max(48, h * 0.08);
    const base = Math.min(
      (w - padX * 2) / this.dict.cols,
      (h - padY * 2) / this.dict.rows
    );
    const cs = base * this._zoom;
    const ox = (w - this.dict.cols * cs) / 2;
    const oy = (h - this.dict.rows * cs) / 2;
    return { cs, ox, oy };
  }

  _draw() {
    const ctx = this.ctx;
    const { w, h } = this._logical();

    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, w, h);

    if (!this.dict) return;

    const { cs, ox, oy } = this._layout();
    const { rows, cols, grid } = this.dict;
    const gap = cs > 3 ? 1 : 0;

    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        ctx.fillStyle = CANVAS_COLORS[grid[r][c]] || "#000";
        ctx.fillRect(ox + c * cs + gap, oy + r * cs + gap, cs - gap, cs - gap);
      }
    }

    // Heatmap overlay — drawn over grid cells, under agents
    if (this._heatmapData) {
      for (const [key, intensity] of Object.entries(this._heatmapData)) {
        const [hr, hc] = key.split(",").map(Number);
        if (hr < 0 || hr >= rows || hc < 0 || hc >= cols) continue;
        const rc = Math.round(intensity * 255);
        const gc = Math.round((1 - intensity) * 80);
        ctx.fillStyle = `rgba(${rc},${gc},20,0.55)`;
        ctx.fillRect(ox + hc * cs + gap, oy + hr * cs + gap, cs - gap, cs - gap);
      }
    }

    // Agents — slightly inset square
    const pad = Math.max(1, cs * 0.12);
    for (let i = 0; i < this.agents.length; i++) {
      const a = this.agents[i];
      ctx.fillStyle = a.carrying.length ? AGENT_CARRY[i % 4] : AGENT_IDLE[i % 4];
      ctx.fillRect(
        ox + a.col * cs + pad,
        oy + a.row * cs + pad,
        cs - pad * 2,
        cs - pad * 2
      );
    }
  }

  _updateHUD(frame) {
    const tickEl = document.getElementById("hud-tick");
    if (this._streaming) {
      const day     = Math.floor(frame.tick / this._dayLength) + 1;
      const dayTick = frame.tick % this._dayLength;
      tickEl.textContent = `Day ${day} · ${dayTick}`;
    } else {
      tickEl.textContent = `tick ${frame.tick}`;
    }

    const container = document.getElementById("hud-agents");
    const agents = this.agents;

    while (container.children.length < agents.length) container.appendChild(document.createElement("div"));
    while (container.children.length > agents.length) container.removeChild(container.lastChild);

    for (let i = 0; i < agents.length; i++) {
      const a    = agents[i];
      const row  = container.children[i];
      row.className = "hud-agent-row";
      const state = a.state.replace(/_/g, " ");
      const carry = a.carrying.length ? ` — ${a.carrying.length} item${a.carrying.length > 1 ? "s" : ""}` : "";
      const dot   = row.children[0] || (() => { const s = document.createElement("span"); s.className = "hud-dot"; row.appendChild(s); return s; })();
      const label = row.children[1] || (() => { const s = document.createElement("span"); row.appendChild(s); return s; })();
      dot.className = "hud-dot";
      dot.style.background = AGENT_IDLE[i % 4];
      dot.style.flexShrink = "0";
      label.textContent = `${a.id} ${state}${carry}`;
      label.style.overflow = "hidden";
      label.style.textOverflow = "ellipsis";
    }
  }
}

// ─────────────────────────────────────────────────────────────────
// SimulationClient — WebSocket lifecycle
// ─────────────────────────────────────────────────────────────────
class SimulationClient {
  constructor(viewer, metricsTbody, metricsFooter, summaryBox, errorBanner, orderList, dashboard) {
    this.viewer        = viewer;
    this.metricsTbody  = metricsTbody;
    this.metricsFooter = metricsFooter;
    this.summaryBox    = summaryBox;
    this.errorBanner   = errorBanner;
    this.orderList     = orderList;
    this.dashboard     = dashboard;
    this.ws            = null;
    this.orderRows     = new Map();
    this._activeBatchKey = "";
    this._streaming    = false;
    this._dayLength    = 480;
  }

  sendSpeedUpdate(ms) {
    if (this.ws?.readyState === WebSocket.OPEN)
      this.ws.send(JSON.stringify({ type: "set_speed", tick_delay_ms: ms }));
  }

  sendStepsUpdate(steps) {
    if (this.ws?.readyState === WebSocket.OPEN)
      this.ws.send(JSON.stringify({ type: "set_steps", steps_per_frame: steps }));
  }

  stop() {
    if (this.ws?.readyState === WebSocket.OPEN)
      this.ws.send(JSON.stringify({ type: "stop" }));
    this.disconnect();
  }

  connect(config) {
    this._streaming = false;
    this._dayLength = config.day_length || 480;
    this.errorBanner.style.display = "none";
    this.summaryBox.style.display  = "none";
    document.getElementById("hud-summary").style.display = "none";
    if (this.dashboard) this.dashboard.reset();

    const wsUrl = API_BASE.replace(/^http/, "ws") + "/ws/simulate";
    this.ws = new WebSocket(wsUrl);
    this.ws.onopen    = () => this.ws.send(JSON.stringify(config));
    this.ws.onmessage = (e) => this._onMessage(JSON.parse(e.data));
    this.ws.onerror   = () => this._showError("WebSocket connection error.");
    this.ws.onclose   = () => {
      const btn = document.getElementById("btn-run");
      btn.dataset.running = "0";
      btn.textContent = "▶ Run Simulation";
      const bp = document.getElementById("btn-pause");
      bp.style.display = "none";
      bp.dataset.paused = "0";
      bp.textContent = "⏸ Pause";
    };
  }

  disconnect() { if (this.ws) { this.ws.close(); this.ws = null; } }

  _onMessage(frame) {
    switch (frame.type) {
      case "orders_ready":
        this._onOrdersReady(frame);
        if (this.dashboard) this.dashboard.onOrdersReady(frame);
        break;
      case "tick":
        this.viewer.applyTick(frame);
        this._updateActiveOrders(frame.agents || [], frame.waiting_order_ids || []);
        if (this.dashboard) this.dashboard.onTick(frame);
        break;
      case "order_complete":
        this._appendMetricsRow(frame);
        this._markOrderDone(frame.order_id);
        break;
      case "complete":
        this._onComplete(frame);
        if (this.dashboard) this.dashboard.onComplete(frame);
        break;
      case "error":
        this._showError(frame.message);
        break;
    }
  }

  _makeOrderRow(orderId, batchId, statusChar = "·", nItems = null) {
    const row    = document.createElement("div");
    row.className = "order-row";
    const status = document.createElement("span");
    status.className = "order-status";
    status.textContent = statusChar;
    const id = document.createElement("span");
    id.textContent = orderId;
    const items = document.createElement("span");
    items.style.color = "#333";
    items.textContent = nItems != null ? nItems + "×" : "";
    const batch = document.createElement("span");
    batch.className = "order-batch-tag";
    if (batchId && batchId !== orderId) batch.textContent = batchId;
    row.append(status, id, items, batch);
    return row;
  }

  _onOrdersReady(frame) {
    this.orderList.innerHTML = "";
    this.orderRows.clear();
    this._activeBatchKey = "";
    this._streaming = !!frame.streaming;

    if (this._streaming) {
      const ph = document.createElement("div");
      ph.id = "order-list-placeholder";
      ph.style.cssText = "color:#555;font-size:11px;padding:4px 2px;";
      ph.textContent = "Waiting for orders…";
      this.orderList.appendChild(ph);
      return;
    }

    for (const o of frame.orders) {
      const row = this._makeOrderRow(o.order_id, o.batch_id, "·", o.n_items);
      this.orderList.appendChild(row);
      this.orderRows.set(o.order_id, { el: row, batchId: o.batch_id });
    }
  }

  _updateActiveOrders(agents, waitingOrderIds = []) {
    const activeBatches = new Set(agents.map(a => a.active_batch).filter(Boolean));
    const waitingSet    = new Set(waitingOrderIds);
    const key = [...activeBatches].sort().join(",") + "|" + [...waitingSet].sort().join(",");
    if (key === this._activeBatchKey) return;
    this._activeBatchKey = key;

    // Streaming: insert a live row the first time a batch appears.
    if (this._streaming) {
      const ph = document.getElementById("order-list-placeholder");
      for (const batchId of activeBatches) {
        if (!this.orderRows.has(batchId)) {
          if (ph) ph.remove();
          const row = this._makeOrderRow(batchId, batchId, "●");
          row.classList.add("active");
          this.orderList.appendChild(row);
          this.orderRows.set(batchId, { el: row, batchId });
        }
      }
    }

    for (const [orderId, info] of this.orderRows) {
      if (info.el.classList.contains("done")) continue;
      const inActive  = activeBatches.has(info.batchId);
      const inWaiting = waitingSet.has(orderId);

      if (inActive) {
        info.el.classList.remove("waiting");
        info.el.classList.add("active");
        info.el.querySelector(".order-status").textContent = "●";
        const list = info.el.parentElement;
        if (list) {
          const bot = info.el.offsetTop + info.el.offsetHeight;
          if (info.el.offsetTop < list.scrollTop) list.scrollTop = info.el.offsetTop;
          else if (bot > list.scrollTop + list.clientHeight) list.scrollTop = bot - list.clientHeight;
        }
      } else if (inWaiting) {
        info.el.classList.remove("active");
        info.el.classList.add("waiting");
        info.el.querySelector(".order-status").textContent = "⏸";
      } else {
        info.el.classList.remove("active", "waiting");
        info.el.querySelector(".order-status").textContent = "·";
      }
    }
  }

  _markOrderDone(orderId) {
    const info = this.orderRows.get(orderId);
    if (!info) return;
    info.el.classList.remove("active");
    info.el.classList.add("done");
    info.el.querySelector(".order-status").textContent = "✓";
    if (this._streaming) this._pruneStreamingRows();
  }

  _pruneStreamingRows(max = 25) {
    const done = [...this.orderRows.entries()].filter(([, i]) => i.el.classList.contains("done"));
    for (let k = 0; k < done.length - max; k++) {
      const [id, info] = done[k];
      info.el.remove();
      this.orderRows.delete(id);
    }
  }

  _appendMetricsRow(frame) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${frame.order_id}</td>
      <td class="text-right">${frame.items_picked}</td>
      <td class="text-right">${frame.distance_traveled}</td>
      <td class="text-right">${frame.ticks_taken}</td>`;
    this.metricsTbody.appendChild(tr);
    const scroll = this.metricsTbody.closest(".metrics-scroll");
    if (scroll) scroll.scrollTop = scroll.scrollHeight;
  }

  _onComplete(frame) {
    const s = frame.summary;
    document.getElementById("footer-items").textContent = s.total_items_picked;
    document.getElementById("footer-dist").textContent  = s.total_distance;
    document.getElementById("footer-ticks").textContent = frame.total_ticks;
    this.metricsFooter.style.display = "";

    const summaryText =
      `✓ ${frame.total_orders} orders · ${frame.total_ticks} ticks\n` +
      `avg ${s.avg_ticks_per_order} ticks/order · dist ${s.total_distance}`;

    this.summaryBox.style.display = "block";
    this.summaryBox.textContent   = summaryText;

    const hudSummary = document.getElementById("hud-summary");
    hudSummary.style.display = "block";
    hudSummary.textContent   = summaryText;
  }

  _showError(message) {
    this.errorBanner.textContent = "Error: " + message;
    this.errorBanner.style.display = "block";
  }
}

// ─────────────────────────────────────────────────────────────────
// Dashboard — heatmap toggle, rolling throughput, agent utilization
// ─────────────────────────────────────────────────────────────────
class Dashboard {
  constructor(viewer) {
    this.viewer        = viewer;
    this._mode         = "off";   // "off" | "visits" | "slotting"
    this._visitData    = null;    // "r,c" -> count  (arrives in complete frame)
    this._slottingData = null;    // "r,c" -> pick_rate (arrives in orders_ready frame)
  }

  onOrdersReady(frame) {
    this._slottingData = frame.slot_pick_rates || null;
    if (this._mode === "slotting") this._applyHeatmap();
  }

  onTick(frame) {
    const el = document.getElementById("dash-throughput");
    if (el && frame.lines_per_hour_rolling != null)
      el.textContent = `${frame.lines_per_hour_rolling} lines/hr`;
    this._updateAgentUtil(frame.agents || []);
  }

  onComplete(frame) {
    this._visitData = frame.heatmap || null;
    if (this._mode === "visits") this._applyHeatmap();
  }

  setMode(mode) {
    this._mode = mode;
    if (mode === "off") this.viewer.clearHeatmap();
    else                this._applyHeatmap();
  }

  reset() {
    this._visitData = null;
    this.viewer.clearHeatmap();
    const el = document.getElementById("dash-throughput");
    if (el) el.textContent = "— lines/hr";
    const util = document.getElementById("dash-util");
    if (util) util.innerHTML = "";
  }

  _applyHeatmap() {
    const data = this._mode === "slotting" ? this._slottingData : this._visitData;
    this.viewer.setHeatmap(data || null);
  }

  _updateAgentUtil(agents) {
    const el = document.getElementById("dash-util");
    if (!el) return;
    while (el.children.length < agents.length) el.appendChild(document.createElement("div"));
    while (el.children.length > agents.length) el.removeChild(el.lastChild);
    for (let i = 0; i < agents.length; i++) {
      const a   = agents[i];
      const row = el.children[i];
      row.className = "util-row";
      const pct = a.util_pct ?? 0;
      row.innerHTML =
        `<span class="util-label">${a.id}</span>` +
        `<div class="util-bar-bg"><div class="util-bar-fill" style="width:${pct}%;background:${AGENT_IDLE[i % 4]}"></div></div>` +
        `<span class="util-pct">${pct}%</span>`;
    }
  }
}

// ─────────────────────────────────────────────────────────────────
// Module wiring
// ─────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  const editor    = new GridEditor(document.getElementById("editor-grid"));
  const viewer    = new SimulationViewer(document.getElementById("sim-canvas"));
  const dashboard = new Dashboard(viewer);
  let client = null;

  // ── Heatmap mode toggle ──────────────────────────
  document.querySelectorAll(".heatmap-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".heatmap-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      dashboard.setMode(btn.dataset.mode);
    });
  });

  // Load default layout on startup
  fetch(API_BASE + "/api/default-layout")
    .then(r => r.json())
    .then(dict => { editor.loadFromDict(dict); viewer.build(dict); });

  // ── Sidebar collapse / expand ────────────────
  const sidebar = document.getElementById("sidebar");
  const btnExpand = document.getElementById("btn-expand-sidebar");

  document.getElementById("btn-collapse-sidebar").addEventListener("click", () => {
    sidebar.classList.add("collapsed");
    btnExpand.classList.add("visible");
  });
  btnExpand.addEventListener("click", () => {
    sidebar.classList.remove("collapsed");
    btnExpand.classList.remove("visible");
  });

  // ── Mode toggle ──────────────────────────────
  const modeButtons = ["mode-default", "mode-quad", "mode-custom"];
  function setActiveMode(activeId) {
    modeButtons.forEach(id => document.getElementById(id).classList.toggle("active", id === activeId));
    document.getElementById("custom-controls").style.display = activeId === "mode-custom" ? "" : "none";
  }

  document.getElementById("mode-default").addEventListener("click", () => {
    fetch(API_BASE + "/api/default-layout").then(r => r.json())
      .then(dict => { editor.loadFromDict(dict); viewer.build(dict); });
    setActiveMode("mode-default");
  });
  document.getElementById("mode-quad").addEventListener("click", () => {
    fetch(API_BASE + "/api/quad-layout").then(r => r.json())
      .then(dict => { editor.loadFromDict(dict); viewer.build(dict); });
    setActiveMode("mode-quad");
  });
  document.getElementById("mode-custom").addEventListener("click", () => setActiveMode("mode-custom"));

  // ── Custom size ──────────────────────────────
  document.getElementById("btn-apply-size").addEventListener("click", () => {
    const rows = parseInt(document.getElementById("input-rows").value);
    const cols = parseInt(document.getElementById("input-cols").value);
    editor.build(rows, cols);
    viewer.build({ rows, cols, grid: Array.from({ length: rows }, () => new Array(cols).fill(2)) });
  });

  // ── Brushes ──────────────────────────────────
  const brushMap = { "brush-rack": CT.RACK, "brush-aisle": CT.AISLE, "brush-pack": CT.PACK, "brush-dock": CT.DOCK, "brush-empty": CT.EMPTY };
  Object.entries(brushMap).forEach(([id, type]) => {
    document.getElementById(id).addEventListener("click", () => {
      editor.setBrush(type);
      document.querySelectorAll(".brush-btn").forEach(b => b.classList.remove("active"));
      document.getElementById(id).classList.add("active");
    });
  });

  // ── Save ─────────────────────────────────────
  document.getElementById("btn-save").addEventListener("click", () => {
    const dict = editor.toDict();
    const blob = new Blob([JSON.stringify(dict, null, 2)], { type: "application/json" });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href = url; a.download = "warehouse-layout.json"; a.click();
    URL.revokeObjectURL(url);
  });

  // ── Load ─────────────────────────────────────
  document.getElementById("file-load-input").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      try {
        const dict = JSON.parse(ev.target.result);
        editor.loadFromDict(dict);
        viewer.build(dict);
      } catch { alert("Invalid JSON file."); }
    };
    reader.readAsText(file);
    e.target.value = "";
  });

  // ── Collapsible sections ─────────────────────
  document.querySelectorAll(".section-title").forEach(title => {
    title.addEventListener("click", () => {
      const section = title.closest(".section");
      if (section.id === "custom-controls" || section.classList.contains("no-collapse")) return;
      section.classList.toggle("collapsed");
    });
  });

  // ── Batch strategy toggle ────────────────────
  document.getElementById("input-batch-strategy").addEventListener("change", (e) => {
    document.getElementById("batch-size-param").style.display = e.target.value === "none" ? "none" : "";
  });

  // ── Speed / steps sliders ────────────────────
  document.getElementById("speed-slider").addEventListener("input", (e) => {
    const ms = parseInt(e.target.value);
    document.getElementById("speed-label").textContent = ms === 0 ? "max" : ms + " ms";
    if (client) client.sendSpeedUpdate(ms);
  });
  document.getElementById("steps-slider").addEventListener("input", (e) => {
    const steps = parseInt(e.target.value);
    document.getElementById("steps-label").textContent = "×" + steps;
    if (client) client.sendStepsUpdate(steps);
  });

  // ── Zoom buttons ─────────────────────────────
  document.getElementById("btn-zoom-in").addEventListener("click",    () => viewer.zoomBy(1.3));
  document.getElementById("btn-zoom-out").addEventListener("click",   () => viewer.zoomBy(1 / 1.3));
  document.getElementById("btn-zoom-reset").addEventListener("click", () => viewer.resetZoom());

  // ── Pause / Resume ───────────────────────────
  const btnPause = document.getElementById("btn-pause");
  btnPause.addEventListener("click", () => {
    if (!client) return;
    const paused = btnPause.dataset.paused === "1";
    if (paused) {
      const ms = parseInt(document.getElementById("speed-slider").value);
      client.sendSpeedUpdate(ms);
      btnPause.dataset.paused = "0";
      btnPause.textContent = "⏸ Pause";
    } else {
      client.sendSpeedUpdate(999_999);
      btnPause.dataset.paused = "1";
      btnPause.textContent = "▶ Resume";
    }
  });

  // ── Run / Stop ───────────────────────────────
  document.getElementById("btn-run").addEventListener("click", async () => {
    const btn = document.getElementById("btn-run");
    if (btn.dataset.running === "1") {
      if (client) client.stop();
      btn.dataset.running = "0";
      btn.textContent = "▶ Run Simulation";
      btnPause.style.display = "none";
      btnPause.dataset.paused = "0";
      btnPause.textContent = "⏸ Pause";
      return;
    }

    const errorBanner = document.getElementById("error-banner");
    errorBanner.style.display = "none";

    const localErrors = editor.validateLocal();
    if (localErrors) {
      errorBanner.textContent = localErrors.join(" ");
      errorBanner.style.display = "block";
      return;
    }

    const dict = editor.toDict();
    const vRes = await fetch(API_BASE + "/api/validate-layout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(dict),
    });
    const validation = await vRes.json();
    if (!validation.valid) {
      errorBanner.textContent = validation.errors.join(" | ");
      errorBanner.style.display = "block";
      return;
    }

    document.getElementById("metrics-tbody").innerHTML = "";
    document.getElementById("metrics-footer").style.display = "none";
    document.getElementById("order-list").innerHTML = "";
    viewer.build(dict, false);
    btn.dataset.running = "1";
    btn.textContent = "■ Stop";
    btnPause.style.display = "inline-block";
    btnPause.dataset.paused = "0";
    btnPause.textContent = "⏸ Pause";

    if (client) client.disconnect();
    client = new SimulationClient(
      viewer,
      document.getElementById("metrics-tbody"),
      document.getElementById("metrics-footer"),
      document.getElementById("summary-box"),
      errorBanner,
      document.getElementById("order-list"),
      dashboard,
    );

    const dayLength = 480;
    client.connect({
      layout:             dict,
      n_orders:           parseInt(document.getElementById("input-orders").value),
      n_items:            parseInt(document.getElementById("input-items").value),
      items_per_order:    parseInt(document.getElementById("input-per-order").value),
      n_families:         parseInt(document.getElementById("input-families").value),
      demand_skew:        parseFloat(document.getElementById("input-skew").value),
      family_affinity:    parseFloat(document.getElementById("input-affinity").value),
      slot_strategy:      document.getElementById("input-slot-strategy").value,
      batch_strategy:     document.getElementById("input-batch-strategy").value,
      batch_size:         parseInt(document.getElementById("input-batch-size").value),
      n_agents:           parseInt(document.getElementById("input-agents").value),
      seed:               parseInt(document.getElementById("input-seed").value),
      tick_delay_ms:      parseInt(document.getElementById("speed-slider").value),
      steps_per_frame:    parseInt(document.getElementById("steps-slider").value),
      order_arrival_rate: parseFloat(document.getElementById("input-arrival-rate").value),
      restock_delay:      parseInt(document.getElementById("input-restock-delay").value),
      day_length:         dayLength,
    });
  });
});
