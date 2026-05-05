"use strict";

const API_BASE = "http://localhost:8000"; // prefix all fetch calls — swap for Tauri sidecar later

// CellType values (mirror warehouse/grid.py CellType enum)
const CT = { EMPTY: 0, RACK: 1, AISLE: 2, PACK: 3 };
const CELL_CLASS = { 0: "cell-empty", 1: "cell-rack", 2: "cell-aisle", 3: "cell-pack" };

// ─────────────────────────────────────────────────────────────────
// GridEditor — manages the drawable editor table
// ─────────────────────────────────────────────────────────────────
class GridEditor {
  constructor(tableEl) {
    this.tableEl = tableEl;
    this.rows = 0;
    this.cols = 0;
    this.cells = [];      // 2D array of CellType int
    this.brush = CT.RACK;
    this.painting = false;
    this.packPos = null;  // [row, col] of current PACK_STATION, enforces exactly one

    document.addEventListener("mouseup", () => { this.painting = false; });
  }

  build(rows, cols) {
    this.rows = rows;
    this.cols = cols;
    this.cells = Array.from({ length: rows }, () => new Array(cols).fill(CT.AISLE));
    this.packPos = null;
    this._render();
  }

  loadFromDict(dict) {
    this.rows = dict.rows;
    this.cols = dict.cols;
    this.cells = dict.grid.map(row => [...row]);
    this.packPos = dict.pack_station_pos ? [...dict.pack_station_pos] : null;
    this._render();
  }

  toDict() {
    return {
      rows: this.rows,
      cols: this.cols,
      grid: this.cells.map(row => [...row]),
      pack_station_pos: this.packPos ? [...this.packPos] : [0, 0],
    };
  }

  setBrush(type) {
    this.brush = type;
  }

  setCellType(row, col, type) {
    if (row < 0 || row >= this.rows || col < 0 || col >= this.cols) return;

    // Enforce single pack station
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

    this.cells[row][col] = type;
    this._updateTd(row, col);
  }

  validateLocal() {
    const errors = [];
    if (!this.packPos) errors.push("Place exactly one Pack Station on the grid.");
    const rackCount = this.cells.flat().filter(c => c === CT.RACK).length;
    if (rackCount === 0) errors.push("Add at least one Rack cell.");
    return errors.length ? errors : null;
  }

