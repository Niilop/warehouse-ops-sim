from __future__ import annotations
import asyncio
import json
import logging
import pathlib

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from warehouse.grid import WarehouseGrid, CellType
from warehouse.inventory import Inventory
from warehouse.agent import PickAgent
from warehouse.simulation import Simulation
from warehouse.batcher import ZoneBatcher, GreedyTSPBatcher
from warehouse.data_gen import generate_catalog, generate_orders
from warehouse.optimizer import slot_distances, demand_placement, affinity_placement

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

EDITOR_DIR = pathlib.Path(__file__).parent / "editor"
app.mount("/static", StaticFiles(directory=str(EDITOR_DIR)), name="static")


@app.get("/")
async def serve_editor() -> FileResponse:
    return FileResponse(str(EDITOR_DIR / "index.html"))


@app.get("/api/default-layout")
async def get_default_layout() -> JSONResponse:
    return JSONResponse(WarehouseGrid.build_default().to_dict())


@app.get("/api/quad-layout")
async def get_quad_layout() -> JSONResponse:
    return JSONResponse(WarehouseGrid.build_quad().to_dict())


class LayoutPayload(BaseModel):
    rows: int
    cols: int
    grid: list[list[int]]
    pack_station_pos: list[int]


@app.post("/api/validate-layout")
async def validate_layout(payload: LayoutPayload) -> JSONResponse:
    errors: list[str] = []

    if payload.rows < 3 or payload.cols < 3:
        errors.append("Grid must be at least 3×3.")
    if len(payload.grid) != payload.rows:
        errors.append(f"Grid has {len(payload.grid)} rows, expected {payload.rows}.")
    else:
        for r, row in enumerate(payload.grid):
            if len(row) != payload.cols:
                errors.append(f"Row {r} has {len(row)} cols, expected {payload.cols}.")

    r, c = payload.pack_station_pos
    if not (0 <= r < payload.rows and 0 <= c < payload.cols):
        errors.append("pack_station_pos is out of bounds.")
    elif payload.grid[r][c] != int(CellType.PACK_STATION):
        errors.append("pack_station_pos cell must be PACK_STATION (3).")

    pack_count = sum(cell == int(CellType.PACK_STATION)
                     for row in payload.grid for cell in row)
    if pack_count != 1:
        errors.append(f"Exactly one PACK_STATION required, found {pack_count}.")

    rack_count = sum(cell == int(CellType.RACK)
                     for row in payload.grid for cell in row)
    if rack_count == 0:
        errors.append("Layout must contain at least one RACK cell.")

    if not errors:
        wg = WarehouseGrid.from_dict({
            "rows": payload.rows, "cols": payload.cols,
            "grid": payload.grid, "pack_station_pos": payload.pack_station_pos,
        })
        any_reachable = any(
            wg.get_rack_neighbors(rr, cc)
            for rr in range(wg.rows)
            for cc in range(wg.cols)
            if wg.grid[rr, cc] == CellType.RACK
        )
        if not any_reachable:
            errors.append("No RACK cell has a walkable neighbor — agent cannot pick any item.")

    return JSONResponse({"valid": len(errors) == 0, "errors": errors})


