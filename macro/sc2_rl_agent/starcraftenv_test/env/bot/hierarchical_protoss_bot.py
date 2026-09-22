"""Astra strategy + Jev immediate macro choices + the original SC2 executor."""

import statistics
import time
from collections import Counter, deque

from sc2.dicts.unit_train_build_abilities import TRAIN_INFO
from sc2.dicts.unit_trained_from import UNIT_TRAINED_FROM
from sc2.dicts.unit_research_abilities import RESEARCH_INFO
from sc2.dicts.upgrade_researched_from import UPGRADE_RESEARCHED_FROM
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId

from ...agent.astra_planner import StrategicPlanner
from ...agent.macro_contract import ACTION_LIMITS, ARMY_ACTIONS, primary_action
from ...agent.strategic_policy import plan_progress, policy_reason
from .jev_protoss_bot import JevProtossBot


class HierarchicalProtossBot(JevProtossBot):
    def __init__(self, *args, planner_client, planner_interval=60, plan_ttl=180,
                 max_plan_age=60, max_planner_requests=80, planner_min_interval=10,
                 planner_event_cooldown=30, planner_execution_window=30, **kwargs):
        super().__init__(*args, **kwargs)
        self.planner = StrategicPlanner(planner_client, self.log, planner_interval, plan_ttl,
                                        max_plan_age, max_planner_requests, min_interval=planner_min_interval,
                                        event_cooldown=planner_event_cooldown,
                                        execution_window=planner_execution_window)
        self._last_strategy_tick = -100
        self._last_intent_time = -100
        self._last_plan_id = None
        self._completed_goals = set()
        self._failure_notifications = {}
        self._samples = deque(maxlen=61)
        self._enemy_memory = {}
        self._last_threat = False
        self._last_ready_bases = None
        self._depleted_bases = set()
        self._last_army_loss = False
        self._seen_enemy_types = set()
        self._last_legal_blocked = {}
        self._planner_steps_inflight = 0
        self._catalog_cache = {}
        self._policy_blocks = Counter()
        self._hybrid_closed = False
        self._execution_directive = None
        self._acknowledged_priorities = set()

    async def on_start(self):
        await super().on_start()
        self.log("planner_start", backend="codex_exec", model=self.planner.client.model,
                 interval_game_seconds=self.planner.interval, ttl_game_seconds=self.planner.ttl,
                 max_plan_age_seconds=self.planner.max_age,
                 min_request_interval_wall_seconds=self.planner.min_interval,
                 event_cooldown_wall_seconds=self.planner.event_cooldown,
                 execution_window_game_seconds=self.planner.execution_window,
                 timeout_wall_seconds=self.planner.client.timeout,
                 reasoning_effort=self.planner.client.effort)

    def _catalog(self):
        catalog = {}
        for action, description in self.action_dict.items():
            minerals = gas = count = 0
            requirements = []
            if action <= 33:
                kind = U.ARCHON if action == 18 else U[description.split()[1]]
                cost = self.calculate_cost(kind)
                minerals, gas = cost.minerals, cost.vespene
                if action == 18:
                    minerals = gas = 0  # Merging spends templars, not banked resources.
                count = self._count_with_pending(kind)
                if action == 18:
                    requirements = [{"consumes_two_templars": ["HIGHTEMPLAR", "DARKTEMPLAR"]}]
                else:
                    for source in sorted(UNIT_TRAINED_FROM.get(kind, set()), key=lambda u: u.name):
                        info = TRAIN_INFO.get(source, {}).get(kind, {})
                        requirements.append({"producer": source.name, **{
                            k: getattr(v, "name", v) for k, v in info.items() if k != "ability"}})
            elif action <= 59:
                upgrade = UpgradeId[description.split()[1]]
                data = self.game_data.upgrades.get(upgrade.value)
                if data is not None and data.research_ability is not None:
                    cost = self.calculate_cost(upgrade)
                    minerals, gas = cost.minerals, cost.vespene
                    count = int(self.already_pending_upgrade(upgrade) > 0)
                source = UPGRADE_RESEARCHED_FROM.get(upgrade)
                if source is not None:
                    info = RESEARCH_INFO.get(source, {}).get(upgrade, {})
                    requirements = [{"producer": source.name, **{
                        k: getattr(v, "name", v) for k, v in info.items() if k != "ability"}}]
            catalog[str(action)] = {"description": description, "cost": {"minerals": minerals, "gas": gas},
                                    "count_with_pending": count,
                                    "target_limit": ACTION_LIMITS.get(action),
                                    "production_alternatives": requirements,
                                    "last_legality_rejection": self._last_legal_blocked.get(str(action))}
            reservation_blocked = None
            if action <= 17:
                reservation_blocked = self._train_reason(kind, ignore_resources=True)
            elif 19 <= action <= 33:
                reservation_blocked = self._build_reason(kind, self._build_worker_for_catalog, ignore_resources=True)
            elif 34 <= action <= 59:
                reservation_blocked = self._research_reason(upgrade, ignore_resources=True)
            catalog[str(action)]["reservation_blocked"] = reservation_blocked
            if 34 <= action <= 59:
                catalog[str(action)]["exists_in_game_version"] = data is not None and data.research_ability is not None
        return catalog

    def _strategy_snapshot(self):
        state = super()._snapshot()
        state["execution_directive"] = self._execution_directive
        score = self.state.score
        state["strategic_metrics"] = {
            "mineral_income_per_minute": round(score.collection_rate_minerals, 1),
            "gas_income_per_minute": round(score.collection_rate_vespene, 1),
            "idle_workers": self.workers.idle.amount,
            "pending_workers": self.already_pending(U.PROBE),
            "pending_bases": self.already_pending(U.NEXUS),
            "base_under_attack": self._emergency(),
            "army_units_near_home": sum(any(u.distance_to(b) < 30 for b in self.townhalls)
                                        for u in self._combat_units()),
            "base_resources": [{
                "tag": b.tag, "ready": b.is_ready,
                "mineral_workers": b.assigned_harvesters, "ideal_mineral_workers": b.ideal_harvesters,
                "mineral_patches": self.mineral_field.closer_than(12, b).amount,
                "minerals_remaining": sum(m.mineral_contents for m in self.mineral_field.closer_than(12, b)),
                "gas_workers": sum(g.assigned_harvesters for g in self.gas_buildings.closer_than(12, b)),
                "ideal_gas_workers": sum(g.ideal_harvesters for g in self.gas_buildings.ready.closer_than(12, b)),
            } for b in self.townhalls],
        }
        for enemy in list(self.enemy_units) + list(self.enemy_structures):
            if enemy.is_visible:
                self._enemy_memory[enemy.tag] = {"type": enemy.type_id.name,
                                                "position": list(enemy.position),
                                                "last_seen_game_seconds": round(self.time, 1)}
        self._enemy_memory = {tag: e for tag, e in self._enemy_memory.items()
                              if self.time - e["last_seen_game_seconds"] <= 120}
        historical = {}
        for enemy in self._enemy_memory.values():
            item = historical.setdefault(enemy["type"], {"tags_seen": 0, "last_seen_game_seconds": 0})
            item["tags_seen"] += 1
            item["last_seen_game_seconds"] = max(item["last_seen_game_seconds"], enemy["last_seen_game_seconds"])
        state["enemy_last_seen_120s"] = historical
        state["enemy_memory_note"] = "Historical sightings may include dead units; they are not current enemy counts."
        state["recent_economy"] = list(self._samples)[::10]
        return state

    def _snapshot(self):
        state = super()._snapshot()
        plan = self.planner.active
        if plan is not None and self.time < plan["expires_game_seconds"]:
            catalog = self._catalog()
            state["strategic_plan"] = {**plan, **plan_progress(plan, catalog, state["resource"])}
        else:
            state["strategic_plan"] = None
        state["base_under_attack"] = self._emergency()
        directive = self._execution_directive
        state["execution_directive"] = directive if directive and directive["plan_id"] == self._active_plan_id() else None
        state["strategy_status"] = {"mode": "astra_plan" if state["strategic_plan"] else "jev_fallback",
                                    "planner_inflight": self.planner.task is not None}
        return state

    async def available_actions(self):
        choices, blocked = await super().available_actions()
        # Ability queries await SC2; a new Astra result may have arrived during that wait.
        self.planner.poll(self.state.game_loop)
        self._last_legal_blocked = dict(blocked)
        plan = self.planner.active
        if plan is None or self.time >= plan["expires_game_seconds"]:
            self._execution_directive = None
            return choices, blocked
        resource = self.get_information()["resource"]
        catalog = self._catalog()
        emergency = self._emergency()
        for action in list(choices):
            reason = policy_reason(action, plan, catalog, resource, self.army_intent,
                                   self._last_intent_time, self.time, emergency)
            if reason:
                del choices[action]
                blocked[str(action)] = reason
                self._policy_blocks[reason] += 1
        self._acknowledged_priorities = {key for key in self._acknowledged_priorities if key[0] == plan["plan_id"]}
        action, reason = primary_action(plan, choices, self.army_intent, resource["ready_army_supply"],
                                         target_changed=self._planned_target() != self._army_target_id,
                                         acknowledged_actions={key[1] for key in self._acknowledged_priorities})
        if action is not None:
            key = (plan["plan_id"], action)
            previous = self._execution_directive
            if previous is None or key != (previous["plan_id"], previous["action_id"]):
                self._execution_directive = {"plan_id": plan["plan_id"], "action_id": action,
                    "description": self.action_dict[action], "reason": reason,
                    "ready_game_seconds": self.time, "ack_deadline_game_seconds": self.time + 2,
                    "acknowledged": False, "delay_reported": False}
                self.log("priority_order_ready", game_loop=self.state.game_loop, **self._execution_directive)
            elif not previous["acknowledged"] and not previous["delay_reported"] and self.time > previous["ack_deadline_game_seconds"]:
                previous["delay_reported"] = True
                self.log("priority_order_delayed", game_loop=self.state.game_loop, **previous)
            if action in ARMY_ACTIONS and not self._execution_directive["acknowledged"]:
                choices[action] += " | MAIN ORDER: apply army posture now; unfinished production is not a prerequisite"
        else:
            self._execution_directive = None
        return choices, blocked

    async def _execute_decision(self, decision):
        previous = self.army_intent
        await super()._execute_decision(decision)
        if self.army_intent != previous:
            self._last_intent_time = self.time
            self.log("army_intent_changed", game_loop=self.state.game_loop, previous=previous,
                     intent=self.army_intent, plan_id=self.planner.active["plan_id"] if self.planner.active else None)
        if self.recent_outcomes and self.recent_outcomes[-1]["game_loop"] == self.state.game_loop:
            outcome = self.recent_outcomes[-1]
            self._notify_execution_outcome(outcome)
            directive = self._execution_directive
            if (directive and not directive["acknowledged"] and decision.action_id == directive["action_id"]
                    and (outcome["orders_submitted"] or outcome.get("intent_applied"))):
                directive["acknowledged"] = True
                self._acknowledged_priorities.add((directive["plan_id"], decision.action_id))
                self.log("priority_order_acknowledged", game_loop=self.state.game_loop,
                         request_id=decision.request_id, plan_id=directive["plan_id"], action_id=decision.action_id,
                         delay_game_seconds=self.time - directive["ready_game_seconds"],
                         acknowledgement="intent_applied" if outcome.get("intent_applied") else "order_submitted")

    def _notify_execution_outcome(self, outcome):
        action = outcome["action_id"]
        if action == self.empty_action:
            return
        if outcome["orders_submitted"] or outcome.get("intent_applied"):
            self._failure_notifications.pop(action, None)
            return
        signature = tuple(str(f) for f in outcome.get("failures", []))
        previous = self._failure_notifications.get(action)
        if previous and previous[0] == signature and self.time - previous[1] < 180:
            self.planner.stats["duplicate_failures_suppressed"] += 1
            return
        self._failure_notifications[action] = (signature, self.time)
        self.planner.trigger("execution_feedback_failure" if outcome.get("phase") in {"failed", "unconfirmed"}
                             else "executor_no_order", action_id=action, failures=list(signature),
                             phase=outcome.get("phase", "no_order"))

    def _check_plan_progress(self, state, catalog):
        plan = self.planner.active
        if plan is None:
            return
        if plan["plan_id"] != self._last_plan_id:
            self._last_plan_id = plan["plan_id"]
            counts = plan.get("goal_counts_at_observation", {})
            self._completed_goals = {g["action_id"] for g in plan["goals"]
                                     if counts.get(str(g["action_id"]), 0) >= g["target"]}
        progress = plan_progress(plan, catalog, state["resource"])
        remaining = {g["action_id"] for g in progress["remaining_goals"]}
        completed = {g["action_id"] for g in plan["goals"]} - remaining
        newly_completed = completed - self._completed_goals
        if newly_completed:
            # Counts include pending orders: these are committed targets, not finished construction.
            milestone = any(21 <= a <= 31 or 34 <= a <= 59 for a in newly_completed)
            refresh = milestone or not remaining
            self.log("plan_progress", plan_id=plan["plan_id"], game_loop=self.state.game_loop,
                     newly_completed=sorted(newly_completed), progress=progress,
                     requests_refresh=refresh, counts_include_pending=True)
            if refresh:
                self.planner.trigger("goal_completed", plan_id=plan["plan_id"],
                                     newly_completed=sorted(newly_completed), phase_complete=not remaining)
        self._completed_goals |= completed

    async def on_step(self, iteration):
        try:
            await self._hierarchical_step(iteration)
        except Exception as exc:
            if self.log.failure is None:
                self.log.fail(exc)
            raise

    async def _hierarchical_step(self, iteration):
        started = time.monotonic()
        self.log.set_game_loop(self.state.game_loop)
        self.iteration = iteration
        self._update_navigation()
        self.planner.poll(self.state.game_loop)
        if self.planner.task is not None and not self.planner.task.done():
            self._planner_steps_inflight += 1
        if self.time - self._last_strategy_tick >= 1:
            state = self._strategy_snapshot()
            emergency = state["strategic_metrics"]["base_under_attack"]
            if emergency and not self._last_threat:
                self.planner.trigger("base_attacked")
            ready_bases = self.townhalls.ready.amount
            if self._last_ready_bases is None:
                self.planner.trigger("opening")
            elif ready_bases != self._last_ready_bases:
                self.planner.trigger("base_lost" if ready_bases < self._last_ready_bases else "base_count_changed",
                                     previous=self._last_ready_bases, current=ready_bases)
            depleted = {b["tag"] for b in state["strategic_metrics"]["base_resources"]
                        if b["ready"] and b["minerals_remaining"] < 1500}
            if depleted - self._depleted_bases:
                self.planner.trigger("mineral_depletion", base_tags=sorted(depleted - self._depleted_bases))
            self._depleted_bases = depleted
            enemy_types = set(state["enemy_last_seen_120s"])
            new_types = enemy_types - self._seen_enemy_types
            significant = {t for t in new_types if not t.startswith("CHANGELING")
                           and t not in {"LARVA", "EGG", "BROODLING", "OVERLORD", "SCV", "PROBE", "DRONE"}}
            critical = significant & {"MUTALISK", "CORRUPTOR", "BROODLORD", "LURKERMP", "LURKERMPBURROWED",
                                      "ULTRALISK", "RAVAGER", "HYDRALISK", "VIPER", "INFESTOR",
                                      "BANSHEE", "BATTLECRUISER", "SIEGETANK", "SIEGETANKSIEGED",
                                      "LIBERATOR", "LIBERATORAG", "GHOST", "DARKTEMPLAR", "COLOSSUS",
                                      "CARRIER", "TEMPEST", "MOTHERSHIP"}
            if significant:
                self.planner.trigger("new_enemy_threat" if critical else "new_enemy_composition",
                                     types=sorted(significant))
            self._seen_enemy_types |= enemy_types
            peak_army = max((s.get("ready_army_supply", s["army_supply"]) for s in self._samples
                             if self.time - s["game_seconds"] <= 30), default=0)
            current_army = state["resource"].get("ready_army_supply", state["resource"]["army_supply"])
            army_loss = peak_army - current_army >= 8 and current_army < peak_army * .7
            if army_loss and not self._last_army_loss:
                self.planner.trigger("army_losses", peak_army_30s=peak_army, current_army=current_army)
            self._last_army_loss = army_loss
            self._last_threat, self._last_ready_bases = emergency, ready_bases
            self._samples.append({"game_seconds": round(self.time, 1), **state["resource"]})
            self._catalog_cache = self._catalog()
            self.planner.tick(self.state.game_loop, state, self._catalog_cache)
            self._check_plan_progress(state, self._catalog_cache)
            self._last_strategy_tick = self.time
        await super().on_step(iteration)
        self._max_step_ms = max(self._max_step_ms, (time.monotonic() - started) * 1000)

    def _active_plan_id(self):
        plan = self.planner.active
        return plan["plan_id"] if plan and self.time < plan["expires_game_seconds"] else None

    def _decision_context_rejection(self, decision):
        if decision.plan_id != self._active_plan_id():
            return "plan_changed_during_inference"
        return None

    def _extra_summary(self):
        return {"planner": self._planner_summary}

    async def shutdown(self, result="interrupted"):
        if self._hybrid_closed:
            return
        self._hybrid_closed = True
        await self.planner.close()
        self._planner_summary = {"backend": "codex_exec", "model": self.planner.client.model,
                           "stats": dict(self.planner.stats), "steps_while_inflight": self._planner_steps_inflight,
                           "latency_median_ms": statistics.median(self.planner.latencies) if self.planner.latencies else None,
                           "latency_max_ms": max(self.planner.latencies, default=None),
                           "policy_blocks": dict(self._policy_blocks), "last_plan": self.planner.active}
        self.log("planner_end", **self._planner_summary)
        await super().shutdown(result)
