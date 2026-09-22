"""Persistent macro missions using only observed enemies and public map geography."""

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

WORKERS = {U.PROBE, U.SCV, U.DRONE}


class MacroNavigation:
    def _initialize_navigation(self):
        self._known_enemy_buildings = {}
        self._search_sites = []
        self._army_target_id = None
        self._army_destination = None
        self._resolved_army_target_id = None
        self._unit_destinations = {}
        self._scouts = {}
        self._last_scout_order = 0
        self._base_integrity = {}
        self._base_damaged_until = {}
        self._last_navigation = -100

    def _set_search_sites(self, locations):
        points = sorted(set(locations), key=lambda p: (p.x, p.y))
        self._search_sites = [{"id": f"expansion_{i}", "position": list(p), "last_checked": None}
                              for i, p in enumerate(points)]

    def _area_visible(self, position):
        return all(self.is_visible(position + Point2(offset)) for offset in [(0, 0), (5, 0), (-5, 0), (0, 5), (0, -5)])

    def _update_navigation(self):
        if self.time - self._last_navigation < 1:
            return
        self._last_navigation = self.time
        visible = {b.tag: b for b in self.enemy_structures if b.is_visible}
        for tag, building in visible.items():
            self._known_enemy_buildings[tag] = {"id": f"enemy_{tag}", "type": building.type_id.name,
                                                "position": list(building.position), "last_seen": self.time}
        for tag, memory in list(self._known_enemy_buildings.items()):
            if tag not in visible and self._area_visible(Point2(memory["position"])):
                del self._known_enemy_buildings[tag]
                self.log("enemy_location_cleared", game_loop=self.state.game_loop, target_id=memory["id"])
        for site in self._search_sites:
            if self._area_visible(Point2(site["position"])):
                site["last_checked"] = round(self.time, 1)
        for base in self.townhalls:
            integrity = getattr(base, "health", 0) + getattr(base, "shield", 0)
            if integrity < self._base_integrity.get(base.tag, integrity) - 1:
                self._base_damaged_until[base.tag] = self.time + 5
            self._base_integrity[base.tag] = integrity
        alive = {u.tag for u in self.units}
        self._scouts = {tag: mission for tag, mission in self._scouts.items() if tag in alive}
        self._unit_destinations = {tag: pos for tag, pos in self._unit_destinations.items() if tag in alive}

    def _threat_units(self):
        nearby = self.enemy_units.filter(lambda e: e.is_visible and e.can_attack
                                         and not e.type_id.name.startswith("CHANGELING")
                                         and any(e.distance_to(b) < 30 for b in self.townhalls))
        workers = nearby.of_type(WORKERS)
        damaged = any(self._base_damaged_until.get(b.tag, 0) > self.time for b in self.townhalls)
        return nearby.filter(lambda e: e.type_id not in WORKERS or workers.amount >= 3 or damaged)

    def _emergency(self):
        return bool(self._threat_units()) or any(self._base_damaged_until.get(b.tag, 0) > self.time for b in self.townhalls)

    def _defense_position(self):
        threats = self._threat_units()
        if self.townhalls:
            if threats:
                return min(self.townhalls, key=lambda b: threats.closest_distance_to(b)).position
            # Rally at the most exposed existing base; this is a macro rally, not a wall-in.
            return self.townhalls.closest_to(self.enemy_start_locations[0]).position
        return self.start_location

    def _search_target(self, exclude=None):
        candidates = [s for s in self._search_sites
                      if not any(b.distance_to(Point2(s["position"])) < 12 for b in self.townhalls)
                      and (exclude is None or Point2(s["position"]).distance_to(exclude) > 12)]
        if not candidates:
            return "enemy_start", self.enemy_start_locations[0]
        site = min(candidates, key=lambda s: (s["last_checked"] is not None,
                    s["last_checked"] if s["last_checked"] is not None else 0,
                    Point2(s["position"]).distance_to(self.enemy_start_locations[0])))
        return site["id"], Point2(site["position"])

    def _navigation_snapshot(self):
        army = self._combat_units()
        return {"army_target_id": self._army_target_id,
                "current_target_id": self._resolved_army_target_id,
                "army_center": list(army.center) if army else None,
                "army_destination": list(self._army_destination) if self._army_destination else None,
                "targets": [{"id": "home", "position": list(self._defense_position())},
                            {"id": "enemy_start", "position": list(self.enemy_start_locations[0])},
                            *self._search_sites, *self._known_enemy_buildings.values()],
                "scouts": [{"unit_tag": tag, **mission} for tag, mission in self._scouts.items()],
                "knowledge": "Map expansion sites are public geography; enemy structures are remembered only after sighting. Cleared sites are searched again later."}

    def _planned_target(self):
        planner = getattr(self, "planner", None)
        plan = planner.active if planner else None
        if plan and self.time < plan["expires_game_seconds"]:
            return plan.get("army_target_id")
        return None

    def _attack_position(self):
        requested = self._army_target_id
        candidates = list(self._known_enemy_buildings.values())
        for memory in candidates:
            if memory["id"] == requested:
                return memory["id"], Point2(memory["position"])
        for site in self._search_sites:
            if site["id"] == requested and not self._area_visible(Point2(site["position"])):
                return site["id"], Point2(site["position"])
        if requested == "enemy_start" and not self._area_visible(self.enemy_start_locations[0]):
            return "enemy_start", self.enemy_start_locations[0]
        if candidates:
            army = self._combat_units()
            origin = army.center if army else self.start_location
            target = min(candidates, key=lambda e: Point2(e["position"]).distance_to(origin))
            return target["id"], Point2(target["position"])
        return self._search_target()

    def _set_army_intent(self, intent):
        self.army_intent = intent
        self._army_target_id = self._planned_target() if intent == "attack" else "home"
        self._issue_army_intent(include_busy=True)

    def _issue_army_intent(self, include_busy=False):
        army = self._combat_units().filter(lambda u: u.tag not in self.unit_tags_received_action and u.tag not in self._scouts)
        if not army:
            return
        if self.army_intent == "attack":
            target_id, target = self._attack_position()
        elif self.army_intent == "retreat":
            threats = self._threat_units()
            bases = list(self.townhalls)
            base = max(bases, key=lambda b: threats.closest_distance_to(b)) if bases and threats else (
                min(bases, key=lambda b: b.distance_to(self.start_location)) if bases else None)
            target_id, target = "home", base.position if base else self.start_location
        else:
            target_id, target = "home", self._defense_position()
            threats = self._threat_units().closer_than(30, target)
            if threats:
                target = threats.closest_to(target).position
        if self._army_destination is None or self._army_destination.distance_to(target) > 6:
            self.log("army_target_changed", game_loop=self.state.game_loop, intent=self.army_intent,
                     target_id=target_id, position=list(target))
        self._army_destination = target
        self._resolved_army_target_id = target_id
        for unit in army:
            previous = self._unit_destinations.get(unit.tag)
            changed = previous is None or previous.distance_to(target) > 6
            if not include_busy and not changed and not unit.is_idle:
                continue
            if unit.distance_to(target) < 4 and self.army_intent != "attack":
                continue
            if self.army_intent == "retreat":
                unit.move(target)
            else:
                unit.attack(target)
            self._unit_destinations[unit.tag] = target

    def _scout_candidates(self, kind):
        units = self.units(kind).ready
        if kind == U.OBSERVER and self._combat_units():
            # Keep one detector with the army, including during defense.
            units = units.filter(lambda u: u.tag != min(self.units(U.OBSERVER).ready.tags, default=None))
        return units

    async def _start_scout(self, action, kind):
        candidates = self._scout_candidates(kind)
        if not candidates:
            self.record_failure(action, "no_available_scout_keep_army_detector")
            return
        unit = min(candidates, key=lambda u: (u.tag in self._scouts, not u.is_idle, u.tag))
        target_id, target = self._search_target(exclude=self._army_destination if self.army_intent == "attack" else None)
        self._scouts[unit.tag] = {"target_id": target_id, "position": list(target), "assigned_at": self.time}
        unit.move(target)
        self._last_scout_order = self.time
        self.log("scout_target_changed", game_loop=self.state.game_loop, unit_tag=unit.tag,
                 target_id=target_id, position=list(target))

    def _maintain_scouts_and_detection(self):
        for tag, mission in list(self._scouts.items()):
            unit = self.units.find_by_tag(tag)
            if unit is None or tag in self.unit_tags_received_action:
                continue
            target = Point2(mission["position"])
            if self._area_visible(target) or self.time - mission["assigned_at"] > 60:
                target_id, target = self._search_target(exclude=self._army_destination if self.army_intent == "attack" else None)
                mission.update(target_id=target_id, position=list(target), assigned_at=self.time)
                unit.move(target)
                self.log("scout_target_changed", game_loop=self.state.game_loop, unit_tag=tag,
                         target_id=target_id, position=list(target))
            elif unit.is_idle:
                unit.move(target)
        army = self._combat_units()
        observers = self.units(U.OBSERVER).ready.filter(lambda u: u.tag not in self._scouts)
        if army and observers:
            detector = observers.sorted(lambda u: u.tag).first
            destination = army.center.towards(self._defense_position(), 3)
            if detector.tag not in self.unit_tags_received_action and detector.distance_to(destination) > 4:
                detector.move(destination)
        # Support units must not be left at their production location. This only
        # follows the army; it does not invent spellcasting or transport orders.
        reserved = self._morph_reserved_tags()
        supports = self.units.of_type({U.ORACLE, U.DISRUPTOR, U.WARPPRISM, U.HIGHTEMPLAR}).ready.filter(
            lambda u: (not u.can_attack or u.type_id == U.ORACLE)
            and u.tag not in reserved and u.tag not in self._scouts
            and u.tag not in self.unit_tags_received_action)
        destination = army.center.towards(self._defense_position(), 3) if army else self._defense_position()
        for unit in supports:
            if unit.distance_to(destination) > 5:
                unit.move(destination)
