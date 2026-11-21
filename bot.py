import math
import random
import mechfight as mf

UPGRADE_PREF = ["armor", "plating", "gun_rate", "laser_charge", "laser_damage", "servos", "sensors", "reactor", "capacitor"]
WEAPON_PREF = {"laser": 3, "lightning": 2, "mortar": 1}


def ang(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Bot:
    name = "kaci-mech"

    def __init__(self, hello):
        c = hello["config"]
        self.cfg = c
        self.rng = random.Random(hello.get("seed", 0))
        self.reach = c["loot"]["pickup_radius"]
        self.strafe = 1
        self.strafe_until = 0.0
        self.stuck_t = 0.0
        self.detour = None
        self.last_pos = None
        self.threat_yaw = None
        self.threat_until = 0.0

    def zone_now(self, z, t):
        if t <= z["shrink_start"]:
            return z["center"], z["radius"]
        if t >= z["shrink_end"]:
            return z["next_center"], z["next_radius"]
        f = (t - z["shrink_start"]) / max(1e-6, z["shrink_end"] - z["shrink_start"])
        c = [z["center"][i] + (z["next_center"][i] - z["center"][i]) * f for i in range(2)]
        return c, z["radius"] + (z["next_radius"] - z["radius"]) * f

    def zone_goal(self, obs, p):
        z, t = obs["zone"], obs["t"]
        nc, nr = z["next_center"], z["next_radius"]
        c, r = self.zone_now(z, t + 8)
        # stay well inside the circle we are heading for
        tgt, rad = (nc, nr) if t > z["shrink_start"] - 25 else (c, r)
        dist = d2(p, tgt)
        lim = max(0.0, rad * 0.55 - 6)
        if dist <= lim:
            return None
        return tgt

    def lead(self, me, tgt, speed):
        sc = 1 + self.cfg["cubes"]["size_per_cube"] * tgt.get("cubes", 0)
        m = [me["pos"][0], me["pos"][1], me["pos"][2] + me["stats"].get("muzzle_height", 7)]
        tp = [tgt["pos"][0], tgt["pos"][1], tgt["pos"][2] + (4.5 * sc if tgt["kind"] == "mech" else 1.8)]
        v = tgt.get("vel", [0, 0, 0])
        t = 0.0
        for _ in range(3):
            q = [tp[i] + v[i] * t for i in range(3)]
            dist = math.dist(q, m)
            t = dist / speed
        q = [tp[i] + v[i] * t for i in range(3)]
        if not tgt.get("grounded", True):
            q[2] = max(q[2], tp[2])
        h = math.hypot(q[0] - m[0], q[1] - m[1])
        return math.atan2(q[1] - m[1], q[0] - m[0]), math.atan2(q[2] - m[2], max(h, 0.1)), h

    def mortar_pitch(self, me, tgt):
        w = self.cfg["weapons"]["mortar"]
        v, g = w["speed"], w["gravity"]
        x = d2(me["pos"], tgt["pos"])
        y = tgt["pos"][2] - (me["pos"][2] + me["stats"].get("height", 9) + 0.5)
        k = v ** 4 - g * (g * x * x + 2 * y * v * v)
        if k < 0 or x < 1:
            return None
        return math.atan((v * v - math.sqrt(k)) / (g * x))

    def act(self, obs):
        me = obs["self"]
        p = me["pos"]
        t = obs["t"]
        a = {"tick": obs["tick"]}
        stats = me["stats"]

        if obs.get("offer"):
            ch = [c["id"] for c in obs["offer"]["choices"]]
            best = min(range(len(ch)), key=lambda i: UPGRADE_PREF.index(ch[i]) if ch[i] in UPGRADE_PREF else 99)
            a["pick"] = best

        for e in obs.get("events", []):
            if e["type"] == "damaged" and e.get("bearing") is not None:
                self.threat_yaw = e["bearing"]
                self.threat_until = t + 2.0

        mechs = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        for m in mechs + npcs:
            m["d"] = d2(m["pos"], p)
        mechs.sort(key=lambda m: m["d"])
        hp_frac = me["hp"] / me["max_hp"]

        # target: closest mech, weakest breaks near ties; drones only when no mech around
        target = None
        if mechs:
            near = [m for m in mechs if m["d"] < mechs[0]["d"] + 40]
            target = min(near, key=lambda m: m.get("hp", 1400) + m["d"] * 4)
        elif npcs:
            n = min(npcs, key=lambda m: m["d"])
            if n["d"] < 90 and hp_frac > 0.5:
                target = n

        # weapon pickup
        weps = me["weapons"]
        have = {w["kind"] for w in weps if w}
        want_pick = None
        for pk in obs.get("pickups", []):
            if pk["kind"] in have or pk["kind"] == "gun":
                continue
            dd = d2(pk["pos"], p)
            if weps[1] is not None and WEAPON_PREF.get(pk["kind"], 0) <= WEAPON_PREF.get(weps[1]["kind"], 0):
                continue
            if dd <= self.reach and abs(pk["pos"][2] - p[2]) < 4:
                a["take"] = pk["id"]
                if weps[1] is not None:
                    if weps[1]["kind"] == "laser" and weps[1].get("charge", 0) > 0:
                        a.pop("take")
                        continue
                    a["drop"] = 1
            elif dd < 70 and (want_pick is None or dd < want_pick[1]):
                want_pick = (pk, dd)

        cube = None
        for cb in obs.get("cubes", []):
            dd = d2(cb["pos"], p)
            if dd < 60 and abs(cb["pos"][2] - p[2]) < 6 and (cube is None or dd < cube[1]):
                cube = (cb, dd)

        # movement
        zg = self.zone_goal(obs, p)
        fighting = target is not None and target["kind"] == "mech" and target["d"] < 230
        move = [0.0, 0.0]
        boost = False
        if not me["in_zone"]:
            c, _ = self.zone_now(obs["zone"], t)
            y = math.atan2(c[1] - p[1], c[0] - p[0])
            move = [math.cos(y), math.sin(y)]
            boost = True
        elif fighting:
            e = target
            y = math.atan2(e["pos"][1] - p[1], e["pos"][0] - p[0])
            if t > self.strafe_until:
                self.strafe = -self.strafe if self.rng.random() < 0.7 else self.strafe
                self.strafe_until = t + self.rng.uniform(0.6, 1.6)
            side = y + self.strafe * math.pi / 2
            want = 60 if "lightning" in have else 110
            if hp_frac < 0.35:
                want = 250
            radial = 0.0
            if e["d"] > want + 30:
                radial = 0.6
            elif e["d"] < want - 30:
                radial = -0.8
            mx = math.cos(side) + radial * math.cos(y)
            my = math.sin(side) + radial * math.sin(y)
            if zg:
                gy = math.atan2(zg[1] - p[1], zg[0] - p[0])
                mx += 0.5 * math.cos(gy)
                my += 0.5 * math.sin(gy)
            n = math.hypot(mx, my) or 1
            move = [mx / n, my / n]
            boost = hp_frac < 0.35 and me["energy"] > 30
        else:
            goal = None
            if want_pick and (zg is None or d2(want_pick[0]["pos"], zg) < d2(p, zg) + 40):
                goal = want_pick[0]["pos"]
            elif cube:
                goal = cube[0]["pos"]
            elif target is not None and target["kind"] == "npc":
                goal = None
            elif zg:
                goal = zg
            if goal:
                y = math.atan2(goal[1] - p[1], goal[0] - p[0])
                move = [math.cos(y), math.sin(y)]

        # unstick: slide around walls when we are not making progress
        spd = math.hypot(me["vel"][0], me["vel"][1])
        if math.hypot(*move) > 0.5 and spd < 2.0 and me["grounded"]:
            self.stuck_t += 0.1
        else:
            self.stuck_t = max(0.0, self.stuck_t - 0.05)
        if self.stuck_t > 0.6 and self.detour is None:
            y = math.atan2(move[1], move[0]) + self.rng.choice([-1, 1]) * self.rng.uniform(1.2, 2.0)
            self.detour = (y, t + 1.2)
            a["jump"] = True
        if self.detour:
            y, until = self.detour
            if t < until:
                move = [math.cos(y), math.sin(y)]
                if not me["grounded"] and me["energy"] > 25:
                    boost = True
            else:
                self.detour = None
                self.stuck_t = 0.0

        a["move"] = move
        a["boost"] = boost

        # aim and fire
        fire = [False, False]
        if target is not None:
            gun = next((w for w in weps if w and w["kind"] == "gun"), None)
            spd_b = gun["stats"]["speed"] if gun else 252
            yaw, pitch, h = self.lead(me, target, spd_b)
            a["aim"] = [yaw, pitch]
            yerr = abs(ang(me["aim"][0] - yaw))
            perr = abs(me["aim"][1] - pitch)
            tol = math.atan2(2.0, max(h, 1)) + 0.01
            for i, w in enumerate(weps):
                if not w:
                    continue
                k = w["kind"]
                st = w["stats"]
                if k == "gun":
                    if h < st["range"] and yerr < tol and perr < tol * 1.5 and w.get("reload", 0) == 0:
                        fire[i] = True
                    if w.get("ammo", 1) == 0:
                        a["reload"] = True
                elif k == "laser":
                    if h < st["range"] and target["kind"] == "mech":
                        full = w.get("charge", 0) >= st["charge"] - 0.01
                        fire[i] = not (full and yerr < tol and perr < tol * 1.5)
                        if w.get("cooldown", 0) > 0 and w.get("charge", 0) == 0:
                            fire[i] = False
                elif k == "lightning":
                    if h < st["range"] - 2 and yerr < math.radians(st["cone_deg"]) * 0.8 and me["energy"] > 10:
                        fire[i] = True
                elif k == "mortar":
                    mp = self.mortar_pitch(me, target)
                    if mp is not None and 25 < h < 230 and w.get("cooldown", 0) == 0:
                        ty = math.atan2(target["pos"][1] - p[1], target["pos"][0] - p[0])
                        if abs(ang(me["aim"][0] - ty)) < 0.04:
                            # only take the mortar shot when gun is reloading or target is far
                            if not gun or gun.get("reload", 0) > 0 or h > 120:
                                a["aim"] = [ty, mp]
                                if abs(me["aim"][1] - mp) < 0.03:
                                    fire = [False, False]
                                    fire[i] = True
        elif self.threat_yaw is not None and t < self.threat_until:
            a["aim"] = [self.threat_yaw, 0.0]
        else:
            y = math.atan2(move[1], move[0]) if math.hypot(*move) > 0.3 else me["aim"][0] + 0.5
            a["aim"] = [y, 0.0]
        a["fire"] = fire

        mf.draw.label(f"hp {me['hp']:.0f} tgt {target['id'] if target else '-'}")
        return a


mf.run(Bot)
