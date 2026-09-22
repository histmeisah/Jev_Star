"""Versioned public map priors, separate from fog-limited live observations."""

from collections import Counter, deque
import hashlib
import inspect
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import mpyq
import numpy as np

from .catalog import REPO, map_param_registry, map_source
from .environment import BaseScript, ExtendedBaseScript, NearestScript, WeakestScript


BRIEF_VERSION = 1
SPAWN = re.compile(r'libNtve_gf_CreateUnitsWithDefaultFacing\(\s*(\d+)\s*,\s*"([^\"]+)"\s*,\s*\d+\s*,\s*([12])\s*,\s*PointFromId\((\d+)\)\s*\)')
UPGRADE = re.compile(r'libNtve_gf_SetUpgradeLevelForPlayer\(\s*([12])\s*,\s*"([^\"]+)"\s*,\s*(\d+)\s*\)')
ACTION_CONTRACT = {
    "attack_candidate_center_distance_cap": 6,
    "attack_candidate_rule": "Living, visible, compatible targets within center distance 6; use supplied criteria. "
                             "This SMAC cap is independent of native weapon range and cooldown.",
    "range_estimate": "Center distance <= native weapon range + both unit radii. null means unknown; "
                      "geometry alone does not guarantee line of sight, weapon firing or an available action.",
    "attack_order": "Approach if needed, fire when ready; accepted is not fired.",
    "abilities_enabled": False,
    "actions": "0 dead only; 1 Stop cancels order; 2/3/4/5 N/S/E/W movement endpoints; "
               "6+j attack visible e{j}, or heal u{j} for Medivacs. No hold-order choice.",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def map_facts(name):
    source = map_source(name)
    archive = mpyq.MPQArchive(str(source))
    try:
        galaxy = archive.read_file("MapScript.galaxy").decode("utf-8-sig")
        points = {p.attrib["Id"]: [float(v) for v in p.attrib["Position"].split(",")[:2]]
                  for p in ET.fromstring(archive.read_file("Objects")).findall("ObjectPoint")}
        rosters = {"allies": Counter(), "enemies": Counter()}
        spawns = []
        for count, unit_type, owner, point in SPAWN.findall(galaxy):
            side = "allies" if owner == "1" else "enemies"
            rosters[side][unit_type] += int(count)
            spawns.append({"side": side, "type": unit_type, "count": int(count),
                           "spawn_anchor": points.get(point), "point_id": point})
        params = map_param_registry[name]
        verified = (sum(rosters["allies"].values()) == params["n_agents"] and
                    sum(rosters["enemies"].values()) == params["n_enemies"])
        if not verified:
            raise ValueError(f"Static map roster does not match registry: {name}")
        return {"source": str(source.relative_to(REPO)),
                "map_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "members": ["MapScript.galaxy", "Objects"],
                "rosters": {k: dict(v) for k, v in rosters.items()}, "initial_spawns": spawns,
                "initial_upgrade_calls": [{"side": "allies" if owner == "1" else "enemies",
                                           "upgrade": upgrade, "level": int(level)}
                                          for owner, upgrade, level in UPGRADE.findall(galaxy)],
                "scope": "Literal fixed initialization in the map assets, not runtime enemy observations. "
                         "Spawn anchors are not exact per-unit positions. Not a complete Galaxy evaluator; "
                         "custom unit overrides are not inferred from unused XML entries."}
    finally:
        archive.file.close()


def spans(values):
    """Half-open integer runs [start, end)."""
    result = []
    for value in sorted(values):
        if result and result[-1][1] == value:
            result[-1][1] += 1
        else:
            result.append([value, value + 1])
    return result


def terrain_brief(env):
    if not hasattr(env, "playable_area") or not hasattr(env, "pathing_grid"):
        return {"status": "unknown", "reason": "Engine static terrain was not supplied."}
    (x0, y0), (x1, y1) = env.playable_area
    grid = env.pathing_grid
    # Native ImageData is row-major [y,x]. The legacy RL terrain tensor flips
    # y, unlike its 1-bit pathing tensor, so it is not suitable for world geometry.
    native = getattr(env, "map_start_raw", None)
    height_grid = env.terrain_height
    source = "backend pathing_grid[x,y] and caller-supplied terrain_height[x,y]"
    if native is not None:
        height_grid = unpack_world_grid(native.terrain_height) / 255
        pathing = unpack_world_grid(native.pathing_grid).astype(bool)
        if pathing.shape != grid.shape or not np.array_equal(pathing, grid):
            raise ValueError("Native terrain pathing differs from SMAC's action-mask grid")
        source = "game_info.start_raw ImageData, row-major [y,x] -> world [x,y], no y flip; pathing matches action-mask grid"
    slabs, heights, walkable = [], Counter(), set()
    for y in range(y0, y1):
        cells = [x for x in range(x0, x1) if grid[x, y]]
        runs = spans(cells)
        if slabs and slabs[-1]["x_intervals"] == runs:
            slabs[-1]["y"][1] = y + 1
        else:
            slabs.append({"y": [y, y + 1], "x_intervals": runs})
        for x in cells:
            walkable.add((x, y))
            heights[round(float(height_grid[x, y]), 4)] += 1
    # Exact 4-neighbour components; no speculative tactical choke/retreat labels.
    pending, regions = set(walkable), []
    while pending:
        seed = min(pending)
        pending.remove(seed)
        queue, component = deque([seed]), []
        while queue:
            x, y = queue.popleft()
            component.append((x, y))
            for cell in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if cell in pending:
                    pending.remove(cell)
                    queue.append(cell)
        xs, ys = zip(*component)
        regions.append({"cells": len(component), "bounds": [[min(xs), min(ys)], [max(xs) + 1, max(ys) + 1]],
                        "seed": list(seed)})
    # Height runs for walkable cells preserve where raised terrain actually is.
    height_slabs = []
    for y in range(y0, y1):
        row = []
        for x in range(x0, x1):
            if (x, y) not in walkable:
                continue
            level = round(float(height_grid[x, y]), 4)
            if row and row[-1][1] == x and row[-1][2] == level:
                row[-1][1] = x + 1
            else:
                row.append([x, x + 1, level])
        if height_slabs and height_slabs[-1]["x_start_end_height"] == row:
            height_slabs[-1]["y"][1] = y + 1
        else:
            height_slabs.append({"y": [y, y + 1], "x_start_end_height": row})
    return {"status": "known", "source": source,
            "playable_area": env.playable_area, "map_size": [env.map_x, env.map_y],
            "geometry_units": "World unit grid cells; all intervals are half-open. +x east, +y north. "
                              "Walkable spans give exact openings at cell resolution, not clearance for a unit radius. "
                              "Static terrain ignores moving units. Air movement is not constrained by ground pathing.",
            "walkable_spans": slabs, "ground_components_4_connected": regions,
            "walkable_height_histogram_normalized": dict(heights), "walkable_height_spans": height_slabs}


def unpack_world_grid(layer):
    if layer.bits_per_pixel not in (1, 8):
        raise ValueError("Unsupported static terrain pixel format")
    data = np.frombuffer(layer.data, dtype=np.uint8)
    if layer.bits_per_pixel == 1:
        data = np.unpackbits(data)[:layer.size.x * layer.size.y]
    return data.reshape(layer.size.y, layer.size.x).T


def opponent_brief(env, configured):
    script = env.dts_script
    cls = type(script)
    source = Path(inspect.getfile(cls))
    result = {"configured_policy": configured, "selected_class": cls.__module__ + "." + cls.__name__,
              "source": str(source.relative_to(REPO)), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    if cls in (BaseScript, ExtendedBaseScript):
        destination = getattr(script, "destination", None)
        if cls is BaseScript:
            destinations = {
                **dict.fromkeys(("3st_vs_5zl", "3rp_vs_5zl", "2c_vs_64zg"), (16, 16)),
                **dict.fromkeys(("6m_vs_10m", "3hl_vs_24zl", "3rp_vs_24zl"), (6, 16)),
                **dict.fromkeys(("mmmt", "mmmt_vs_zhb", "mmmt_vs_zspi"), (3, 16)),
                **dict.fromkeys(("3m", "8m", "5m_vs_6m", "8m_vs_9m", "10m_vs_11m", "25m", "27m_vs_30m",
                                 "2s3z", "3s5z", "3s5z_vs_3s6z", "1c3s5z", "MMM", "MMM2",
                                 "3s_vs_3z", "3s_vs_4z", "3s_vs_5z"), (9, 16)),
                "7q_vs_2bc": (16, 21), "6h_vs_8z": (14.5, 9.5), "corridor": (4.5, 4),
            }
            destination = destinations.get(env.map_name)
        result.update(wait_before_step=5, attack_move_destination=list(destination) if destination else None,
                      behavior="After the wait, repeatedly attack-move toward the fixed destination. "
                               "Upstream base Medivacs follow the allied centroid on mixed maps. "
                               "For 2vr_vs_3sc, base issues no commands and relies on automatic Spore Crawler attacks.")
    elif cls in (NearestScript, WeakestScript):
        result.update(wait_before_step=10, behavior=(
            "Keep the current target until it dies; then select the nearest opponent." if cls is NearestScript else
            "Keep the current target until it dies; then select the lowest effective-health fraction opponent."))
    else:
        result.update(behavior="unknown: selected script has no verified textual summary")
    if configured == "mixed":
        result["scope"] = "The selected script is for this episode; subsequent episodes can select another script."
    return result


def build_scenario_brief(env, configured_opponent):
    facts = map_facts(env.map_name)
    actual = Counter(env.micro_data.units.get(u.unit_type, f"unit_type_{u.unit_type}")
                     for u in env.agents.values() if u.health > 0)
    if dict(actual) != facts["rosters"]["allies"]:
        raise ValueError("Static allied roster does not match initial own observation")
    brief = {"version": BRIEF_VERSION, "map": env.map_name,
             "objective": "Eliminate opposing units before the episode limit; no production or reinforcements.",
             "public_map_facts": facts, "terrain": terrain_brief(env),
             "opponent": opponent_brief(env, configured_opponent), "action_contract": ACTION_CONTRACT,
             "unknowns": ["Runtime state of unseen enemies", "Complete effects of all custom map data"],
             "live_visibility": "Enemy health and positions during play still come only from allied vision."}
    brief["sha256"] = digest(brief)
    return brief


def compact_brief(brief):
    return {"version": brief["version"], "sha256": brief["sha256"],
            "map_sha256": brief["public_map_facts"]["map_sha256"],
            "fixed_rosters": brief["public_map_facts"]["rosters"],
            "playable_area": brief["terrain"].get("playable_area"),
            "opponent": {k: v for k, v in brief["opponent"].items() if k not in ("source", "source_sha256")},
            "scope": "Public fixed scenario prior; not current hidden enemy state. Full static geometry is supplied to the one-time planner."}
