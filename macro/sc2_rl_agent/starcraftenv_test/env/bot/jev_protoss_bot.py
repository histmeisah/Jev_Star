"""Jev macro adapter for the existing LLM Play SC2 Protoss observation/actions."""

import statistics
import threading
import time
from collections import Counter, deque
from pathlib import Path

from sc2.dicts.unit_train_build_abilities import TRAIN_INFO
from sc2.dicts.unit_trained_from import UNIT_TRAINED_FROM
from sc2.dicts.unit_research_abilities import RESEARCH_INFO
from sc2.dicts.upgrade_researched_from import UPGRADE_RESEARCHED_FROM
from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId

from ...agent.jev_agent import DecisionScheduler
from ...agent.macro_contract import VERSION, DEFEND_ACTION, ARMY_ACTIONS, UNIT_LIMITS, BUILDING_LIMITS
from ...utils.action_info import ActionDescriptions
from ...utils.run_logging import RunLog
from .Protoss_bot import Protoss_Bot
from .macro_execution import MacroExecution
from .macro_navigation import MacroNavigation


class JevProtossBot(MacroExecution, MacroNavigation, Protoss_Bot):
    def __init__(self, jev_client, output_dir: Path, decision_interval=1.0,
                 max_decision_age=4.0, max_requests=2000, run_log=None):
        super().__init__({}, threading.Lock(), threading.Event())
        self.military_unit_types.update({U.SENTRY, U.MOTHERSHIP})
        # Derive IDs from the flattened registry, not the legacy category count (4).
        self.action_dict = ActionDescriptions("Protoss").flattened_actions
        self.action_dict[DEFEND_ACTION] = "MULTI-DEFEND"
        self.empty_action = next(k for k, v in self.action_dict.items() if v == "EMPTY ACTION")
        self.output_dir = output_dir
        self._owns_log = run_log is None
        self.log = run_log if run_log is not None else RunLog(output_dir)
        self.scheduler = DecisionScheduler(jev_client, self.log, decision_interval,
                                           max_decision_age, max_requests)
        self.recent_outcomes = deque(maxlen=8)
        self.cooldowns = {}
        self.army_intent = "defend"
        self._last_maintenance = -100.0
        self._last_heartbeat = -100.0
        self._abilities = {}
        self._abilities_ignoring_cost = {}
        self._producers = []
        self._started_at = time.monotonic()
        self._closed = False
        self._last_game_loop = 0
        self._last_economy = {}
        self._steps = 0
        self._steps_while_inflight = 0
        self._max_step_ms = 0.0
        self._action_stats = Counter()
        self._initialize_execution()
        self._initialize_navigation()
        self._build_worker_for_catalog = None

    async def on_start(self):
        self.client.game_step = 4
        self._set_search_sites(self.expansion_locations_list)
        self._install_action_feedback()
        self.log("start", model=self.scheduler.client.model, race="Protoss", realtime=True,
                 action_count=len(self.action_dict), empty_action_id=self.empty_action,
                 map=self.game_info.map_name, base_build=self.base_build,
                 decision_interval_s=self.scheduler.interval,
                 max_decision_age_s=self.scheduler.max_age,
                 macro_contract=VERSION,
                 initial_workers=self.supply_workers, initial_supply_cap=self.supply_cap,
                 local_automation=["worker_distribution", "gateway_morph", "army_intent_execution",
                                   "assigned_scout_missions", "one_observer_army_escort"])

    def get_enemy_unity(self):
        return dict(Counter(u.type_id.name for u in self.enemy_units if u.is_visible))

    def get_enemy_structure(self):
        # The legacy method forgot to return the nonempty Counter.
        return dict(Counter(u.type_id.name for u in self.enemy_structures if u.is_visible))

    def _snapshot(self):
        information = self.get_information()
        information["game_loop"] = self.state.game_loop
        information["enemy_race"] = self.enemy_race.name
        information["visibility"] = "Enemy counts cover currently visible units only; unseen forces are unknown."
        information["army_intent"] = self.army_intent
        information["navigation"] = self._navigation_snapshot()
        information["supply_forecast"] = self._supply_forecast()
        information["base_under_attack"] = self._emergency()
        information["ready_idle_producers"] = dict(Counter(
            u.type_id.name for u in self.structures.ready.idle))
        information["bases"] = [{
            "tag": b.tag, "position": list(b.position),
            "workers": b.assigned_harvesters, "ideal_workers": b.ideal_harvesters,
            "health_fraction": round(b.health_percentage, 2),
            "visible_enemies_within_30": self.enemy_units.filter(
                lambda e: e.is_visible and e.distance_to(b) < 30).amount,
        } for b in self.townhalls.ready]
        information["recent_outcomes"] = list(self.recent_outcomes)
        return information

    def _has_ability(self, unit, ability, ignore_resources=False):
        ability_map = (self._abilities_ignoring_cost or self._abilities) if ignore_resources else self._abilities
        available = ability_map.get(unit.tag, set())
        data = self.game_data.abilities.get(ability.value)
        return ability in available or (data is not None and data.id in available)

    async def _refresh_abilities(self):
        self._producers = list(self.structures.ready)
        workers = self.workers.filter(lambda w: w.is_gathering or w.is_idle)
        units = self._producers + list(self.units.of_type({U.HIGHTEMPLAR, U.DARKTEMPLAR}).ready)
        if workers:
            units.append(workers.first)
        # Ignoring resources also exposes uncharged Warpgate abilities in this SC2
        # build. Only the ordinary query may authorize an actual order.
        abilities = await self.get_available_abilities(units) if units else []
        self._abilities = {u.tag: set(a) for u, a in zip(units, abilities)}
        readiness = await self.get_available_abilities(units, ignore_resource_requirements=True) if units else []
        self._abilities_ignoring_cost = {u.tag: set(a) for u, a in zip(units, readiness)}
        self._build_worker_for_catalog = workers.first if workers else None
        return self._build_worker_for_catalog

    def _train_reason(self, unit_type, ignore_resources=False):
        if not ignore_resources and not self.can_afford(unit_type):
            return "resources_or_supply"
        limit = UNIT_LIMITS.get(unit_type.name)
        if limit is not None and self._count_with_pending(unit_type) >= limit:
            return "unit_policy_limit"
        for producer in self._producers:
            if producer.type_id not in UNIT_TRAINED_FROM.get(unit_type, set()) or not producer.is_idle:
                continue
            info = TRAIN_INFO[producer.type_id][unit_type]
            if info.get("requires_power") and not producer.is_powered:
                continue
            if self._has_ability(producer, info["ability"], ignore_resources=ignore_resources):
                return None
        return "no_ready_producer_with_available_ability"

    def _build_reason(self, unit_type, worker, ignore_resources=False):
        if not worker:
            return "no_available_builder"
        if not ignore_resources and not self.can_afford(unit_type):
            return "building_resources"
        info = TRAIN_INFO[U.PROBE].get(unit_type)
        if not info or not self._has_ability(worker, info["ability"], ignore_resources=ignore_resources):
            return "missing_build_ability_or_technology"
        if self.already_pending(unit_type) >= (3 if unit_type == U.PYLON else 1):
            return "already_pending"
        limit = BUILDING_LIMITS.get(unit_type.name)
        if limit is not None and self._count_with_pending(unit_type) >= limit:
            return "building_policy_limit"
        if unit_type == U.PYLON:
            forecast = self._supply_forecast()
            if not forecast["needs_supply"] and not forecast["needs_power"]:
                return "supply_and_power_sufficient"
        elif unit_type == U.ASSIMILATOR:
            if not self._assimilator_candidates():
                return "no_free_geyser_at_ready_base"
        elif unit_type != U.NEXUS:
            if not self.structures(U.PYLON).ready:
                return "no_ready_pylon"
        return None

    def _research_reason(self, upgrade, ignore_resources=False):
        data = self.game_data.upgrades.get(upgrade.value)
        # SC2 retains obsolete upgrade records with no research ability. The SDK's
        # pending-upgrade query dereferences that ability, so check it first.
        if data is None or data.research_ability is None:
            return "upgrade_not_in_game_version"
        if self.already_pending_upgrade(upgrade) or (not ignore_resources and not self.can_afford(upgrade)):
            return "research_started_or_resources"
        source = UPGRADE_RESEARCHED_FROM.get(upgrade)
        info = RESEARCH_INFO.get(source, {}).get(upgrade)
        if not info:
            return "unknown_research_ability"
        for producer in self._producers:
            if producer.type_id == source and producer.is_idle and self._has_ability(producer, info["ability"], ignore_resources=ignore_resources):
                return None
        return "no_ready_researcher_with_available_ability"

    async def available_actions(self):
        worker = await self._refresh_abilities()
        choices, blocked = {}, {}
        for action, description in self.action_dict.items():
            reason = None
            if self.cooldowns.get(action, 0) > self.time:
                reason = "recent_executor_rejection"
            elif action <= 17:
                reason = self._train_reason(U[description.split()[1]])
            elif action == 18:
                if not self._archon_pair():
                    reason = "need_two_available_templars"
            elif action <= 33:
                reason = self._build_reason(U[description.split()[1]], worker)
            elif action <= 59:
                reason = self._research_reason(UpgradeId[description.split()[1]])
            elif action <= 63:
                if not self._scout_candidates(U[description.split()[1]]):
                    reason = "no_available_scout_keep_army_detector"
                elif self.time - self._last_scout_order < 15:
                    reason = "scout_order_cooldown"
            elif action == 64:
                retarget = self._planned_target() != self._army_target_id
                if not self._combat_units() or self._ready_army_supply() < 10 or (self.army_intent == "attack" and not retarget):
                    reason = "need_army_or_already_attacking"
            elif action == 65:
                if not self._combat_units() or self.army_intent == "retreat":
                    reason = "no_army_or_already_retreating"
            elif action == DEFEND_ACTION:
                if not self._combat_units() or self.army_intent == "defend":
                    reason = "no_army_or_already_defending"
            elif action <= 70:
                target_type = U[description.split()[1]]
                targets = self.structures(target_type).ready.filter(
                    lambda u: not u.is_idle and not u.has_buff(BuffId.CHRONOBOOSTENERGYCOST))
                if not targets or not any(self._has_ability(n, AbilityId.EFFECT_CHRONOBOOSTENERGYCOST)
                                          for n in self.townhalls.ready):
                    reason = "no_chronoboost_target_or_energy"
            if reason:
                blocked[str(action)] = reason
            else:
                choices[action] = description
        return choices, blocked

    async def _execute_decision(self, decision):
        choices, blocked = await self.available_actions()
        action = decision.action_id
        context_rejection = self._decision_context_rejection(decision)
        if context_rejection or action not in choices:
            self._action_stats["invalidated_before_execution"] += 1
            if context_rejection:
                self._action_stats["plan_changed_discards"] += 1
            self.log("action_discarded", request_id=decision.request_id, action_id=action,
                     plan_id=decision.plan_id, execution_plan_id=self._active_plan_id(),
                     game_loop=self.state.game_loop, reason=context_rejection or blocked.get(str(action), "unavailable"))
            return
        self.temp_failure_list.clear()
        count_before = len(self.actions)
        if action <= 17:
            await self._train_one(action, U[self.action_dict[action].split()[1]])
        elif 19 <= action <= 33:
            await self._build_one(action, U[self.action_dict[action].split()[1]])
        elif 34 <= action <= 59:
            self._research_one(action, UpgradeId[self.action_dict[action].split()[1]])
        elif 60 <= action <= 63:
            await self._start_scout(action, U[self.action_dict[action].split()[1]])
        else:
            await getattr(self, f"handle_action_{action}")()
        submitted = self.actions[count_before:]
        self._track_production_order(decision, submitted)
        outcome = {
            "game_loop": self.state.game_loop, "action_id": action,
            "description": self.action_dict[action], "orders_submitted": len(submitted),
            "failures": list(self.temp_failure_list),
            "intent_applied": action in ARMY_ACTIONS and self.army_intent == ARMY_ACTIONS[action],
        }
        self.recent_outcomes.append(outcome)
        self._action_stats["decisions_executed"] += 1
        self._action_stats["orders_submitted"] += len(submitted)
        if action != self.empty_action and not submitted and not outcome["intent_applied"]:
            self.cooldowns[action] = self.time + 5
            self._action_stats["executor_no_order"] += 1
        self.log("action", request_id=decision.request_id, **outcome,
                 plan_id=decision.plan_id, execution_plan_id=self._active_plan_id(),
                 status="wait" if action == self.empty_action else "intent_applied" if outcome["intent_applied"] else "executor_no_order" if not submitted else
                        "orders_submitted_with_failures" if outcome["failures"] else "orders_submitted",
                 orders=[self._order_record(c) for c in submitted])

    def _active_plan_id(self):
        return None

    def _decision_context_rejection(self, decision):
        return None

    @staticmethod
    def _order_record(command):
        target = getattr(command, "target", None)
        if hasattr(target, "x") and hasattr(target, "y"):
            target = {"x": float(target.x), "y": float(target.y)}
        elif target is not None:
            target = int(getattr(target, "tag", target))
        return {"ability": command.ability.name, "unit_tag": command.unit.tag,
                "target": target, "queue": bool(getattr(command, "queue", False))}

    async def on_step(self, iteration):
        try:
            await self._step(iteration)
        except Exception as exc:
            if self.log.failure is None:
                self.log.fail(exc)
            raise

    async def _step(self, iteration):
        started = time.monotonic()
        self.log.set_game_loop(self.state.game_loop)
        self.iteration = iteration
        self._steps += 1
        self._last_game_loop = self.state.game_loop
        self._update_navigation()
        self._consume_engine_feedback()
        if self.scheduler.task is not None and not self.scheduler.task.done():
            self._steps_while_inflight += 1
        # Update the legacy counters used by handlers before dispatch.
        self._last_economy = self.get_information()["resource"]
        decision = self.scheduler.poll(self.state.game_loop)
        # Let the runner mark this as an interrupted/failed trial and close both
        # model clients. A disabled decision service cannot play a valid game.
        self.scheduler.require_service()
        if decision is not None:
            await self._execute_decision(decision)
        # Give production/movement orders a frame to become visible before the next snapshot.
        elif self.scheduler.ready:
            choices, blocked = await self.available_actions()
            if len(choices) > 1:
                self.scheduler.submit(self.state.game_loop, self._snapshot(), choices)
                self.log("action_mask", request_id=self.scheduler.request_id, blocked=blocked)
        if self.time - self._last_maintenance >= 1:
            await self._maintain_local_behaviors()
            self._last_maintenance = self.time
        self._max_step_ms = max(self._max_step_ms, (time.monotonic() - started) * 1000)
        if self.time - self._last_heartbeat >= 5:
            self.log("heartbeat", game_loop=self.state.game_loop, steps=self._steps,
                     steps_while_inflight=self._steps_while_inflight,
                     inflight=self.scheduler.task is not None, economy=self._last_economy,
                     units=dict(Counter(u.type_id.name for u in self.units)),
                     structures=dict(Counter(u.type_id.name for u in self.structures)),
                     stats=dict(self.scheduler.stats), actions=dict(self._action_stats))
            self._last_heartbeat = self.time

    def _combat_units(self):
        reserved = self._morph_reserved_tags()
        return self.units.of_type(self.military_unit_types).ready.filter(
            lambda u: (u.can_attack or u.type_id in {U.CARRIER, U.SENTRY})
            and u.type_id not in {U.ORACLE, U.DISRUPTOR, U.WARPPRISM, U.OBSERVER}
            and u.tag not in reserved)

    async def _maintain_local_behaviors(self):
        # Do not replace a fresh construction/scouting order using this frame's old worker state.
        if not any(command.unit.type_id == U.PROBE for command in self.actions):
            workers = self.workers
            self.workers = workers.filter(lambda u: u.tag not in self._scouts)
            try:
                await self.distribute_workers(resource_ratio=4 if self.minerals < 150 and self.vespene > 250 else 2)
            finally:
                self.workers = workers
        if self.already_pending_upgrade(UpgradeId.WARPGATERESEARCH) == 1:
            gateways = self.structures(U.GATEWAY).ready.idle.filter(
                lambda u: u.tag not in self.unit_tags_received_action)
            if gateways:
                for gate, abilities in zip(gateways, await self.get_available_abilities(gateways)):
                    if AbilityId.MORPH_WARPGATE in abilities:
                        gate(AbilityId.MORPH_WARPGATE)
        self._issue_army_intent()
        self._maintain_scouts_and_detection()

    async def produce_bg_unit(self, action_id, unit_type, required_buildings=None):
        # Continue using ordinary Gateways while researched gates are morphing.
        gates = self.structures(U.GATEWAY).ready.idle
        ability = TRAIN_INFO[U.GATEWAY][unit_type]["ability"]
        for gate in gates:
            if self._has_ability(gate, ability):
                gate.train(unit_type)
                return
        pylons = self.structures(U.PYLON).ready
        if pylons:
            await self.warp_unit(action_id, unit_type, pylons.closest_to(self.start_location).position)
        else:
            self.record_failure(action_id, "No ready pylon for warp-in")

    async def warp_unit(self, action_id, unit_type, initial_reference_point,
                        required_buildings=None, max_attempts=5):
        ability = TRAIN_INFO[U.WARPGATE][unit_type]["ability"]
        self._warp_reservations = [(p, t) for p, t in self._warp_reservations if self.time - t < 10]
        references = [initial_reference_point]
        references += [p.position for p in sorted(self.structures(U.PYLON).ready,
                       key=lambda p: p.distance_to(initial_reference_point)) if p.distance_to(initial_reference_point) > 4][:3]
        for gate in self.structures(U.WARPGATE).ready.idle:
            if not self._has_ability(gate, ability):
                continue
            for reference in references:
                for distance in range(1, max_attempts + 1):
                    pos = reference.random_on_distance(distance)
                    placement = await self.find_placement(ability, pos, max_distance=6, placement_step=1)
                    if not placement or placement.distance_to(reference) > 6:
                        continue
                    if any(placement.distance_to(p) < 1.5 for p, _ in self._warp_reservations):
                        continue
                    if any(u.distance_to(placement) < getattr(u, "radius", .5) + .7 for u in self.units):
                        continue
                    gate.warp_in(unit_type, placement)
                    self._warp_reservations.append((placement, self.time))
                    return
        self.record_failure(action_id, "No available warpgate or warp-in placement")

    async def apply_chronoboost(self, action_id, target_type, supply_requirement, description):
        targets = self.structures(target_type).ready.filter(
            lambda u: not u.is_idle and not u.has_buff(BuffId.CHRONOBOOSTENERGYCOST))
        for nexus in self.townhalls.ready:
            if targets and self._has_ability(nexus, AbilityId.EFFECT_CHRONOBOOSTENERGYCOST):
                nexus(AbilityId.EFFECT_CHRONOBOOSTENERGYCOST, targets.first)
                return
        self.record_failure(action_id, "No eligible chronoboost target or Nexus")

    async def handle_action_18(self):
        pair = self._archon_pair()
        if not pair:
            self.record_failure(18, "need_two_available_templars")
            return
        # SC2 97563 omits this multi-unit ability from single-unit queries.
        # The controlled engine probe verifies the grouped two-tag command.
        for unit in pair:
            unit(AbilityId.MORPH_ARCHON)

    async def handle_action_64(self):
        self._set_army_intent("attack")

    async def handle_action_65(self):
        self._set_army_intent("retreat")

    async def handle_action_72(self):
        self._set_army_intent("defend")

    async def on_end(self, game_result):
        await self.shutdown(game_result.name)

    def _extra_summary(self):
        return {}

    async def shutdown(self, result="interrupted"):
        if self._closed:
            return
        self._closed = True
        await self.scheduler.close()
        if self._original_client_actions is not None:
            self.client.actions = self._original_client_actions
            self._original_client_actions = None
        latencies = self.scheduler.latencies
        summary = {
            "result": result, "realtime": True, "game_loop": self._last_game_loop,
            "game_seconds": self._last_game_loop / 22.4,
            "wall_seconds": time.monotonic() - self._started_at,
            "steps": self._steps, "steps_while_inflight": self._steps_while_inflight,
            "max_on_step_ms": self._max_step_ms, "economy": self._last_economy,
            "api": dict(self.scheduler.stats), "actions": dict(self._action_stats),
            "macro_contract": VERSION, "production_orders_unresolved": list(self._production_orders.values()),
            "latency_median_ms": statistics.median(latencies) if latencies else None,
            "latency_max_ms": max(latencies) if latencies else None,
            "note": "Submitted commands are not proof of completed production. Check heartbeat observations/replay.",
        }
        summary.update(self._extra_summary())
        self.log("end", **summary)
        self.log.write_summary(summary)
        if self._owns_log:
            self.log.finish("interrupted" if result == "interrupted" else "completed", result)
            self.log.close()
