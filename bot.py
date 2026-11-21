import math
import os
import random
import mechfight as mf

try:
    from arena import Arena
except Exception:
    Arena = None

UPGRADE_PREF = ["plating", "armor", "nanites", "gun_damage", "gun_rate", "laser_damage", "laser_charge",
                "lightning_efficiency", "gun_velocity", "gyros", "servos", "sensors", "gun_mag",
                "mortar_reload", "reactor", "capacitor", "jets"]
WEAPON_PREF = {"laser": 3, "lightning": 2, "mortar": 1}
LAG = float(os.environ.get("MF_LAG", "0"))  # extra prediction for the live test rig only; 0 in scored matches
GHOST = os.environ.get("MF_MODE") == "ghost"  # experiment: never shoot, only survive
DIRS = [i * math.pi / 8 for i in range(16)]


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
        self.arena = None
        if Arena:
            try:
                self.arena = Arena(hello["map"]["dir"])
            except Exception as e:
                mf.log("map load failed", e)
        self.strafe = 1
        self.strafe_until = 0.0
        self.stuck_t = 0.0
        self.detour = None
        self.threat_yaw = None
        self.threat_until = 0.0
        self.scan = 0.0

    # zone

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
        if t > z["shrink_start"] - 25:
            tgt, rad = z["next_center"], z["next_radius"]
        else:
            tgt, rad = self.zone_now(z, t + 8)
        if d2(p, tgt) <= max(0.0, rad * 0.55 - 6):
            return None
        return tgt

    # aiming

    def lead(self, me, tgt, speed):
        sc = 1 + self.cfg["cubes"]["size_per_cube"] * tgt.get("cubes", 0)
        m = [me["pos"][0], me["pos"][1], me["pos"][2] + me["stats"].get("muzzle_height", 7)]
        tp = [tgt["pos"][0], tgt["pos"][1], tgt["pos"][2] + (4.5 * sc if tgt["kind"] == "mech" else 1.8)]
        v = tgt.get("vel", [0, 0, 0])
        tp = [tp[i] + v[i] * LAG for i in range(3)]
        t = 0.0
        for _ in range(3):
            q = [tp[i] + v[i] * t for i in range(3)]
            t = math.dist(q, m) / speed
        q = [tp[i] + v[i] * t for i in range(3)]
        if self.arena:
            g = self.arena.terrain(q[0], q[1])
            q[2] = max(q[2], g + 2.0)
        h = math.hypot(q[0] - m[0], q[1] - m[1])
        return math.atan2(q[1] - m[1], q[0] - m[0]), math.atan2(q[2] - m[2], max(h, 0.1)), h, m, tp

    def mortar_pitch(self, me, tgt):
        w = self.cfg["weapons"]["mortar"]
        v, g = w["speed"], w["gravity"]
        x = d2(me["pos"], tgt["pos"])
        y = tgt["pos"][2] - (me["pos"][2] + me["stats"].get("height", 9) + 0.5)
        k = v ** 4 - g * (g * x * x + 2 * y * v * v)
        if k < 0 or x < 1:
            return None
        return math.atan((v * v - math.sqrt(k)) / (g * x))

    def los(self, a, b):
        return self.arena.los(a, b) if self.arena else True

    def eye(self, m, h=8.0):
        return [m["pos"][0], m["pos"][1], m["pos"][2] + h]

    # movement

    def pick_dir(self, obs, me, threats, goal, want, cover):
        """Score 16 headings and return the best as a move vector."""
        p = me["pos"]
        t = obs["t"]
        zc, zr = self.zone_now(obs["zone"], t + 6)
        main = threats[0] if threats else None
        best, best_s = None, -1e9
        step = 10.0
        for y in DIRS:
            cx, cy = math.cos(y), math.sin(y)
            run = self.arena.clear_run(p[0], p[1], p[2], y, step) if self.arena else step
            if run < 3:
                continue
            q = [p[0] + cx * run, p[1] + cy * run, p[2]]
            s = run * 0.05
            dz = d2(q, zc) - zr
            if dz > -5:
                s -= 2 + (dz + 5) * 0.2
            if goal:
                s += 1.5 * math.cos(ang(y - math.atan2(goal[1] - p[1], goal[0] - p[0])))
            for k, e in enumerate(threats[:3]):
                w = 1.0 if k == 0 else 0.4
                de = d2(q, e["pos"])
                if de < want[0]:
                    s -= w * (want[0] - de) * 0.08
                elif de > want[1]:
                    s -= w * (de - want[1]) * 0.03
                if cover and self.arena:
                    qe = [q[0], q[1], self.arena.terrain(q[0], q[1]) + 8]
                    if not self.los(self.eye(e), qe):
                        s += 2.5 * w
            if main is not None:
                ey = math.atan2(main["pos"][1] - p[1], main["pos"][0] - p[0])
                s += 0.6 * abs(math.sin(ang(y - ey))) * (1 if not cover else 0.3)
                s += 0.4 * math.cos(ang(y - (ey + self.strafe * math.pi / 2)))
            for sh in obs.get("projectiles", []):
                if sh["kind"] == "mortar":
                    land = self.shell_land(sh)
                    if land and d2(land, q) < 11 and d2(land, p) < 25:
                        s -= 3
            if s > best_s:
                best, best_s = y, s
        if best is None:
            return None
        return [math.cos(best), math.sin(best)]

    def shell_land(self, sh):
        g = self.cfg["weapons"]["mortar"]["gravity"]
        x, y, z = sh["pos"]
        vx, vy, vz = sh["vel"]
        gz = self.arena.terrain(x, y) if self.arena else 0
        k = vz * vz + 2 * g * (z - gz)
        if k < 0:
            return None
        tt = (vz + math.sqrt(k)) / g
        return [x + vx * tt, y + vy * tt]

    def act(self, obs):
        me = obs["self"]
        p = me["pos"]
        t = obs["t"]
        a = {"tick": obs["tick"]}
        weps = me["weapons"]
        have = {w["kind"] for w in weps if w}
        hp_frac = me["hp"] / me["max_hp"]

        if obs.get("offer"):
            ch = [c["id"] for c in obs["offer"]["choices"]]
            a["pick"] = min(range(len(ch)), key=lambda i: UPGRADE_PREF.index(ch[i]) if ch[i] in UPGRADE_PREF else 99)

        hurt = False
        for e in obs.get("events", []):
            if e["type"] == "damaged" and e.get("bearing") is not None:
                self.threat_yaw = e["bearing"]
                self.threat_until = t + 2.5
                hurt = True

        mechs = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        my_eye = self.eye(me, me["stats"].get("eye_height", 8))
        for m in mechs + npcs:
            m["d"] = d2(m["pos"], p)
            m["see"] = self.los(my_eye, [m["pos"][0], m["pos"][1], m["pos"][2] + 4.5])
        mechs.sort(key=lambda m: m["d"])
        threats = [m for m in mechs if m["d"] < 220]

        target = None
        if mechs:
            near = [m for m in mechs if m["d"] < mechs[0]["d"] + 40]
            target = min(near, key=lambda m: m.get("hp", 1400) + m["d"] * 4 + (0 if m["see"] else 2500))
        elif npcs:
            n = min(npcs, key=lambda m: m["d"])
            if n["d"] < 90 and hp_frac > 0.6 and n["see"]:
                target = n

        # loot
        want_pick = None
        for pk in obs.get("pickups", []):
            if pk["kind"] in have or pk["kind"] == "gun":
                continue
            if weps[1] is not None and WEAPON_PREF.get(pk["kind"], 0) <= WEAPON_PREF.get(weps[1]["kind"], 0):
                continue
            dd = d2(pk["pos"], p)
            if dd <= self.reach and abs(pk["pos"][2] - p[2]) < 4:
                if weps[1] is not None and weps[1]["kind"] == "laser" and weps[1].get("charge", 0) > 0:
                    continue
                a["take"] = pk["id"]
                if weps[1] is not None:
                    a["drop"] = 1
            elif dd < (130 if weps[1] is None else 70) and abs(pk["pos"][2] - p[2]) < 6 and (want_pick is None or dd < want_pick[1]):
                want_pick = (pk, dd)
        cube = None
        for cb in obs.get("cubes", []):
            dd = d2(cb["pos"], p)
            if dd < 70 and abs(cb["pos"][2] - p[2]) < 6 and (cube is None or dd < cube[1]):
                cube = (cb, dd)

        # how far to stay from the main threat
        enemy_light = bool(threats) and "lightning" in threats[0].get("weapons", [])
        if "lightning" in have and hp_frac > 0.6 and not enemy_light:
            want = (18, 38)
        elif enemy_light:
            want = (60, 140)
        else:
            want = (90, 160)
        cover = hp_frac < 0.4 or any(w and w["kind"] == "gun" and w.get("reload", 0) > 0.5 for w in weps)
        if hp_frac < 0.4:
            want = (150, 260)
        if GHOST:
            want, cover = (220, 400), True

        zg = self.zone_goal(obs, p)
        goal = zg
        close = threats[0]["d"] if threats else 999
        loot_ok = close > 140 or (weps[1] is None and close > 60) or (want_pick and want_pick[1] < 25 and close > 35)
        if GHOST:
            loot_ok = close > 160
        if loot_ok and not (hurt and close < 90):
            if want_pick and (zg is None or d2(want_pick[0]["pos"], zg) < d2(p, zg) + 60):
                goal = want_pick[0]["pos"]
            elif cube and close > 100:
                goal = cube[0]["pos"]
        looting = goal is not zg

        boost = False
        if not me["in_zone"]:
            c, _ = self.zone_now(obs["zone"], t)
            goal = c
            boost = True

        if t > self.strafe_until:
            self.strafe = -self.strafe if self.rng.random() < 0.7 else self.strafe
            self.strafe_until = t + self.rng.uniform(0.6, 1.6)

        if goal and looting and d2(goal, p) < 12:
            y = math.atan2(goal[1] - p[1], goal[0] - p[0])
            move = [math.cos(y), math.sin(y)]
        elif threats and me["in_zone"] and not looting:
            move = self.pick_dir(obs, me, threats, goal, want, cover)
            boost = (hp_frac < 0.4 or (GHOST and close < 120)) and me["energy"] > 30
        elif goal:
            move = self.pick_dir(obs, me, [], goal, (0, 0), False)
        else:
            move = [0.0, 0.0]
        if move is None:
            move = [0.0, 0.0]

        # unstick
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
        gun = next((w for w in weps if w and w["kind"] == "gun"), None)
        if target is not None:
            yaw, pitch, h, muzzle, tp = self.lead(me, target, gun["stats"]["speed"] if gun else 252)
            a["aim"] = [yaw, pitch]
            clear = target["see"] and self.los(muzzle, tp)
            yerr = abs(ang(me["aim"][0] - yaw))
            perr = abs(me["aim"][1] - pitch)
            tol = math.atan2(2.0, max(h, 1)) + 0.008
            for i, w in enumerate(weps):
                if not w:
                    continue
                k, st = w["kind"], w["stats"]
                if k == "gun":
                    if clear and h < st["range"] and yerr < tol and perr < tol * 1.5 and w.get("reload", 0) == 0:
                        fire[i] = True
                    if w.get("ammo", 1) == 0:
                        a["reload"] = True
                elif k == "laser":
                    if target["kind"] == "mech" and h < st["range"] and (clear or w.get("charge", 0) > 0):
                        full = w.get("charge", 0) >= st["charge"] - 0.01
                        fire[i] = not (full and clear and yerr < tol and perr < tol * 1.5)
                        if w.get("cooldown", 0) > 0 and w.get("charge", 0) == 0:
                            fire[i] = False
                elif k == "lightning":
                    if clear and h < st["range"] - 2 and yerr < math.radians(st["cone_deg"]) * 0.8 and me["energy"] > 10:
                        fire[i] = True
                elif k == "mortar":
                    mp = self.mortar_pitch(me, target)
                    if mp is not None and 25 < h < 230 and w.get("cooldown", 0) == 0 and target["kind"] == "mech":
                        ty = math.atan2(target["pos"][1] - p[1], target["pos"][0] - p[0])
                        if (not gun or gun.get("reload", 0) > 0 or h > 120 or not clear) and abs(ang(me["aim"][0] - ty)) < 0.04:
                            a["aim"] = [ty, mp]
                            if abs(me["aim"][1] - mp) < 0.03:
                                fire = [False, False]
                                fire[i] = True
        elif threats:
            e = threats[0]
            a["aim"] = [math.atan2(e["pos"][1] - p[1], e["pos"][0] - p[0]), 0.0]
        elif self.threat_yaw is not None and t < self.threat_until:
            a["aim"] = [self.threat_yaw, 0.0]
        else:
            # sweep the view around the walking direction to spot hunters early
            self.scan += 0.25
            base = math.atan2(move[1], move[0]) if math.hypot(*move) > 0.3 else me["aim"][0]
            a["aim"] = [base + 1.3 * math.sin(self.scan), 0.0]
        a["fire"] = [False, False] if GHOST else fire

        mf.draw.label(f"hp {me['hp']:.0f} tgt {target['id'] if target else '-'}")
        return a


mf.run(Bot)
