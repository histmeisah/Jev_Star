"""Semantic observations and closed action candidates; no scripted micro for Jev."""

import math
import json
import copy

from .catalog import SUPPORTED_MAPS
from .environment import is_medivac
from .feedback import order_record
from .scenario import ACTION_CONTRACT, compact_brief


SCHEMA_VERSION = 6
INTERFACE_VERSION = "p0-v1"
DIRECTIONS = {2: ("north", 0, 1), 3: ("south", 0, -1),
              4: ("east", 1, 0), 5: ("west", -1, 0)}
INSTRUCTIONS = (
    "Control ONLY {actor}, identified in state.allies. Choose its next complete "
    "action to win this StarCraft II micro battle on {map_name}. Each surviving ally "
    "receives one action at this same game loop. Use the observed health, weapon "
    "cooldown, shields, weapon profiles, distances, recent damage and previous actions. Eliminate enemies "
    "efficiently while preserving allies. Attack orders can remain active during "
    "weapon cooldown; movement can interrupt firing. A stop command cancels the "
    "current order; it is NOT a no-op. Enemies outside team vision are unknown. "
    "If none are visible, the map center is a search location, not a known enemy "
    "position. Range null means unknown. SMAC attack candidates have a center-distance cap of 6, "
    "independent of native range and cooldown. last_action_result reports SC2 acknowledgements, "
    "not proof of firing. In batches, team_core preserves every ally's core fields in named columns; "
    "allies entries outside the batch point to those rows. No pending team choices are known. "
    "Select only a supplied action ID."
)


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def weapon_range(static_data, unit, target):
    stat = static_data.unit_stats.get(unit.unit_type)
    if unit.unit_type == 57 and (stat is None or not stat.weapons):
        return 6  # Battlecruiser effect-based attacks are absent from the API weapon list.
    # SC2 weapon target types: 1=ground, 2=air, 3=any.
    target_types = {2, 3} if target.is_flying else {1, 3}
    if target.unit_type == 4:
        target_types.add(2)  # Colossi can also be attacked by anti-air weapons.
    weapons = stat.weapons if stat else []
    ranges = [w.range for w in weapons if getattr(w, "type", 3) in target_types]
    return max(ranges) if ranges else None


def range_status(static_data, unit, target, center_distance):
    native_range = weapon_range(static_data, unit, target)
    if native_range is not None:
        inside = center_distance <= native_range + unit.radius + target.radius
        return ("in_range" if inside else "out_of_range"), inside, native_range
    stat = static_data.unit_stats.get(unit.unit_type)
    if is_medivac(unit, static_data) or (stat is not None and stat.weapons):
        return "no_compatible_weapon", False, None
    return "unknown", None, None


def combat_profile(stat, unit_type=None):
    if stat is None:
        return {"known": False}
    profile = {
        "known": True,
        "movement_speed_native": getattr(stat, "movement_speed", None),
        "armor": getattr(stat, "armor", None),
        "weapons": [{"target_type": {1: "ground", 2: "air", 3: "any"}.get(getattr(w, "type", 3)),
                     "range": round(w.range, 3), "base_damage": getattr(w, "damage", None),
                     "hits_per_attack": getattr(w, "attacks", None)} for w in stat.weapons],
    }
    if unit_type == 57 and not stat.weapons:
        profile["weapons"] = [{"target_type": "any", "range": 6, "base_damage": None,
                               "hits_per_attack": None}]
        profile["weapon_source"] = "Battlecruiser fallback; SC2 omits effect-based weapons. Damage unknown."
    elif not stat.weapons:
        profile["weapon_source"] = "API weapon list is empty; effect-based attacks and their range may be unknown."
    return profile