  _render() {
    this.tableEl.innerHTML = "";
    for (let r = 0; r < this.rows; r++) {
      const tr = document.createElement("tr");
      for (let c = 0; c < this.cols; c++) {
        const td = document.createElement("td");
        td.dataset.row = r;
        td.dataset.col = c;
        td.className = CELL_CLASS[this.cells[r][c]];
        td.addEventListener("mousedown", (e) => {
          e.preventDefault();
          this.painting = true;
          this.setCellType(r, c, this.brush);
        });
        td.addEventListener("mouseenter", () => {
          if (this.painting) this.setCellType(r, c, this.brush);
        });
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
// SimulationViewer — read-only grid with agent overlay
// ─────────────────────────────────────────────────────────────────
class SimulationViewer {
  constructor(tableEl) {
    this.tableEl = tableEl;
    this.rows = 0;
    this.cols = 0;
    this.agentRow = -1;
    this.agentCol = -1;
  }

  build(dict) {
    this.rows = dict.rows;
    this.cols = dict.cols;
    this.agentRow = -1;
    this.agentCol = -1;
    this.tableEl.innerHTML = "";
    for (let r = 0; r < this.rows; r++) {
      const tr = document.createElement("tr");
      for (let c = 0; c < this.cols; c++) {
        const td = document.createElement("td");
        td.className = CELL_CLASS[dict.grid[r][c]];
        tr.appendChild(td);
      }
      this.tableEl.appendChild(tr);
    }
  }

  applyTick(frame) {
    const { row, col, state, carrying } = frame.agent;

    // Clear previous agent cell
    if (this.agentRow >= 0) {
      const prev = this.tableEl.rows[this.agentRow]?.cells[this.agentCol];
      if (prev) {
        prev.classList.remove("cell-agent", "cell-agent-carry");
      }
    }

    // Set new agent cell
    const td = this.tableEl.rows[row]?.cells[col];
    if (td) {
      td.classList.add(carrying.length > 0 ? "cell-agent-carry" : "cell-agent");
    }
    this.agentRow = row;
    this.agentCol = col;

    // Update status bar
    document.getElementById("status-tick").textContent = frame.tick;
    document.getElementById("status-order").textContent = frame.active_order ?? "—";
    document.getElementById("status-state").textContent = state.replace(/_/g, " ");
    document.getElementById("status-carrying").textContent =
      carrying.length ? carrying.join(", ") : "nothing";
  }
}

// ─────────────────────────────────────────────────────────────────
// SimulationClient — WebSocket lifecycle
// ─────────────────────────────────────────────────────────────────
class SimulationClient {
  constructor(viewer, metricsTbody, metricsFooter, summaryBox, errorBanner, orderList) {
    this.viewer = viewer;
    this.metricsTbody = metricsTbody;
    this.metricsFooter = metricsFooter;
    this.summaryBox = summaryBox;
    this.errorBanner = errorBanner;
    this.orderList = orderList;
    this.ws = null;
    this.orderRows = new Map();  // order_id -> { el, batchId }
    this.activeBatchId = null;
  }

  sendSpeedUpdate(ms) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "set_speed", tick_delay_ms: ms }));
    }
  }

  stop() {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "stop" }));
    }
    this.disconnect();
  }

  connect(config) {
    this.errorBanner.style.display = "none";
    this.summaryBox.style.display = "none";

    const wsUrl = API_BASE.replace(/^http/, "ws") + "/ws/simulate";
    this.ws = new WebSocket(wsUrl);

    this.ws.onopen = () => {
      this.ws.send(JSON.stringify(config));
    };

    this.ws.onmessage = (event) => {
      const frame = JSON.parse(event.data);
      switch (frame.type) {
        case "orders_ready":   this._onOrdersReady(frame); break;
        case "tick":           this.viewer.applyTick(frame); this._updateActiveOrders(frame.active_batch); break;
        case "order_complete": this._appendMetricsRow(frame); this._markOrderDone(frame.order_id); break;
        case "complete":       this._onComplete(frame); break;
        case "error":          this._showError(frame.message); break;
      }
    };

    this.ws.onerror = () => this._showError("WebSocket connection error.");
    this.ws.onclose = () => {
      const btn = document.getElementById("btn-run");
      btn.dataset.running = "0";
      btn.textContent = "▶ Run Simulation";
    };
  }

  disconnect() {
    if (this.ws) { this.ws.close(); this.ws = null; }
  }

  _onOrdersReady(frame) {
    this.orderList.innerHTML = "";
    this.orderRows.clear();
    this.activeBatchId = null;

    for (const o of frame.orders) {
      const row = document.createElement("div");
      row.className = "order-row";

      const status = document.createElement("span");
      status.className = "order-status";
      status.textContent = "·";

      const id = document.createElement("span");
      id.textContent = o.order_id;

      const items = document.createElement("span");
      items.style.color = "#444";
      items.textContent = o.n_items + "×";

      const batch = document.createElement("span");
      batch.className = "order-batch-tag";
      if (o.batch_id && o.batch_id !== o.order_id) batch.textContent = o.batch_id;

      row.append(status, id, items, batch);
      this.orderList.appendChild(row);
      this.orderRows.set(o.order_id, { el: row, batchId: o.batch_id });
    }
  }

  _updateActiveOrders(batchId) {
    if (batchId === this.activeBatchId) return;

    for (const [, info] of this.orderRows) {
      if (info.el.classList.contains("active")) {
        info.el.classList.remove("active");
        info.el.querySelector(".order-status").textContent = "·";
      }
    }
    if (batchId) {
      for (const [, info] of this.orderRows) {
        if (info.batchId === batchId && !info.el.classList.contains("done")) {
          info.el.classList.add("active");
          info.el.querySelector(".order-status").textContent = "●";
          info.el.scrollIntoView({ block: "nearest" });
        }
      }
    }
    this.activeBatchId = batchId;
  }

  _markOrderDone(orderId) {
    const info = this.orderRows.get(orderId);
    if (!info) return;
    info.el.classList.remove("active");
    info.el.classList.add("done");
    info.el.querySelector(".order-status").textContent = "✓";
  }

  _appendMetricsRow(frame) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${frame.order_id}</td>
      <td class="text-right">${frame.items_picked}</td>
      <td class="text-right">${frame.distance_traveled}</td>
      <td class="text-right">${frame.ticks_taken}</td>
    `;
    this.metricsTbody.appendChild(tr);
    tr.scrollIntoView({ block: "nearest" });
  }

  _onComplete(frame) {
    const s = frame.summary;
    document.getElementById("footer-items").textContent = s.total_items_picked;
    document.getElementById("footer-dist").textContent = s.total_distance;
    document.getElementById("footer-ticks").textContent = frame.total_ticks;
    this.metricsFooter.style.display = "";

    this.summaryBox.style.display = "block";
    this.summaryBox.innerHTML =
      `✓ Done — ${frame.total_orders} orders completed in ${frame.total_ticks} ticks &nbsp;|&nbsp; ` +
      `avg ${s.avg_ticks_per_order} ticks/order &nbsp;|&nbsp; ` +
      `total distance ${s.total_distance}`;
  }

  _showError(message) {
    this.errorBanner.textContent = "Error: " + message;
    this.errorBanner.style.display = "block";
  }
}

// ─────────────────────────────────────────────────────────────────
// Module wiring
// ─────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  const editor = new GridEditor(document.getElementById("editor-grid"));
  const viewer = new SimulationViewer(document.getElementById("sim-grid"));
  let client = null;

  // Load default layout on startup
  fetch(API_BASE + "/api/default-layout")
    .then(r => r.json())
    .then(dict => {
      editor.loadFromDict(dict);
      viewer.build(dict);
    });

  // ── Mode toggle ──────────────────────────────
  const modeButtons = ["mode-default", "mode-quad", "mode-custom"];

  function setActiveMode(activeId) {
    modeButtons.forEach(id => document.getElementById(id).classList.toggle("active", id === activeId));
    document.getElementById("custom-controls").style.display = activeId === "mode-custom" ? "" : "none";
  }

  document.getElementById("mode-default").addEventListener("click", () => {
    fetch(API_BASE + "/api/default-layout")
      .then(r => r.json())
      .then(dict => { editor.loadFromDict(dict); viewer.build(dict); });
    setActiveMode("mode-default");
  });

  document.getElementById("mode-quad").addEventListener("click", () => {
    fetch(API_BASE + "/api/quad-layout")
      .then(r => r.json())
      .then(dict => { editor.loadFromDict(dict); viewer.build(dict); });
    setActiveMode("mode-quad");
  });

  document.getElementById("mode-custom").addEventListener("click", () => {
    setActiveMode("mode-custom");
  });

  // ── Custom size ──────────────────────────────
  document.getElementById("btn-apply-size").addEventListener("click", () => {
    const rows = parseInt(document.getElementById("input-rows").value);
    const cols = parseInt(document.getElementById("input-cols").value);
    editor.build(rows, cols);
    viewer.build({ rows, cols, grid: Array.from({ length: rows }, () => new Array(cols).fill(2)) });
  });

  // ── Brushes ──────────────────────────────────
  const brushMap = {
    "brush-rack":  CT.RACK,
    "brush-aisle": CT.AISLE,
    "brush-pack":  CT.PACK,
    "brush-empty": CT.EMPTY,
  };
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
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "warehouse-layout.json";
    a.click();
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
      } catch {
        alert("Invalid JSON file.");
      }
    };
    reader.readAsText(file);
    e.target.value = "";
  });

  // ── Batch strategy — show/hide batch size ────────────
  document.getElementById("input-batch-strategy").addEventListener("change", (e) => {
    document.getElementById("batch-size-param").style.display =
      e.target.value === "none" ? "none" : "";
  });

  // ── Speed slider — updates live if simulation is running ─────────
  document.getElementById("speed-slider").addEventListener("input", (e) => {
    document.getElementById("speed-label").textContent = e.target.value + " ms";
    if (client) client.sendSpeedUpdate(parseInt(e.target.value));
  });

  // ── Run / Stop button ─────────────────────────
  document.getElementById("btn-run").addEventListener("click", async () => {
    const btn = document.getElementById("btn-run");

    // If simulation is running, stop it
    if (btn.dataset.running === "1") {
      if (client) client.stop();
      btn.dataset.running = "0";
      btn.textContent = "▶ Run Simulation";
      btn.classList.remove("stopping");
      return;
    }

    const errorBanner = document.getElementById("error-banner");
    errorBanner.style.display = "none";

    // Local validation first
    const localErrors = editor.validateLocal();
    if (localErrors) {
      errorBanner.textContent = localErrors.join(" ");
      errorBanner.style.display = "block";
      return;
    }

    const dict = editor.toDict();

    // Server-side validation
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

    // Reset UI
    document.getElementById("metrics-tbody").innerHTML = "";
    document.getElementById("metrics-footer").style.display = "none";
    document.getElementById("order-list").innerHTML = "";
    viewer.build(dict);
    btn.dataset.running = "1";
    btn.textContent = "■ Stop";

    if (client) client.disconnect();
    client = new SimulationClient(
      viewer,
      document.getElementById("metrics-tbody"),
      document.getElementById("metrics-footer"),
      document.getElementById("summary-box"),
      errorBanner,
      document.getElementById("order-list"),
    );

    client.connect({
      layout: dict,
      n_orders: parseInt(document.getElementById("input-orders").value),
      n_items: parseInt(document.getElementById("input-items").value),
      items_per_order: parseInt(document.getElementById("input-per-order").value),
      n_families: parseInt(document.getElementById("input-families").value),
      demand_skew: parseFloat(document.getElementById("input-skew").value),
      family_affinity: parseFloat(document.getElementById("input-affinity").value),
      batch_strategy: document.getElementById("input-batch-strategy").value,
      batch_size: parseInt(document.getElementById("input-batch-size").value),
      seed: 42,
      tick_delay_ms: parseInt(document.getElementById("speed-slider").value),
    });
  });
});