@app.websocket("/ws/simulate")
async def ws_simulate(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        raw = await websocket.receive_text()
        config = json.loads(raw)
        await _run_simulation(websocket, config)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logging.exception(e)
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass


async def _run_simulation(websocket: WebSocket, config: dict) -> None:
    layout_dict = config["layout"]
    n_orders = config.get("n_orders", 10)
    n_items = config.get("n_items", 30)
    items_per_order = config.get("items_per_order", 4)
    n_families = config.get("n_families", 4)
    demand_skew = config.get("demand_skew", 2.0)
    family_affinity = config.get("family_affinity", 0.7)
    seed = config.get("seed", 42)
    tick_delay_s = config.get("tick_delay_ms", 80) / 1000.0
    steps_per_frame = max(1, int(config.get("steps_per_frame", 4)))
    batch_strategy = config.get("batch_strategy", "none")
    batch_size = int(config.get("batch_size", 2))
    slot_strategy = config.get("slot_strategy", "spread")

    grid = WarehouseGrid.from_dict(layout_dict)
    inventory = Inventory(grid)

    items = generate_catalog(n_items=n_items, n_families=n_families, demand_skew=demand_skew, seed=seed)
    items = items[:len(inventory._slots)]  # clamp to available rack slots
    if not items:
        await websocket.send_text(json.dumps(
            {"type": "error", "message": "Layout has no reachable rack slots."}
        ))
        return

    # Generate orders before seeding so affinity placement can use co-occurrence data
    orders = generate_orders(
        items, n_orders=n_orders, items_per_order=items_per_order,
        family_affinity=family_affinity, seed=seed
    )

    if slot_strategy in ("demand", "affinity"):
        sorted_slot_order = [idx for idx, _ in slot_distances(grid, inventory)][:len(items)]
        if slot_strategy == "demand":
            ordered_items = demand_placement(items)
        else:
            ordered_items = affinity_placement(items, orders)
        inventory.seed(ordered_items, slot_order=sorted_slot_order)
    else:
        inventory.seed(items)

    n_agents = max(1, int(config.get("n_agents", 1)))
    agents = [PickAgent(agent_id=f"A{i+1}", start_pos=grid.pack_station_pos, grid=grid) for i in range(n_agents)]
    sim = Simulation(grid=grid, inventory=inventory, agents=agents, restock_delay=0)

    if batch_strategy == "zone":
        batches = ZoneBatcher(grid, inventory, max_batch_size=batch_size).batch(orders)
        for batch in batches:
            sim.enqueue_batch(batch)
        order_batch = {o.order_id: b.batch_id for b in batches for o in b.orders}
    elif batch_strategy == "tsp":
        batches = GreedyTSPBatcher(inventory, grid, max_batch_size=batch_size).batch(orders)
        for batch in batches:
            sim.enqueue_batch(batch)
        order_batch = {o.order_id: b.batch_id for b in batches for o in b.orders}
    else:
        for order in orders:
            sim.enqueue_order(order)
        order_batch = {o.order_id: o.order_id for o in orders}

    # Sort by batch_id so batched orders appear grouped in the UI
    orders_sorted = sorted(orders, key=lambda o: order_batch[o.order_id])
    await websocket.send_text(json.dumps({
        "type": "orders_ready",
        "orders": [
            {
                "order_id": o.order_id,
                "n_items": len(o.item_ids),
                "batch_id": order_batch[o.order_id],
            }
            for o in orders_sorted
        ],
        "slot_pick_rates": {
            f"{slot.rack_pos[0]},{slot.rack_pos[1]}": round(slot.item.pick_rate, 4)
            for slot in inventory._slots
            if slot.item is not None
        },
    }))

    prev_metrics_count = 0
    cell_visit_freq: dict[str, int] = {}
    agent_ticks_active: dict[str, int] = {a.agent_id: 0 for a in agents}
    _window_items = 0
    _window_start = 0
    _WINDOW = 100
    lines_per_hour_rolling = 0.0
    prev_total_items = 0

    while True:
        # Run steps_per_frame simulation ticks before sending one WebSocket frame.
        # This lets the simulation appear faster without increasing message rate.
        has_more = True
        for _ in range(steps_per_frame):
            has_more = sim.step()
            # Track utilization and heatmap inside the inner loop so speed multiplier
            # doesn't artificially cap the counters.
            for a in sim.agents:
                key = f"{a.pos[0]},{a.pos[1]}"
                cell_visit_freq[key] = cell_visit_freq.get(key, 0) + 1
                if a.state.value != "idle":
                    agent_ticks_active[a.agent_id] = agent_ticks_active.get(a.agent_id, 0) + 1
            if not has_more:
                break

        # Rolling throughput over last _WINDOW ticks
        new_items = sum(m.items_picked for m in sim.completed_metrics) - prev_total_items
        prev_total_items += new_items
        _window_items += new_items
        window_elapsed = sim.current_tick - _window_start
        if window_elapsed >= _WINDOW:
            lines_per_hour_rolling = round(_window_items / window_elapsed * 3600, 1)
            _window_items = 0
            _window_start = sim.current_tick
        elif window_elapsed > 0:
            lines_per_hour_rolling = round(_window_items / window_elapsed * 3600, 1)

        # Emit one order_complete per completed order — a batch deposit adds multiple at once
        if len(sim.completed_metrics) > prev_metrics_count:
            for m in sim.completed_metrics[prev_metrics_count:]:
                await websocket.send_text(json.dumps({
                    "type": "order_complete",
                    "tick": sim.current_tick,
                    "order_id": m.order_id,
                    "items_picked": m.items_picked,
                    "distance_traveled": m.distance_traveled,
                    "ticks_taken": m.ticks_taken,
                    "completed_at_tick": m.completed_at_tick,
                }))
            prev_metrics_count = len(sim.completed_metrics)

        await websocket.send_text(json.dumps({
            "type": "tick",
            "tick": sim.current_tick,
            "lines_per_hour_rolling": lines_per_hour_rolling,
            "agents": [
                {
                    "id": a.agent_id,
                    "row": a.pos[0],
                    "col": a.pos[1],
                    "state": a.state.value,
                    "carrying": [item.item_id for item in a.carried_items],
                    "active_batch": sim._agent_batch[a.agent_id].batch_id
                        if sim._agent_batch[a.agent_id] else None,
                    "util_pct": round(
                        agent_ticks_active.get(a.agent_id, 0) / max(1, sim.current_tick) * 100
                    ),
                }
                for a in sim.agents
            ],
            # backward-compat single-agent fields
            "agent": {
                "row": sim.agent.pos[0],
                "col": sim.agent.pos[1],
                "state": sim.agent.state.value,
                "carrying": [item.item_id for item in sim.agent.carried_items],
            },
            "active_order": sim._active_order.order_id if sim._active_order else None,
            "active_batch": sim._active_batch.batch_id if sim._active_batch else None,
        }))

        if not has_more:
            break

        # Wait tick_delay_s before next frame, processing any control messages
        # that arrive mid-wait without accidentally advancing the simulation.
        if tick_delay_s > 0:
            tick_start = asyncio.get_event_loop().time()
            while True:
                elapsed = asyncio.get_event_loop().time() - tick_start
                remaining = tick_delay_s - elapsed
                if remaining <= 0.001:
                    break
                try:
                    msg = await asyncio.wait_for(websocket.receive_text(), timeout=remaining)
                    ctrl = json.loads(msg)
                    if ctrl.get("type") == "set_speed":
                        tick_delay_s = max(0, ctrl["tick_delay_ms"] / 1000.0)
                    elif ctrl.get("type") == "set_steps":
                        steps_per_frame = max(1, int(ctrl["steps_per_frame"]))
                    elif ctrl.get("type") == "stop":
                        return
                except asyncio.TimeoutError:
                    break
        else:
            # tick_delay_s == 0: still yield to the event loop so WS messages can arrive
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=0)
                ctrl = json.loads(msg)
                if ctrl.get("type") == "set_speed":
                    tick_delay_s = max(0, ctrl["tick_delay_ms"] / 1000.0)
                elif ctrl.get("type") == "set_steps":
                    steps_per_frame = max(1, int(ctrl["steps_per_frame"]))
                elif ctrl.get("type") == "stop":
                    return
            except (asyncio.TimeoutError, Exception):
                await asyncio.sleep(0)  # yield to event loop

    summary = sim.get_summary()
    metrics = sim.completed_metrics
    avg_ticks = (
        sum(m.ticks_taken for m in metrics) / len(metrics) if metrics else 0.0
    )
    await websocket.send_text(json.dumps({
        "type": "complete",
        "total_ticks": summary.total_ticks,
        "total_orders": summary.total_orders,
        "summary": {
            "total_items_picked": summary.total_items,
            "total_distance": summary.total_distance,
            "avg_ticks_per_order": round(avg_ticks, 1),
            "lines_per_hour": summary.lines_per_hour,
            "idle_ticks": summary.idle_ticks,
            "stockout_count": summary.stockout_count,
        },
        "heatmap": cell_visit_freq,
    }))
