from __future__ import annotations
from dataclasses import dataclass, field
from enum import IntEnum
import numpy as np


class CellType(IntEnum):
    EMPTY = 0
    RACK = 1
    AISLE = 2
    PACK_STATION = 3


@dataclass
class WarehouseGrid:
    rows: int
    cols: int
    grid: np.ndarray  # shape (rows, cols), dtype=np.uint8
    pack_station_pos: tuple[int, int]
    zone_map: dict[tuple[int, int], str] = field(default_factory=dict)
    # zone_map: rack_pos (row, col) -> zone label ("A", "B", ...) ordered by distance from pack station

    @staticmethod
    def _fill_rack_bands(g: np.ndarray, row_start: int, row_end: int,
                         col_start: int, col_end: int) -> None:
        """Fill alternating 2-wide RACK bands within a rectangular region (exclusive end).
        Bands are separated by 2-cell aisles to allow congestion modelling."""
        col = col_start + 1
        while col + 1 < col_end - 1:
            for r in range(row_start + 1, row_end - 1):
                g[r, col] = CellType.RACK
                g[r, col + 1] = CellType.RACK
            col += 4

    @staticmethod
    def _assign_zones(
        g: np.ndarray,
        pack_pos: tuple[int, int],
    ) -> dict[tuple[int, int], str]:
        """
        Assign zone labels ("A", "B", ...) to each RACK cell based on which column band
        it belongs to, ordered by ascending distance of the band's centre column from
        pack_pos[1].  Bands are groups of consecutive rack columns.
        """
        rack_rows, rack_cols = np.where(g == CellType.RACK)
        if len(rack_cols) == 0:
            return {}

        # Group consecutive rack columns into bands
        unique_cols = sorted(set(rack_cols.tolist()))
        bands: list[list[int]] = []
        current: list[int] = [unique_cols[0]]
        for c in unique_cols[1:]:
            if c == current[-1] + 1:
                current.append(c)
            else:
                bands.append(current)
                current = [c]
        bands.append(current)

        # Sort bands by centre-column distance to pack station column
        pack_col = pack_pos[1]
        bands.sort(key=lambda band: abs(sum(band) / len(band) - pack_col))

        # Build lookup: column -> zone label
        col_to_zone: dict[int, str] = {}
        for idx, band in enumerate(bands):
            label = chr(ord("A") + idx)
            for c in band:
                col_to_zone[c] = label

        return {
            (int(r), int(c)): col_to_zone[int(c)]
            for r, c in zip(rack_rows.tolist(), rack_cols.tolist())
        }

    @staticmethod
    def build_default(rows: int = 12, cols: int = 24) -> "WarehouseGrid":
        """
        Symmetrical standard layout:
          - All cells start as AISLE
          - Alternating 2-wide RACK bands separated by 2-col aisles
          - Outer rows and cols remain AISLE (corridors on all four sides)
          - Pack station at bottom-left (rows-1, 0)
        cols=24 gives 6 rack bands with 2-cell aisles between them.
        """
        g = np.full((rows, cols), CellType.AISLE, dtype=np.uint8)
        WarehouseGrid._fill_rack_bands(g, 0, rows, 0, cols)
        pack_pos = (rows - 1, 0)
        g[pack_pos[0], pack_pos[1]] = CellType.PACK_STATION
        zone_map = WarehouseGrid._assign_zones(g, pack_pos)
        return WarehouseGrid(rows=rows, cols=cols, grid=g,
                             pack_station_pos=pack_pos, zone_map=zone_map)

    @staticmethod
    def build_quad(unit_rows: int = 12, unit_cols: int = 24) -> "WarehouseGrid":
        """
        Four default layouts arranged in a 2×2 grid.
        Total size: (2*unit_rows) × (2*unit_cols).
        Each quadrant has its own rack bands and surrounding aisle corridors.
        The shared borders create wider central aisles acting as main thoroughfares.
        Pack station at bottom-left corner.
        """
        rows = unit_rows * 2
        cols = unit_cols * 2
        g = np.full((rows, cols), CellType.AISLE, dtype=np.uint8)

        for r_off in (0, unit_rows):
            for c_off in (0, unit_cols):
                WarehouseGrid._fill_rack_bands(
                    g, r_off, r_off + unit_rows, c_off, c_off + unit_cols
                )

        pack_pos = (rows - 1, 0)
        g[pack_pos[0], pack_pos[1]] = CellType.PACK_STATION
        zone_map = WarehouseGrid._assign_zones(g, pack_pos)
        return WarehouseGrid(rows=rows, cols=cols, grid=g,
                             pack_station_pos=pack_pos, zone_map=zone_map)

    def in_bounds(self, row: int, col: int) -> bool:
        return 0 <= row < self.rows and 0 <= col < self.cols

    def is_walkable(self, row: int, col: int) -> bool:
        if not self.in_bounds(row, col):
            return False
        ct = self.grid[row, col]
        return ct == CellType.AISLE or ct == CellType.PACK_STATION

    def get_rack_neighbors(self, row: int, col: int) -> list[tuple[int, int]]:
        """Returns walkable cells adjacent to a RACK cell."""
        neighbors = []
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = row + dr, col + dc
            if self.is_walkable(nr, nc):
                neighbors.append((nr, nc))
        return neighbors

    def to_dict(self) -> dict:
        return {
            "rows": self.rows,
            "cols": self.cols,
            "grid": self.grid.tolist(),
            "pack_station_pos": list(self.pack_station_pos),
            "zone_map_list": [[r, c, z] for (r, c), z in self.zone_map.items()],
        }

    @staticmethod
    def from_dict(data: dict) -> "WarehouseGrid":
        g = np.array(data["grid"], dtype=np.uint8)
        pack_pos = tuple(data["pack_station_pos"])
        zone_map: dict[tuple[int, int], str] = {
            (entry[0], entry[1]): entry[2]
            for entry in data.get("zone_map_list", [])
        }
        if not zone_map:
            zone_map = WarehouseGrid._assign_zones(g, pack_pos)
        return WarehouseGrid(
            rows=data["rows"],
            cols=data["cols"],
            grid=g,
            pack_station_pos=pack_pos,
            zone_map=zone_map,
        )
