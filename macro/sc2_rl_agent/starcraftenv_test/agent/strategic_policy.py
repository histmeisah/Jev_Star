"""Pure, testable constraints for an Astra plan over the existing macro actions."""

from .macro_contract import ARMY_ACTIONS, DEFEND_ACTION


def plan_progress(plan, catalog, resource):
    remaining = [g for g in plan["goals"]
                 if catalog[str(g["action_id"])]["count_with_pending"] < g["target"]]
    reserve = plan["reserve_for_action"]
    active_reserve = (reserve is not None and resource["worker_supply"] >= plan["reserve_after_workers"]
                      and any(g["action_id"] == reserve for g in remaining)
                      and not catalog[str(reserve)].get("reservation_blocked"))
    cost = catalog[str(reserve)]["cost"] if active_reserve else {"minerals": 0, "gas": 0}
    return {"remaining_goals": remaining, "reserve_for_action": reserve if active_reserve else None,
            "reserved_minerals": cost["minerals"], "reserved_gas": cost["gas"],
            "reservation_suspended_reason": catalog[str(reserve)].get("reservation_blocked") if reserve is not None else None}


def policy_reason(action, plan, catalog, resource, army_intent, last_intent_time, game_time, emergency):
    """Called only after the action passes actual SC2 legality/affordability checks."""
    progress = plan_progress(plan, catalog, resource)
    if action == 0 and catalog["0"]["count_with_pending"] >= plan["worker_target"]:
        return "plan_worker_target_reached"
    if action == 21 and catalog["21"]["count_with_pending"] >= plan["base_target"]:
        return "plan_base_target_reached"
    urgent_supply = action == 19 and (resource.get("needs_power") or resource.get("needs_supply") or
                                      resource["supply_left"] <= 2 and resource["supply_cap"] < 200)
    urgent_defense = emergency and (action in {1, 3, 14, 32, 33} or
                                    action in plan["allowed_spending_actions"] and 1 <= action <= 18 and action not in {13, 15})
    override = urgent_supply or urgent_defense
    if action <= 59 and not override:
        if action not in plan["allowed_spending_actions"]:
            return "plan_spending_not_allowed"
        goal = next((g for g in plan["goals"] if g["action_id"] == action), None)
        if goal and catalog[str(action)]["count_with_pending"] >= goal["target"]:
            return "plan_goal_target_reached"
        if action != progress["reserve_for_action"]:
            cost = catalog[str(action)]["cost"]
            if ((cost["minerals"] and resource["mineral"] - cost["minerals"] < progress["reserved_minerals"])
                    or (cost["gas"] and resource["gas"] - cost["gas"] < progress["reserved_gas"])):
                return "plan_resource_reservation"
    if action in ARMY_ACTIONS:
        ready_army = resource.get("ready_army_supply", resource["army_supply"])
        retreat_needed = emergency or ready_army < plan["retreat_below_army"]
        target = ARMY_ACTIONS[action]
        urgent_withdrawal = action == 65 and army_intent == "attack" and retreat_needed
        new_defense_order = (action == DEFEND_ACTION and plan["army_posture"] == "defend"
                             and plan.get("accepted_game_seconds", -1) > last_intent_time)
        if (target != army_intent and game_time - last_intent_time < plan["min_posture_seconds"]
                and not urgent_withdrawal and not new_defense_order):
            return "plan_hold_army_intent"
        if action == 64 and (plan["army_posture"] != "attack" or ready_army < plan["attack_min_army"]):
            return "plan_attack_not_ready"
        if action == 65 and plan["army_posture"] == "attack" and not retreat_needed:
            return "plan_continue_attack"
        if action == DEFEND_ACTION and plan["army_posture"] != "defend" and not emergency:
            return "plan_posture_not_defend"
    return None