def unit_record(unit, name, previous_health=None, own=False, previous_shields=None, references=None):
    effective_health = unit.health + unit.shield if unit.health > 0 else 0
    effective_max = unit.health_max + unit.shield_max
    result = {
        "tag": str(unit.tag), "type": name, "alive": unit.health > 0,
        "health": round(unit.health, 2), "max_health": round(unit.health_max, 2),
        "shield": round(unit.shield, 2) if unit.health > 0 else 0,
        "max_shield": round(unit.shield_max, 2),
        "effective_health": round(effective_health, 2), "unit_type_id": unit.unit_type,
        "position": [round(unit.pos.x, 3), round(unit.pos.y, 3)],
        "radius": round(unit.radius, 3),
        "is_flying": unit.is_flying,
    }
    if own:
        result.update(
            weapon_cooldown_loops=round(unit.weapon_cooldown, 2),
            weapon_ready=unit.weapon_cooldown <= 0,
            energy=round(unit.energy, 2), max_energy=round(unit.energy_max, 2),
            health_status="critical" if effective_health <= effective_max * 0.3 else "healthy_or_damaged",
            health_lost_since_previous_decision=round(max(0, (previous_health or {}).get(
                unit.tag, unit.health) - unit.health), 2),
            shield_lost_since_previous_decision=round(max(0, (previous_shields or {}).get(
                unit.tag, unit.shield) - unit.shield), 2),
            orders=[order_record(order, references or {}) for order in unit.orders] if unit.health > 0 else [],
        )
    return result


