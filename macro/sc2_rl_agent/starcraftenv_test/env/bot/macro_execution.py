"""SC2-checked production and observations, with order lifecycle feedback."""

from sc2.dicts.unit_train_build_abilities import TRAIN_INFO
from sc2.dicts.unit_trained_from import UNIT_TRAINED_FROM
from sc2.dicts.unit_research_abilities import RESEARCH_INFO
from sc2.dicts.upgrade_researched_from import UPGRADE_RESEARCHED_FROM
from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.upgrade_id import UpgradeId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2
from sc2.action import combine_actions
from sc2.data import ActionResult


class MacroExecution:
    def _initialize_execution(self):
        self._production_orders = {}
        self._recent_engine_errors = {}
        self._placement_attempts = {}
        self._original_client_actions = None
        self._warp_reservations = []

    def _install_action_feedback(self):
        """Observe this bot's client only; preserve BurnySC2's command grouping."""
        original = self.client.actions
        self._original_client_actions = original

        async def actions_with_feedback(actions, return_successes=False):
            if not actions:
                return await original(actions, return_successes=return_successes)
            commands = actions if isinstance(actions, list) else [actions]
            groups = list(combine_actions(commands))
            results = await original(commands, return_successes=True)
            if results is not None and len(results) == len(groups):
                for group, result in zip(groups, results):
                    command = group.unit_command
                    matches = [o for o in self._production_orders.values() if o["phase"] == "submitted"
                               and o["producer_tag"] in command.unit_tags and o["ability_id"] == command.ability_id]
                    for order in matches:
                        self._production_event(order, "accepted" if result == ActionResult.Success else "failed",
                                               engine_result=result.name, source="action_response")
                        if result != ActionResult.Success:
                            self.cooldowns[order["action_id"]] = self.time + 5
            else:
                self.log("engine_action_response_unconfirmed", game_loop=self.state.game_loop,
                         submitted_groups=len(groups), response_count=len(results or []))
            if return_successes or results is None:
                return results
            return [r for r in results if r != ActionResult.Success]

        self.client.actions = actions_with_feedback

    def get_information(self):
        information = super().get_information()
        self.worker_supply = int(self.supply_workers)
        information["resource"]["worker_supply"] = self.worker_supply
        information["resource"]["visible_workers"] = self.workers.amount
        information["resource"]["pending_workers"] = self.already_pending(U.PROBE)
        information["resource"]["ready_army_supply"] = self._ready_army_supply()
        forecast = self._supply_forecast()
        information["resource"].update(needs_supply=forecast["needs_supply"], needs_power=forecast["needs_power"])
        information["unit"]["probe_count"] = self.worker_supply
        return information

    def _ready_army_supply(self):
        return sum(self.calculate_supply_cost(u.type_id) for u in self._combat_units().ready)

    def _count_with_pending(self, kind):
        if kind == U.ARCHON:
            # Includes the appearing, unfinished Archon and pairs still approaching.
            return self.units(kind).amount + sum(o["kind"] == "ARCHON" and o["unit_tag"] is None
                                                 for o in self._production_orders.values())
        if kind == U.PROBE:
            owned = self.supply_workers
        elif kind in TRAIN_INFO[U.PROBE]:
            owned = self.structures(kind).ready.amount
            if kind == U.GATEWAY:
                owned += self.structures(U.WARPGATE).amount
        else:
            owned = self.units(kind).ready.amount
        return owned + self.already_pending(kind)

    def _assimilator_candidates(self):
        return self.vespene_geyser.filter(lambda g:
            any(b.distance_to(g) < 10 for b in self.townhalls.ready)
            and not self.gas_buildings.closer_than(2, g))

    def _morph_reserved_tags(self):
        return {tag for order in self._production_orders.values() if order["kind"] == "ARCHON"
                for tag in order.get("producer_tags", [order["producer_tag"]])}

    def _archon_pair(self):
        reserved = self._morph_reserved_tags() | self.unit_tags_received_action | set(self._scouts)
        candidates = list(self.units.of_type({U.HIGHTEMPLAR, U.DARKTEMPLAR}).ready.filter(
            lambda u: u.tag not in reserved and not getattr(u, "is_hallucination", False)
            and not any(o.ability.id == AbilityId.MORPH_ARCHON for o in getattr(u, "orders", []))))
        pairs = [(a, b) for i, a in enumerate(candidates) for b in candidates[i + 1:]]
        return min(pairs, key=lambda pair: (pair[0].distance_to(pair[1]),
                   pair[0].energy + pair[1].energy), default=None)

    def _research_one(self, action, upgrade):
        source = UPGRADE_RESEARCHED_FROM.get(upgrade)
        info = RESEARCH_INFO.get(source, {}).get(upgrade)
        for producer in self._producers:
            if info and producer.type_id == source and producer.is_idle and self._has_ability(producer, info["ability"]):
                producer.research(upgrade)
                return
        self.record_failure(action, "no_ready_researcher_with_available_ability")

    def _supply_forecast(self):
        """One upcoming production cycle; do not double-count reserved training supply."""
        production = self.structures.of_type({U.GATEWAY, U.WARPGATE, U.ROBOTICSFACILITY, U.STARGATE}).ready.amount
        desired_headroom = min(28, max(4, self.townhalls.ready.amount + production * 3))
        pending_pylons = self.already_pending(U.PYLON)
        unpowered = self.structures.ready.filter(lambda b: b.type_id not in {U.NEXUS, U.PYLON, U.ASSIMILATOR}
                                                and not b.is_powered)
        return {"headroom_target": desired_headroom, "pending_pylons": pending_pylons,
                "needs_supply": self.supply_cap < 200 and self.supply_left + 8 * pending_pylons <= desired_headroom,
                "unpowered_buildings": unpowered.amount,
                "needs_power": bool(unpowered) and pending_pylons == 0}

    async def _train_one(self, action, kind):
        for producer in self._producers:
            if producer.type_id not in UNIT_TRAINED_FROM.get(kind, set()) or not producer.is_idle:
                continue
            info = TRAIN_INFO[producer.type_id][kind]
            if info.get("requires_power") and not producer.is_powered:
                continue
            if not self._has_ability(producer, info["ability"]):
                continue
            if producer.type_id == U.WARPGATE:
                pylons = self.structures(U.PYLON).ready
                if pylons:
                    await self.warp_unit(action, kind, pylons.closest_to(self._defense_position()).position)
                    return
            else:
                producer.train(kind)
                return
        self.record_failure(action, "no_ready_producer_with_available_ability")

    async def _build_one(self, action, kind):
        if kind == U.NEXUS:
            candidates = sorted(self.expansion_locations_list, key=lambda p: p.distance_to(self.start_location))
            for position in candidates:
                if any(b.distance_to(position) < 8 for b in self.townhalls):
                    continue
                if any(e.distance_to(position) < 18 for e in self.enemy_units if e.is_visible and e.can_attack):
                    continue
                if any(Point2(e["position"]).distance_to(position) < 10 for e in self._known_enemy_buildings.values()):
                    continue
                worker = self.select_build_worker(position)
                if worker is None or await self.client.query_pathing(worker.position, position) is None:
                    continue
                if await self.build(kind, position, max_distance=2, placement_step=1,
                                    random_alternative=False, build_worker=worker):
                    return
            self.record_failure(action, "no_safe_reachable_expansion_placement")
            return
        if kind == U.ASSIMILATOR:
            geysers = self._assimilator_candidates()
            for geyser in sorted(geysers, key=lambda g: g.distance_to(self.start_location)):
                worker = self.select_build_worker(geyser.position)
                if worker and await self.build(kind, geyser, build_worker=worker):
                    return
            self.record_failure(action, "no_free_geyser_or_available_builder")
            return
        pylons = self.structures(U.PYLON).ready
        if kind == U.PYLON:
            unpowered = self.structures.ready.filter(lambda b: b.type_id not in {U.NEXUS, U.PYLON, U.ASSIMILATOR}
                                                    and not b.is_powered)
            anchors = [b.position for b in unpowered]
            anchors += [b.position.towards(self.game_info.map_center, 5) for b in self.townhalls]
            if not anchors:
                anchors = [self.workers.center]
        else:
            if kind in {U.SHIELDBATTERY, U.PHOTONCANNON}:
                center = self._defense_position()
                anchors = [p.position.towards(center, 3) for p in sorted(pylons, key=lambda p: p.distance_to(center))]
            else:
                anchors = [p.position for p in sorted(pylons, key=lambda p: self.structures.closer_than(7, p).amount)]
        attempt = self._placement_attempts.get(action, 0)
        self._placement_attempts[action] = attempt + 1
        if anchors:
            offset = attempt % len(anchors)
            anchors = anchors[offset:] + anchors[:offset]
        for anchor in anchors[:6]:
            if await self.build(kind, anchor, max_distance=7 if kind != U.PYLON else 10,
                                placement_step=2, random_alternative=False):
                return
        self.record_failure(action, "no_valid_powered_placement_or_available_builder")

    def _track_production_order(self, decision, commands):
        if not (0 <= decision.action_id <= 59 or 66 <= decision.action_id <= 70) or not commands:
            return
        action = decision.action_id
        order_type = "research" if 34 <= action <= 59 else "chronoboost" if 66 <= action <= 70 else "production"
        kind = self.action_dict[action].split()[1]
        command = commands[0]
        target = getattr(command, "target", None)
        position = target if isinstance(target, Point2) else getattr(target, "position", None)
        self._production_orders[decision.request_id] = {
            "request_id": decision.request_id, "action_id": action, "kind": kind, "order_type": order_type,
            "producer_tag": command.unit.tag, "ability_id": command.ability.value,
            "producer_tags": [c.unit.tag for c in commands],
            "target_tag": getattr(target, "tag", None),
            "position": list(position) if position else None, "submitted_at": self.time,
            "phase": "submitted", "unit_tag": None,
        }

    def _production_event(self, order, phase, **details):
        order["phase"] = phase
        self.log("order_lifecycle", game_loop=self.state.game_loop, request_id=order["request_id"],
                 action_id=order["action_id"], phase=phase, elapsed_game_seconds=self.time - order["submitted_at"],
                 kind=order["kind"], producer_tag=order["producer_tag"], unit_tag=order["unit_tag"], **details)
        if phase in {"failed", "unconfirmed"}:
            reason = str(details.get("engine_result", details.get("reason", "unconfirmed")))
            outcome = {"game_loop": self.state.game_loop, "action_id": order["action_id"],
                       "description": self.action_dict[order["action_id"]], "orders_submitted": 0,
                       "request_id": order["request_id"], "phase": phase,
                       "failures": ["engine_or_observation: " + reason], "intent_applied": False}
            self.recent_outcomes.append(outcome)
            notify = getattr(self, "_notify_execution_outcome", None)
            if notify is not None:
                notify(outcome)
        if phase in {"completed", "failed", "unconfirmed"}:
            self._production_orders.pop(order["request_id"], None)

    def _match_created_unit(self, unit, phase):
        for order in list(self._production_orders.values()):
            if order.get("order_type", "production") != "production":
                continue
            if order["kind"] != unit.type_id.name:
                continue
            if order["unit_tag"] is not None and order["unit_tag"] != unit.tag:
                continue
            if phase == "started" and order["phase"] not in {"submitted", "accepted"}:
                continue
            if order["position"] and unit.distance_to(Point2(order["position"])) > 4:
                continue
            order["unit_tag"] = unit.tag
            self._production_event(order, phase)
            break

    async def on_unit_created(self, unit):
        self._match_created_unit(unit, "completed" if unit.is_ready else "started")

    async def on_unit_type_changed(self, unit, previous_type):
        self._match_created_unit(unit, "completed" if unit.is_ready else "started")

    async def on_upgrade_complete(self, upgrade):
        for order in list(self._production_orders.values()):
            if order.get("order_type") == "research" and order["kind"] == upgrade.name:
                self._production_event(order, "completed", source="upgrade_observation")

    async def on_building_construction_started(self, unit):
        self._match_created_unit(unit, "started")

    async def on_building_construction_complete(self, unit):
        self._match_created_unit(unit, "completed")

    async def on_unit_destroyed(self, unit_tag):
        self._known_enemy_buildings.pop(unit_tag, None)
        for order in list(self._production_orders.values()):
            if order["unit_tag"] == unit_tag:
                self._production_event(order, "failed", reason="destroyed_during_construction")
            elif order.get("order_type") == "research" and order["producer_tag"] == unit_tag:
                self._production_event(order, "failed", reason="researcher_destroyed")

    def _consume_engine_feedback(self):
        for error in getattr(self.state, "action_errors", []):
            signature = (error.unit_tag, error.ability_id, error.result)
            if self.time - self._recent_engine_errors.get(signature, -100) < 1:
                continue
            self._recent_engine_errors[signature] = self.time
            matches = [o for o in self._production_orders.values()
                       if error.unit_tag in o.get("producer_tags", [o["producer_tag"]]) and o["ability_id"] == error.ability_id
                       and o["phase"] in {"submitted", "accepted"}]
            for order in matches:
                self._production_event(order, "failed", reason="engine_action_error", engine_result=error.result)
                self.cooldowns[order["action_id"]] = self.time + 5
            self.log("engine_action_error", game_loop=self.state.game_loop, unit_tag=error.unit_tag,
                     ability_id=error.ability_id, engine_result=error.result, matched_orders=len(matches))
        self._recent_engine_errors = {k: v for k, v in self._recent_engine_errors.items() if self.time - v < 10}
        for order in list(self._production_orders.values()):
            order_type = order.get("order_type", "production")
            if order_type == "research":
                producer = self.structures.find_by_tag(order["producer_tag"])
                if UpgradeId[order["kind"]] in getattr(self.state, "upgrades", set()):
                    self._production_event(order, "completed", source="upgrade_observation")
                    continue
                if producer and any(o.ability.id.value == order["ability_id"] for o in producer.orders):
                    if order["phase"] in {"submitted", "accepted"}:
                        self._production_event(order, "started", source="research_order_observation")
            elif order_type == "chronoboost":
                target = self.structures.find_by_tag(order["target_tag"])
                if target and target.has_buff(BuffId.CHRONOBOOSTENERGYCOST):
                    self._production_event(order, "completed", source="buff_observation")
                    continue
            elif order["unit_tag"] is not None:
                unit = self.units.find_by_tag(order["unit_tag"])
                if unit and unit.is_ready:
                    self._production_event(order, "completed", source="ready_unit_observation")
                    continue
            elif order["kind"] == "ARCHON" and any(self.units.find_by_tag(tag) is None
                                                     for tag in order.get("producer_tags", [])):
                self._production_event(order, "failed", reason="templar_lost_before_merge")
                continue
            deadline = 10 if order_type == "chronoboost" else 180
            if self.time - order["submitted_at"] > deadline:
                self._production_event(order, "unconfirmed", reason="observation_deadline_exceeded")
