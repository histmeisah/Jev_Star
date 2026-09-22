"""Observed orders and SC2 acknowledgements, without selecting any actions."""

import copy

from s2clientprotocol import error_pb2


ORDER_KINDS = {4: "stop", 16: "move", 23: "attack", 386: "heal"}


def order_record(order, references):
    tag = order.target_unit_tag
    return {
        "ability_id": order.ability_id,
        "kind": ORDER_KINDS.get(order.ability_id, "other"),
        "target_tag": str(tag) if tag else None,
        "target_unit": references.get(tag),
        "target_position": [round(order.target_world_space_pos.x, 3),
                            round(order.target_world_space_pos.y, 3)]
        if order.HasField("target_world_space_pos") else None,
    }


def result_record(code):
    try:
        name = error_pb2.ActionResult.Name(code)
    except ValueError:
        name = "UnknownActionResult"
    return {"code": int(code), "name": name}


def action_feedback(env, observation_loop):
    """Map results to the commands actually submitted, including skipped units.

    Acceptance is not proof of a shot/heal. Observation errors describe the
    following interval and may refer to an already active order.
    """
    submissions = env.last_submissions
    results = list(env.last_action_results[0].result)
    commands = [s for s in submissions if s["command"] is not None]
    mapped = len(commands) == len(results)
    references = {u.tag: f"u{i}" for i, u in env.agents.items()}
    references.update({u.tag: f"e{i}" for i, u in env.enemies.items()})
    actors, command_index = {}, 0
    for submission in submissions:
        command = submission["command"]
        record = {"requested_action_id": str(submission["action_id"]),
                  "command": order_record(command.action_raw.unit_command, references) if command else None,
                  "submission": submission["submission"], "response": None,
                  "observation_errors": []}
        if command is not None:
            record["command_index"] = command_index
            record["submission"] = ("accepted" if results[command_index] == 1 else "rejected") if mapped else "result_unmapped"
            if mapped:
                record["response"] = result_record(results[command_index])
            command_index += 1
        actors[submission["actor"]] = record
    unmatched = []
    for error in env._obs.action_errors:
        record = {"unit_tag": str(error.unit_tag), "ability_id": error.ability_id,
                  "kind": ORDER_KINDS.get(error.ability_id, "other"), **result_record(error.result)}
        actor = references.get(error.unit_tag)
        if actor in actors:
            actors[actor]["observation_errors"].append(record)
        else:
            unmatched.append(record)
    return copy.deepcopy({
        "from_game_loop": observation_loop, "to_game_loop": env._obs.observation.game_loop,
        "meaning": "SC2 accepted/rejected submitted commands; acceptance does not prove firing or healing. "
                   "Observation errors occurred during this interval, possibly on an existing order. "
                   "Current observed orders are in allies/team_core.",
        "actors": actors, "unmatched_observation_errors": unmatched,
        "response_mapping": "exact" if mapped else "count_mismatch",
        "raw_response_results": results,
    })
