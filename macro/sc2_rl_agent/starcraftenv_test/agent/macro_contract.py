"""Shared, explicit limits and command IDs for planning and execution."""

from ..utils.action_info import ActionDescriptions

VERSION = "macro-v2.2.1"
DEFEND_ACTION = 72
ARMY_ACTIONS = {64: "attack", 65: "retreat", DEFEND_ACTION: "defend"}
# These are policy limits, not game technology requirements. Expose them to Astra.
UNIT_LIMITS = {"PROBE": 76, "OBSERVER": 4, "WARPPRISM": 2, "MOTHERSHIP": 1}
BUILDING_LIMITS = {
    "NEXUS": 8, "GATEWAY": 16, "FORGE": 3, "ROBOTICSFACILITY": 6,
    "STARGATE": 6, "PHOTONCANNON": 32, "SHIELDBATTERY": 24,
    "CYBERNETICSCORE": 1, "TWILIGHTCOUNCIL": 1, "TEMPLARARCHIVE": 1,
    "DARKSHRINE": 1, "ROBOTICSBAY": 1, "FLEETBEACON": 1,
}
ACTION_LIMITS = {
    action: (UNIT_LIMITS if action <= 18 else BUILDING_LIMITS).get(description.split()[1], 200)
    for action, description in ActionDescriptions("Protoss").flattened_actions.items() if action <= 33
}
ACTION_LIMITS.update({action: 1 for action in range(34, 60)})


def primary_action(plan, choices, army_intent, ready_army_supply, target_changed=False, acknowledged_actions=()):
    """Recommend a legal next action; Jev still selects the actual command."""
    desired = next(action for action, posture in ARMY_ACTIONS.items() if posture == plan["army_posture"])
    ready = desired != 64 or ready_army_supply >= plan["attack_min_army"]
    if ready and (army_intent != plan["army_posture"] or target_changed) and desired in choices:
        return desired, "apply_army_order_before_optional_production"
    preferred = plan.get("priority_action")
    if preferred in choices and preferred != 71 and preferred not in acknowledged_actions:
        return preferred, "commander_priority"
    for action in plan.get("production_priority", []):
        if action in choices:
            return action, "first_attainable_production_priority"
    return None, "no_priority_currently_executable"