def build_request(env, static_data, model, episode, previous_health=None, previous_actions=None,
                  previous_shields=None, previous_result=None):
    if env.map_name not in SUPPORTED_MAPS or env.use_ability:
        raise ValueError("This adapter uses registered SMAC maps with abilities disabled.")
    enemy_ids = {u.tag: i for i, u in env.enemies.items()}
    # Never serialize env.enemies or get_state(): they include the opponent's view.
    visible = {enemy_ids[u.tag]: u for u in env._obs.observation.raw_data.units
               if u.owner == 2 and u.display_type == 1 and u.health > 0 and u.tag in enemy_ids}
    references = {u.tag: f"u{i}" for i, u in env.agents.items()}
    references.update({tag: f"e{i}" for tag, i in enemy_ids.items()})
    allies = {f"u{i}": unit_record(u, static_data.units.get(u.unit_type, f"unit_type_{u.unit_type}"),
                                     previous_health, own=True, previous_shields=previous_shields, references=references)
              for i, u in env.agents.items()}
    enemies = {f"e{i}": unit_record(u, static_data.units.get(u.unit_type, f"unit_type_{u.unit_type}"))
               for i, u in visible.items()}
    state = {
        "objective": "Eliminate all opposing units before the episode step limit.",
        "schema_version": SCHEMA_VERSION,
        "interface_version": INTERFACE_VERSION,
        "action_contract": copy.deepcopy(ACTION_CONTRACT),
        "map": env.map_name, "episode": episode, "game_loop": env._obs.observation.game_loop,
        "step_mul": env._step_mul, "steps_remaining": env.episode_limit - env._episode_steps,
        "visibility": "shared allied team vision; unseen enemies are unknown, not dead",
        "coordinate_system": "SC2 world coordinates: north=+y, east=+x",
        "search_location": [env.map_x / 2, env.map_y / 2],
        "allies": allies, "visible_enemies": enemies,
        "unit_type_profiles": {str(unit_type): combat_profile(static_data.unit_stats.get(unit_type), unit_type)
                               for unit_type in sorted({u.unit_type for u in [*env.agents.values(), *visible.values()]})},
        "previous_action_ids": previous_actions or {},
        "last_action_result": copy.deepcopy(previous_result),
        "relations": {}, "movement_candidates": {}, "heal_candidates": {},
    }
    brief = getattr(env, "scenario_brief", None)
    state["scenario_brief"] = compact_brief(brief) if brief else {"status": "unknown", "reason": "No public map brief supplied"}
    questions = {}
    for i, unit in env.agents.items():
        if unit.health <= 0:
            continue  # Dead units get SMAC no-op locally; no paid model question.
        actor = f"u{i}"
        healer = is_medivac(unit, static_data)
        origin = allies[actor]["position"]
        mask = env.get_avail_agent_actions(i)
        relations = {}
        for enemy_id, target in visible.items():
            d = distance(origin, [target.pos.x, target.pos.y])
            own_status, in_range, attack_range = range_status(static_data, unit, target, d)
            enemy_status, enemy_in_range, _ = range_status(static_data, target, unit, d)
            available = not healer and bool(mask[6 + enemy_id])
            relations[f"e{enemy_id}"] = {
                "distance": round(d, 2),
                "in_weapon_range": in_range,
                "weapon_range_status": own_status, "native_weapon_range": attack_range,
                "in_enemy_weapon_range": enemy_in_range, "enemy_weapon_range_status": enemy_status,
                "smac_attack_available": available,
                "smac_center_distance_allowed": d <= 6,
                "approach_needed": not in_range if in_range is not None and own_status != "no_compatible_weapon" else None,
            }
        state["relations"][actor] = relations
        criteria = {"1": "Stop: cancel this unit's current order at its current position."}
        movements = {}
        for action_id, (name, dx, dy) in DIRECTIONS.items():
            if not mask[action_id]:
                continue
            target = [round(origin[0] + dx * env._move_amount, 3),
                      round(origin[1] + dy * env._move_amount, 3)]
            movements[str(action_id)] = {
                "direction": name, "destination": target,
                "distance_to_search_location": round(distance(target, state["search_location"]), 2),
                "distances_to_visible_enemies": {
                    enemy_id: round(distance(target, data["position"]), 2)
                    for enemy_id, data in enemies.items()},
            }
            criteria[str(action_id)] = (
                f"Move {name} by {env._move_amount} world units toward {target}. "
                f"See state.movement_candidates.{actor}.{action_id} for distances.")
        state["movement_candidates"][actor] = movements
        for enemy_id in ([] if healer else visible):
            action_id = 6 + enemy_id
            if mask[action_id]:
                relation = relations[f"e{enemy_id}"]
                criteria[str(action_id)] = (
                    f"Attack e{enemy_id}; range={relation['weapon_range_status']}.")
        if healer:
            healing = {}
            for ally_id, target in env.agents.items():
                action_id = 6 + ally_id
                if mask[action_id] and target.health > 0:
                    healing[str(action_id)] = {
                        "target": f"u{ally_id}", "missing_health": round(target.health_max - target.health, 2),
                        "distance": round(distance(origin, [target.pos.x, target.pos.y]), 2)}
                    criteria[str(action_id)] = f"Heal ally u{ally_id}: restore health using energy. Cannot restore shields."
            state["heal_candidates"][actor] = healing
        questions[actor] = {"type": "choice", "instructions": INSTRUCTIONS.format(actor=actor, map_name=env.map_name),
                            "criteria": criteria}
        questions[actor]["instructions"] += f" Current weapon cooldown: {allies[actor]['weapon_cooldown_loops']} loops."
        if healer:
            questions[actor]["instructions"] += " This unit is a Medivac: it heals allies and has no attack weapon."
    return {"model": model, "state": state, "questions": questions}


TEAM_CORE_FIELDS = ("unit_type_id", "position", "radius", "is_flying",
                    "alive", "health", "max_health", "shield", "max_shield",
                    "weapon_cooldown_loops", "weapon_ready", "energy", "max_energy",
                    "health_lost_since_previous_decision", "shield_lost_since_previous_decision", "orders")
ORDER_FIELDS = ("ability_id", "kind", "target_unit", "target_tag", "target_position")
ENEMY_FIELDS = ("unit_type_id", "position", "radius", "is_flying",
                "health", "max_health", "shield", "max_shield")
BATCH_RELATION_FIELDS = ("distance", "weapon_range_status", "native_weapon_range",
                         "enemy_weapon_range_status", "smac_attack_available")


