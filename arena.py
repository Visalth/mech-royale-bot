import json
import math
import os
from array import array

CELL = 2.0


class Arena:
    """Static map as a 2 m grid: ground height, solid top and solid base per cell."""

    def __init__(self, d):
        w = json.load(open(os.path.join(d, "v2", "world.json")))
        tr = w["terrain"]
        self.tnx, self.tny = tr["nx"], tr["ny"]
        self.tx0, self.ty0, self.tstep = tr["minX"], tr["minY"], tr["step"]
        self.ter = array("h")
        with open(os.path.join(d, "v2", tr["file"]), "rb") as f:
            self.ter.frombytes(f.read())
        b = w["bounds"]
        self.x0, self.y0 = b["minX"], b["minY"]
        self.nx = int((b["maxX"] - b["minX"]) / CELL) + 1
        self.ny = int((b["maxY"] - b["minY"]) / CELL) + 1
        n = self.nx * self.ny
        self.ground = array("f", [0.0]) * n
        for j in range(self.ny):
            y = self.y0 + (j + 0.5) * CELL
            for i in range(self.nx):
                self.ground[j * self.nx + i] = self.terrain(self.x0 + (i + 0.5) * CELL, y)
        self.top = array("f", self.ground)
        self.base = array("f", [1e9]) * n
        for p in w["prisms"]:
            self._fill(p)
        self._build_nav()

    def _build_nav(self):
        """4 m walkability grid for ground-level path planning (buildings blocked, low bridges blocked)."""
        k = 2
        self.gnx, self.gny = (self.nx + k - 1) // k, (self.ny + k - 1) // k
        self.walk = bytearray(self.gnx * self.gny)
        for gj in range(self.gny):
            for gi in range(self.gnx):
                ok = 1
                for j in range(gj * k, min(gj * k + k, self.ny)):
                    for i in range(gi * k, min(gi * k + k, self.nx)):
                        c = j * self.nx + i
                        if self.top[c] - self.ground[c] > 1.5 and self.base[c] - self.ground[c] < 9:
                            ok = 0
                self.walk[gj * self.gnx + gi] = ok

    def gcell(self, x, y):
        gi = int((x - self.x0) / (CELL * 2))
        gj = int((y - self.y0) / (CELL * 2))
        return min(max(gi, 0), self.gnx - 1), min(max(gj, 0), self.gny - 1)

    def gcenter(self, gi, gj):
        return self.x0 + (gi + 0.5) * CELL * 2, self.y0 + (gj + 0.5) * CELL * 2

    def path(self, a, b, limit=5000):
        """A* over the 4 m grid from point a to point b. Returns waypoints (x, y) or None."""
        import heapq
        nx, walk = self.gnx, self.walk
        si, sj = self.gcell(a[0], a[1])
        ti, tj = self.gcell(b[0], b[1])
        if not walk[tj * nx + ti]:
            best = None
            for r in range(1, 6):
                for dj in range(-r, r + 1):
                    for di in range(-r, r + 1):
                        i, j = ti + di, tj + dj
                        if 0 <= i < nx and 0 <= j < self.gny and walk[j * nx + i]:
                            d = di * di + dj * dj
                            if best is None or d < best[0]:
                                best = (d, i, j)
                if best:
                    break
            if not best:
                return None
            ti, tj = best[1], best[2]
        start, goal = sj * nx + si, tj * nx + ti
        if start == goal:
            return [tuple(b[:2])]
        steps = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                 (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414))
        g = {start: 0.0}
        prev = {}
        h = lambda i, j: math.hypot(i - ti, j - tj)
        heap = [(h(si, sj), start)]
        n = 0
        found = None
        bestn, besth = start, h(si, sj)
        while heap and n < limit:
            f, c = heapq.heappop(heap)
            if c == goal:
                found = c
                break
            n += 1
            ci, cj = c % nx, c // nx
            gc = g[c]
            for di, dj, w in steps:
                i, j = ci + di, cj + dj
                if not (0 <= i < nx and 0 <= j < self.gny):
                    continue
                q = j * nx + i
                if not walk[q] and q != start:
                    continue
                if di and dj and not (walk[cj * nx + i] and walk[j * nx + ci]):
                    continue
                ng = gc + w
                if ng < g.get(q, 1e18):
                    g[q] = ng
                    prev[q] = c
                    hq = h(i, j)
                    if hq < besth:
                        bestn, besth = q, hq
                    heapq.heappush(heap, (ng + hq, q))
        end = found if found is not None else bestn
        pts = []
        c = end
        while c != start:
            pts.append(self.gcenter(c % nx, c // nx))
            c = prev[c]
        pts.reverse()
        if found is not None:
            pts.append(tuple(b[:2]))
        return pts or None

    def terrain(self, x, y):
        fx = (x - self.tx0) / self.tstep
        fy = (y - self.ty0) / self.tstep
        i = min(max(int(fx), 0), self.tnx - 2)
        j = min(max(int(fy), 0), self.tny - 2)
        u = min(max(fx - i, 0.0), 1.0)
        v = min(max(fy - j, 0.0), 1.0)
        t, nx = self.ter, self.tnx
        a = t[j * nx + i] * (1 - u) + t[j * nx + i + 1] * u
        c = t[(j + 1) * nx + i] * (1 - u) + t[(j + 1) * nx + i + 1] * u
        return (a * (1 - v) + c * v) * 0.01

    def _fill(self, p):
        rings = [p["ring"]] + list(p.get("holes") or [])
        edges = []
        for r in rings:
            k = len(r) // 2
            for e in range(k):
                ax, ay = r[2 * e], r[2 * e + 1]
                bx, by = r[2 * ((e + 1) % k)], r[2 * ((e + 1) % k) + 1]
                if ay != by:
                    edges.append((ax, ay, bx, by))
        ys = p["ring"][1::2]
        j0 = max(0, int((min(ys) - self.y0) / CELL))
        j1 = min(self.ny - 1, int((max(ys) - self.y0) / CELL))
        top, base = p["top"], p["base"]
        for j in range(j0, j1 + 1):
            y = self.y0 + (j + 0.5) * CELL
            xs = sorted(ax + (y - ay) * (bx - ax) / (by - ay) for ax, ay, bx, by in edges if (ay <= y) != (by <= y))
            for k in range(0, len(xs) - 1, 2):
                i0 = max(0, int(math.ceil((xs[k] - self.x0) / CELL - 0.5)))
                i1 = min(self.nx - 1, int(math.floor((xs[k + 1] - self.x0) / CELL - 0.5)))
                row = j * self.nx
                for i in range(i0, i1 + 1):
                    c = row + i
                    if top > self.top[c]:
                        self.top[c] = top
                    if base < self.base[c]:
                        self.base[c] = base

    def idx(self, x, y):
        i = int((x - self.x0) / CELL)
        j = int((y - self.y0) / CELL)
        if 0 <= i < self.nx and 0 <= j < self.ny:
            return j * self.nx + i
        return -1

    def blocked(self, x, y, z):
        """True when a mech with feet at height z can't stand in this cell (wall in the way)."""
        c = self.idx(x, y)
        if c < 0:
            return True
        top = self.top[c]
        if top <= z + 1.2:
            return False
        return self.base[c] < z + 9

    def solid_at(self, x, y, z):
        c = self.idx(x, y)
        if c < 0:
            return True
        if z < self.ground[c]:
            return True
        return self.base[c] <= z <= self.top[c]

    def los(self, a, b, step=1.5):
        """Line of sight between two 3D points through buildings and terrain (trees ignored)."""
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        dist = math.hypot(dx, dy)
        n = int(dist / step)
        if n < 2:
            return True
        for k in range(1, n):
            f = k / n
            if self.solid_at(a[0] + dx * f, a[1] + dy * f, a[2] + dz * f):
                return False
        return True

    def clear_run(self, x, y, z, yaw, dist, step=2.0):
        """How far (m) we can walk from (x,y) along yaw before hitting a wall, up to dist."""
        c, s = math.cos(yaw), math.sin(yaw)
        d = step
        while d <= dist:
            px, py = x + c * d, y + s * d
            ci = self.idx(px, py)
            if ci < 0:
                return d - step
            g = self.top[ci] if self.base[ci] < z + 9 else self.ground[ci]
            if g > z + 1.2 + d * 0.5:
                return d - step
            z = max(z, min(g, z + 1.2)) if g <= z + 1.2 else z
            d += step
        return dist
