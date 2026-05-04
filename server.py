from __future__ import annotations
import asyncio
import json
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
from warehouse.data_gen import generate_catalog, generate_orders

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

    grid = WarehouseGrid.from_dict(layout_dict)
    inventory = Inventory(grid)

    items = generate_catalog(n_items=n_items, n_families=n_families, demand_skew=demand_skew, seed=seed)
    items = items[:len(inventory._slots)]  # clamp to available rack slots
    if not items:
        await websocket.send_text(json.dumps(
            {"type": "error", "message": "Layout has no reachable rack slots."}
        ))
        return

    inventory.seed(items)
    orders = generate_orders(
        items, n_orders=n_orders, items_per_order=items_per_order,
        family_affinity=family_affinity, seed=seed
    )

    agent = PickAgent(agent_id="A1", start_pos=grid.pack_station_pos, grid=grid)
    sim = Simulation(grid=grid, inventory=inventory, agent=agent)
    for order in orders:
        sim.enqueue_order(order)

    prev_metrics_count = 0

    while True:
        has_more = sim.step()

        # Emit order_complete before the tick frame so the UI updates cleanly
        if len(sim.completed_metrics) > prev_metrics_count:
            m = sim.completed_metrics[-1]
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
            "agent": {
                "row": sim.agent.pos[0],
                "col": sim.agent.pos[1],
                "state": sim.agent.state.value,
                "carrying": [item.item_id for item in sim.agent.carried_items],
            },
            "active_order": sim._active_order.order_id if sim._active_order else None,
        }))

        if not has_more:
            break

        # Wait tick_delay_s before next tick, processing any control messages
        # that arrive mid-wait without accidentally advancing the simulation.
        tick_start = asyncio.get_event_loop().time()
        while True:
            elapsed = asyncio.get_event_loop().time() - tick_start
            remaining = tick_delay_s - elapsed
            if remaining <= 0.005:
                break
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=remaining)
                ctrl = json.loads(msg)
                if ctrl.get("type") == "set_speed":
                    tick_delay_s = max(0.01, ctrl["tick_delay_ms"] / 1000.0)
                elif ctrl.get("type") == "stop":
                    return
            except asyncio.TimeoutError:
                break

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
    }))