def team_core(state):
    """Lossless combat core, compact only in representation. No target selection."""
    rows, order_table, order_indices = {}, [], {}
    for actor, unit in state["allies"].items():
        row = [unit[field] for field in TEAM_CORE_FIELDS]
        for field in ("alive", "weapon_ready"):
            row[TEAM_CORE_FIELDS.index(field)] = int(unit[field])
        row[-1] = []
        for order in unit["orders"]:
            key = json.dumps(order, sort_keys=True, separators=(",", ":"))
            if key not in order_indices:
                order_indices[key] = len(order_table)
                order_table.append([order[field] for field in ORDER_FIELDS])
            row[-1].append(order_indices[key])
        rows[actor] = row
    coverage = {}
    for enemy in state["visible_enemies"]:
        legal = ready = unknown = 0
        for actor, relations in state["relations"].items():
            relation = relations[enemy]
            if relation["smac_attack_available"]:
                legal += 1
                if state["allies"][actor]["weapon_ready"]:
                    ready += relation["in_weapon_range"] is True
                    unknown += relation["in_weapon_range"] is None
        coverage[enemy] = [legal, ready, unknown]
    return {"ally_columns": list(TEAM_CORE_FIELDS), "allies": rows, "order_columns": list(ORDER_FIELDS),
            "order_table": order_table,
            "unit_types": {str(u["unit_type_id"]): u["type"] for u in [*state["allies"].values(), *state["visible_enemies"].values()]},
            "enemy_columns": list(ENEMY_FIELDS),
            "visible_enemies": {k: [v[f] for f in ENEMY_FIELDS] for k, v in state["visible_enemies"].items()},
            "live_allies_with_zero_cooldown": sum(u["alive"] and u["weapon_ready"] for u in state["allies"].values()),
            "coverage_columns": ["legal_attackers", "ready_legal_in_range_attackers", "ready_legal_unknown_range_attackers"],
            "enemy_coverage": coverage,
            "meaning": "Rows use named columns; alive/weapon_ready are 0/1. orders indexes order_table, with raw IDs/tags. "
                       "Types look up unit_types/profiles. Zero-cooldown count includes healers. Coverage is factual, not allocated hits."}


def compact_feedback(feedback, actors):
    if feedback is None:
        return None
    result = {k: v for k, v in feedback.items() if k != "actors"}
    result["team_columns"] = ["requested_action_id", "submission", "response_code", "observation_errors"]
    result["result_names"] = {}
    result["team"] = {}
    result["actors"] = {actor: feedback["actors"][actor] for actor in actors if actor in feedback["actors"]}
    for actor, record in feedback["actors"].items():
        response = record["response"]
        if response:
            result["result_names"][str(response["code"])] = response["name"]
        result["team"][actor] = [record["requested_action_id"], record["submission"],
                                  response["code"] if response else None, record["observation_errors"]]
    result["batch_encoding"] = "team rows give every actor's requested action, acknowledgement and errors. " \
                               "actors retains full submitted command/raw result details for this batch."
    return result


