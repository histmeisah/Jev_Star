"""Deterministic observer camera for replay review; never issues game orders."""

import math


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def center(units):
    return (sum(u["pos"][0] for u in units) / len(units),
            sum(u["pos"][1] for u in units) / len(units))


def dense_group(units, radius=12):
    if not units:
        return []
    anchor = max(units, key=lambda a: sum(distance(a["pos"], b["pos"]) <= radius for b in units))
    return [u for u in units if distance(u["pos"], anchor["pos"]) <= radius]


class ReplayCamera:
    """Hold ordinary shots for six game seconds; higher priority action can cut in."""

    def __init__(self, player=1, hold_seconds=6):
        self.player, self.hold_seconds = player, hold_seconds
        self.position = None
        self.reason = "Opening base"
        self.priority = -1
        self.shot_started = -100
        self.last_time = None

    def choose(self, units, game_seconds):
        own = [u for u in units if u["owner"] == self.player]
        enemy = [u for u in units if u["owner"] not in (0, self.player, 16)]
        bases = sorted([u for u in own if u["base"]], key=lambda u: u["tag"])
        army = [u for u in own if u["combat"] and not u["structure"]]
        threats = [u for u in enemy if u["combat"] and any(distance(u["pos"], b["pos"]) < 24 for b in bases)]
        fighting = [u for u in army if any(distance(u["pos"], e["pos"]) < 15 for e in enemy
                                          if e["combat"] or e["structure"] or e.get("worker"))]
        if threats:
            target, reason, priority = center(dense_group(threats)), "Base defense", 4
        elif fighting:
            group = dense_group(fighting)
            target, reason, priority = center(group), "Engagement", 3
        elif len(army) >= 6 and bases and any(min(distance(u["pos"], b["pos"]) for b in bases) > 28 for u in army):
            away = [u for u in army if min(distance(u["pos"], b["pos"]) for b in bases) > 24]
            target, reason, priority = center(dense_group(away)), "Army movement", 2
        elif bases:
            building = [b for b in bases if b.get("build_progress", 1) < 1]
            if building and int(game_seconds // 12) % 2:
                target, reason, priority = building[-1]["pos"], "Expansion", 1
            else:
                base = bases[0] if game_seconds < 60 else bases[int(game_seconds // 12) % len(bases)]
                target, reason, priority = base["pos"], "Base economy", 1
        elif own:
            target, reason, priority = center(dense_group(own)), "Remaining forces", 1
        else:
            return self.position, self.reason

        if (self.position is not None and reason != self.reason and priority <= self.priority
                and game_seconds - self.shot_started < self.hold_seconds):
            return self.position, self.reason
        if reason != self.reason or (self.position and distance(self.position, target) > 30):
            self.shot_started = game_seconds
        if self.position is None or distance(self.position, target) > 30:
            self.position = tuple(target)
        elif distance(self.position, target) > 2:
            dt = max(0, game_seconds - (self.last_time if self.last_time is not None else game_seconds))
            fraction = min(1, 10 * dt / distance(self.position, target))
            self.position = tuple(a + (b - a) * fraction for a, b in zip(self.position, target))
        self.reason, self.priority, self.last_time = reason, priority, game_seconds
        return self.position, self.reason