def compact_numbers(value):
    """Drop JSON .0 suffixes without rounding away any observation precision."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [compact_numbers(v) for v in value]
    if isinstance(value, dict):
        return {k: compact_numbers(v) for k, v in value.items()}
    return value


def batch_requests(payload, state_question_limit=30000, request_limit=56000):
    """Bound UTF-8 bytes conservatively below the model's token context limits.

    Small formations keep one request. Larger formations retain a shared team
    overview and full per-actor features for each batch. No simulation steps occur
    between batches; the chosen actions are executed together.
    """
    def size(value):
        return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))

    def fits(request):
        return (size(request["state"]) + max((size(q) for q in request["questions"].values()), default=0)
                <= state_question_limit and size(request) <= request_limit)

    if fits(payload):
        return [payload]
    source = payload["state"]
    core = compact_numbers(team_core(source))
    # Preserve each independent fact. Boolean range/approach/cap fields in the
    # full observation are derivable from these statuses, distance and cap=6.
    relation_columns = list(BATCH_RELATION_FIELDS)
    enemy_ids = list(source["visible_enemies"])

    def select(actors):
        actors = set(actors)
        state = dict(source)
        state["team_core"] = core
        state["last_action_result"] = compact_feedback(source.get("last_action_result"), actors)
        state["allies"] = {k: v if k in actors else None for k, v in source["allies"].items()}
        state["visible_enemies"] = {k: None for k in source["visible_enemies"]}
        for field in ("relations", "movement_candidates", "heal_candidates"):
            state[field] = {k: v for k, v in source[field].items() if k in actors}
        state["relation_columns"] = relation_columns
        state["relations"] = {actor: {enemy: [r[c] for c in relation_columns] for enemy, r in relations.items()}
                              for actor, relations in state["relations"].items()}
        state["movement_columns"] = ["direction", "destination", "distance_to_search_location", "distances_to_visible_enemies"]
        state["movement_enemy_columns"] = enemy_ids
        state["movement_candidates"] = {
            actor: {action: [m["direction"], m["destination"], m["distance_to_search_location"],
                             [m["distances_to_visible_enemies"][enemy] for enemy in enemy_ids]]
                    for action, m in moves.items()}
            for actor, moves in state["movement_candidates"].items()}
        state["batch_actors"] = sorted(actors, key=lambda k: int(k[1:]))
        state["batch_context"] = "Null allies/visible_enemies entries refer to the same ID in team_core rows. " \
                                 "Team rows preserve combat fields; raw unit tags remain in batch actors and orders. " \
                                 "Full local action features for batch_actors. All batches use the same snapshot. " \
                                 "Relations/movements use named columns; movement distance arrays follow movement_enemy_columns. " \
                                 "last_action_result includes team results and full batch-actor commands. No pending choices are shared."
        return {"model": payload["model"], "state": state,
                "questions": {k: v for k, v in payload["questions"].items() if k in actors}}

    batches, current = [], []
    for actor in payload["questions"]:
        candidate = select([*current, actor])
        if fits(candidate):
            current.append(actor)
        else:
            if current:
                batches.append(select(current))
            current = [actor]
            if not fits(select(current)):
                raise ValueError(f"Single-unit context exceeds conservative request limit for {actor}.")
    if current:
        batches.append(select(current))
    return batches


def decode_actions(payload, answers, n_agents):
    actions = [0] * n_agents
    if set(answers) != set(payload["questions"]):
        raise ValueError("Missing or extra unit answers.")
    for actor, question in payload["questions"].items():
        action = answers[actor]["choice"]
        if action not in question["criteria"]:
            raise ValueError("Action is outside the snapshot candidate set.")
        actions[int(actor[1:])] = int(action)
    return actions


def nearest_actions(payload, n_agents):
    """A declared comparison policy using exactly the same observation/candidates."""
    answers = {}
    state = payload["state"]
    for actor, question in payload["questions"].items():
        candidates = question["criteria"]
        attacks = [a for a in candidates if int(a) >= 6]
        healing = state.get("heal_candidates", {}).get(actor, {})
        if healing:
            choice = min(healing, key=lambda a: (healing[a]["distance"], int(a)))
        elif attacks:
            choice = min(attacks, key=lambda a: (state["relations"][actor][f"e{int(a)-6}"]["distance"], int(a)))
        else:
            moves = state["movement_candidates"][actor]
            def movement_score(a):
                values = moves[a]["distances_to_visible_enemies"].values()
                return min(values) if values else moves[a]["distance_to_search_location"]
            choice = min(moves, key=lambda a: (movement_score(a), int(a))) if moves else "1"
        answers[actor] = {"choice": choice}
    return decode_actions(payload, answers, n_agents)
