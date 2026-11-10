"""Remove BG: cut the subject out of a photo and make the background transparent.

No neural network and no extra dependencies (numpy + Pillow only), so it works offline and keeps
the app small. It is built for the common case: a subject on a fairly plain or smoothly changing
background (product shots, logos, screenshots, portraits against a wall, scanned signatures).

How it works:
1. Learn the background colours from the image's edges (k-means in Lab colour space). Colour
   groups that only touch one edge (a body cut off at the bottom, say) are not treated as
   background; edges that appear on several sides of the frame are.
2. Mark every pixel whose colour is close to one of those colours, then keep only the regions
   connected to the frame or to a point the user clicked ("flood from the edges").
3. Clean up: drop tiny specks, smooth the outline, then refine the edge at full resolution with a
   soft colour-based alpha and remove the background tint from semi-transparent edge pixels.
4. Apply the user's Erase / Keep brush strokes on top.

Coordinates of clicks and strokes are fractions of the image width/height, so the same edit
applies to the reduced-size preview and the full-resolution save."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

PREVIEW_WORK_SIDE = 640   # mask resolution for the live preview
SAVE_WORK_SIDE = 1100     # mask resolution for the saved file (edges are refined at full size)


@dataclass
class Stroke:
    mode: str                                   # "erase" or "keep"
    radius: float                               # fraction of the image's longer side
    points: list[tuple[float, float]] = field(default_factory=list)   # fractions of (w, h)


@dataclass
class Cutout:
    tolerance: int = 40       # 0 … 100: how different from the background a pixel may be and still go
    softness: int = 35        # 0 … 100: edge softness
    holes: bool = False       # also remove background-coloured areas enclosed by the subject
    specks: bool = True       # drop small leftover islands
    trim: bool = False        # crop the result to the subject
    seeds: list[tuple[float, float]] = field(default_factory=list)   # extra background points
    strokes: list[Stroke] = field(default_factory=list)


# ---------------------------------------------------------------- colour
_LIN = np.where(np.arange(256) / 255 <= 0.04045, np.arange(256) / 255 / 12.92,
                ((np.arange(256) / 255 + 0.055) / 1.055) ** 2.4).astype(np.float32)
_M = (np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]],
               np.float32) / np.array([[0.95047], [1.0], [1.08883]], np.float32)).T


def to_lab(rgb: np.ndarray) -> np.ndarray:
    """uint8 RGB (..., 3) → float32 CIE Lab (..., 3)."""
    xyz = _LIN[rgb] @ _M
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def _min_dist(lab: np.ndarray, centers: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Distance from every pixel to the nearest centre, and that centre's index."""
    best = np.full(lab.shape[:-1], np.inf, np.float32)
    idx = np.zeros(lab.shape[:-1], np.int32)
    for i, c in enumerate(centers):
        d = np.sqrt(((lab - c) ** 2).sum(-1))
        closer = d < best
        best[closer] = d[closer]
        idx[closer] = i
    return best, idx


def _kmeans(x: np.ndarray, k: int, iters: int = 12) -> np.ndarray:
    rng = np.random.default_rng(0)
    k = min(k, len(x))
    centers = [x[rng.integers(len(x))]]
    for _ in range(1, k):   # k-means++ seeding
        d = np.min([((x - c) ** 2).sum(1) for c in centers], 0)
        if d.sum() <= 0:
            break
        centers.append(x[rng.choice(len(x), p=d / d.sum())])
    c = np.array(centers, np.float32)
    for _ in range(iters):
        lab = np.argmin(((x[:, None, :] - c[None]) ** 2).sum(-1), 1)
        for j in range(len(c)):
            m = lab == j
            if m.any():
                c[j] = x[m].mean(0)
    return c


def _groups(centers: np.ndarray, link: float = 15.0) -> list[list[int]]:
    """Single-linkage grouping: a smooth gradient becomes one group, a distinct subject colour its own."""
    n = len(centers)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for i in range(n):
        for j in range(i + 1, n):
            if np.linalg.norm(centers[i] - centers[j]) < link:
                parent[find(i)] = find(j)
    out: dict[int, list[int]] = {}
    for i in range(n):
        out.setdefault(find(i), []).append(i)
    return list(out.values())


def background_palette(rgb: np.ndarray, lab: np.ndarray, seeds_px: list[tuple[int, int]],
                       alpha: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Lab and RGB colours that make up the background, learnt from the frame (+ clicked points)."""
    h, w = lab.shape[:2]
    b = max(2, round(min(h, w) * 0.02))
    sides = [lab[:b].reshape(-1, 3), lab[-b:].reshape(-1, 3),
             lab[:, :b].reshape(-1, 3), lab[:, -b:].reshape(-1, 3)]
    sides_rgb = [rgb[:b].reshape(-1, 3), rgb[-b:].reshape(-1, 3),
                 rgb[:, :b].reshape(-1, 3), rgb[:, -b:].reshape(-1, 3)]
    step = [max(1, len(s) // 1500) for s in sides]
    x = np.concatenate([s[::st] for s, st in zip(sides, step)]).astype(np.float32)
    xr = np.concatenate([s[::st] for s, st in zip(sides_rgb, step)]).astype(np.float32)
    side_id = np.concatenate([np.full(len(s[::st]), i) for i, (s, st) in enumerate(zip(sides, step))])

    # how much each edge sample says "background": the top edge almost always is, the bottom
    # often isn't (people and objects are cut off there), and the sides less so further down
    rows = np.arange(h) / max(1, h - 1)
    bw = lab[:, :b].shape[1]
    yrel = np.concatenate([np.zeros(len(sides[0][::step[0]])), np.ones(len(sides[1][::step[1]])),
                           np.repeat(rows, bw)[::step[2]], np.repeat(rows, bw)[::step[3]]])
    weight = np.where(side_id == 0, 1.0, np.where(side_id == 1, 0.15, 1.0 - 0.8 * yrel))
    if alpha is not None:   # ignore already-transparent edge pixels
        a_s = [alpha[:b].reshape(-1), alpha[-b:].reshape(-1), alpha[:, :b].reshape(-1), alpha[:, -b:].reshape(-1)]
        opaque = np.concatenate([s[::st] for s, st in zip(a_s, step)]) >= 8
        if opaque.sum() >= 16:
            x, xr, side_id, weight = x[opaque], xr[opaque], side_id[opaque], weight[opaque]
        else:   # the frame is already transparent: that is the background, no colour is
            x = x[:0]

    lab_c: list[np.ndarray] = []
    rgb_c: list[np.ndarray] = []
    if len(x):
        lab_c, rgb_c = _frame_colours(x, xr, side_id, weight)
    for (sx, sy) in seeds_px:   # clicked points: their colour is background too
        y0, y1, x0, x1 = max(0, sy - 1), min(h, sy + 2), max(0, sx - 1), min(w, sx + 2)
        lab_c.append(lab[y0:y1, x0:x1].reshape(-1, 3).mean(0))
        rgb_c.append(rgb[y0:y1, x0:x1].reshape(-1, 3).astype(np.float32).mean(0))
    return np.array(lab_c, np.float32).reshape(-1, 3), np.array(rgb_c, np.float32).reshape(-1, 3)


def _frame_colours(x, xr, side_id, weight) -> tuple[list, list]:
    """Cluster the frame samples and keep the colour groups that surround the picture."""
    centers = _kmeans(x, 6)
    assign = np.argmin(((x[:, None, :] - centers[None]) ** 2).sum(-1), 1)
    # weighted share of each edge covered by each cluster
    frac = np.zeros((len(centers), 4), np.float32)
    for s in range(4):
        m = side_id == s
        if m.any():
            frac[:, s] = np.bincount(assign[m], weights=weight[m], minlength=len(centers))[:len(centers)] / m.sum()
    # tiny clusters are mostly blended edge pixels; leaving them out stops them from bridging
    # the background and the subject into one colour group
    big = [i for i in range(len(centers)) if (assign == i).sum() >= 0.03 * len(x)] \
        or [int(np.argmax(np.bincount(assign)))]
    groups = [[big[i] for i in g] for g in _groups(centers[big])]
    scores = [float(frac[g].sum()) for g in groups]
    best = max(scores)
    keep = [i for g, s in zip(groups, scores) if s >= 0.4 * best for i in g]
    lab_c = [centers[i] for i in keep]
    rgb_c = [xr[assign == i].mean(0) if (assign == i).any() else np.zeros(3, np.float32) for i in keep]
    return lab_c, rgb_c


# ---------------------------------------------------------------- connected regions
class Regions:
    """Connected components of a boolean mask, stored as horizontal runs (fast in pure numpy/python)."""

    def __init__(self, mask: np.ndarray, eight: bool = False):
        self.shape = mask.shape
        h, w = mask.shape
        m = np.zeros((h, w + 2), np.int8)
        m[:, 1:-1] = mask
        d = np.diff(m, axis=1)
        ys, xs = np.nonzero(d)
        v = d[ys, xs]
        self.y = ys[v == 1]
        self.s = xs[v == 1]
        self.e = xs[v == -1]            # exclusive end
        n = len(self.y)
        ptr = np.searchsorted(self.y, np.arange(h + 1))
        parent = list(range(n))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a
        S, E = self.s.tolist(), self.e.tolist()
        g = 1 if eight else 0
        for row in range(1, h):
            i, iend, j, jend = ptr[row - 1], ptr[row], ptr[row], ptr[row + 1]
            while i < iend and j < jend:
                if S[i] < E[j] + g and S[j] < E[i] + g:
                    a, b = find(i), find(j)
                    if a != b:
                        parent[a] = b
                if E[i] < E[j]:
                    i += 1
                else:
                    j += 1
        roots = np.array([find(a) for a in range(n)], np.int64) if n else np.zeros(0, np.int64)
        _, self.label = np.unique(roots, return_inverse=True)
        self.n = int(self.label.max()) + 1 if n else 0
        self.area = np.bincount(self.label, weights=self.e - self.s, minlength=self.n) if n else np.zeros(0)

    def touching_border(self) -> np.ndarray:
        h, w = self.shape
        sel = np.zeros(self.n, bool)
        edge = (self.y == 0) | (self.y == h - 1) | (self.s == 0) | (self.e == w)
        sel[self.label[edge]] = True
        return sel

    def at(self, points: list[tuple[int, int]]) -> np.ndarray:
        sel = np.zeros(self.n, bool)
        for (x, y) in points:
            k = np.nonzero((self.y == y) & (self.s <= x) & (self.e > x))[0]
            if len(k):
                sel[self.label[k[0]]] = True
        return sel

    def paint(self, sel: np.ndarray) -> np.ndarray:
        h, w = self.shape
        acc = np.zeros((h, w + 1), np.int32)
        k = sel[self.label] if self.n else np.zeros(0, bool)
        np.add.at(acc, (self.y[k], self.s[k]), 1)
        np.add.at(acc, (self.y[k], self.e[k]), -1)
        return np.cumsum(acc, 1)[:, :w] > 0


# ---------------------------------------------------------------- the cut-out
def _tol(t: int) -> float:
    return 3.0 + max(0, min(100, t)) * 0.42      # ΔE (Lab distance): 3 … 45


def subject_mask(small: Image.Image, c: Cutout) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Foreground mask at working resolution + the background palette (Lab, RGB)."""
    rgb = np.asarray(small.convert("RGB"))
    lab = to_lab(rgb)
    h, w = lab.shape[:2]
    seeds = [(min(w - 1, max(0, int(x * w))), min(h - 1, max(0, int(y * h)))) for x, y in c.seeds]
    alpha = np.asarray(small.split()[3]) if small.mode == "RGBA" else None
    pal_lab, pal_rgb = background_palette(rgb, lab, seeds, alpha)
    tol = _tol(c.tolerance)
    dist, _ = _min_dist(lab, pal_lab)
    cand = dist < tol
    if alpha is not None:                              # already-transparent pixels are background
        cand |= alpha < 8
    reg = Regions(cand)
    sel = reg.touching_border() | reg.at(seeds)
    if c.holes:
        sel |= reg.area >= 0.0004 * h * w
    bg = reg.paint(sel)
    fg = ~bg
    if c.specks and fg.any():
        fr = Regions(fg, eight=True)
        keep = fr.area >= max(0.0015 * h * w, 0.02 * fr.area.max())
        fg = fr.paint(keep)
        # fill small background-coloured pinholes inside the subject
        br = Regions(~fg)
        small_holes = (~br.touching_border()) & (br.area < 0.0008 * h * w) & ~br.at(seeds)
        fg |= br.paint(small_holes)
    return fg, pal_lab, pal_rgb


def _smoothstep(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    t = np.clip((x - lo) / max(1e-6, hi - lo), 0, 1)
    return t * t * (3 - 2 * t)


def _stroke_masks(size: tuple[int, int], strokes: list[Stroke]) -> tuple[np.ndarray, np.ndarray] | None:
    if not strokes:
        return None
    w, h = size
    side = max(w, h)
    ov = Image.new("L", size, 0)
    d = ImageDraw.Draw(ov)
    for st in strokes:
        val = 1 if st.mode == "erase" else 2
        r = max(1.0, st.radius * side)
        pts = [(x * w, y * h) for x, y in st.points]
        if len(pts) > 1:
            d.line(pts, fill=val, width=max(1, int(round(2 * r))), joint="curve")
        for x, y in pts:
            d.ellipse((x - r, y - r, x + r, y + r), fill=val)
    a = np.asarray(ov)
    soft = max(0.6, side / 1500)
    er = np.asarray(Image.fromarray((a == 1).astype(np.uint8) * 255).filter(ImageFilter.GaussianBlur(soft)),
                    np.float32) / 255
    kp = np.asarray(Image.fromarray((a == 2).astype(np.uint8) * 255).filter(ImageFilter.GaussianBlur(soft)),
                    np.float32) / 255
    return er, kp


def _box(a: np.ndarray, r: int) -> np.ndarray:
    """Box blur along both axes (float, any number of channels)."""
    for ax in (0, 1):
        pad = [(0, 0)] * a.ndim
        pad[ax] = (r + 1, r)
        cs = np.cumsum(np.pad(a, pad, mode="edge"), axis=ax, dtype=np.float64)
        n = a.shape[ax]
        a = (np.take(cs, np.arange(2 * r + 1, 2 * r + 1 + n), axis=ax)
             - np.take(cs, np.arange(0, n), axis=ax)) / (2 * r + 1)
    return a.astype(np.float32)


def _spread(rgb: np.ndarray, known: np.ndarray, r: int, low: int = 160) -> np.ndarray | None:
    """Smoothly extend the colours of the `known` pixels over their surroundings (normalised blur).
    Done at a low resolution: these colour fields are smooth, and it keeps the preview fast."""
    if not known.any():
        return None
    h, w = known.shape
    f = max(1, -(-max(h, w) // low))
    H, W = -(-h // f) * f, -(-w // f) * f
    mk = np.zeros((H, W), np.float32)
    mk[:h, :w] = known
    px = np.zeros((H, W, 3), np.float32)
    px[:h, :w] = rgb * known[..., None]
    mk = mk.reshape(H // f, f, W // f, f).mean((1, 3))
    px = px.reshape(H // f, f, W // f, f, 3).mean((1, 3))
    out = np.zeros_like(px)
    todo = np.ones(mk.shape, bool)
    rr = max(1, round(r / f))
    for radius in (rr, rr * 3, rr * 9, max(mk.shape)):
        den = _box(_box(mk, radius), radius)
        num = _box(_box(px, radius), radius)
        ok = todo & (den > 1e-4)
        out[ok] = num[ok] / den[ok, None]
        todo &= ~ok
        if not todo.any():
            break
    out[todo] = rgb[known].mean(0)
    return out


def _upscale(a: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    if a.shape[1] == size[0] and a.shape[0] == size[1]:
        return a
    return np.asarray(Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB").resize(size, Image.BILINEAR),
                      np.float32)


def remove_background(im: Image.Image, c: Cutout | None = None, work_side: int = SAVE_WORK_SIDE) -> Image.Image:
    """Return an RGBA image with the background made transparent."""
    c = c or Cutout()
    src = im.convert("RGBA")
    w, h = src.size
    k = min(1.0, work_side / max(w, h))
    small = src if k >= 1 else src.resize((max(1, round(w * k)), max(1, round(h * k))), Image.BILINEAR)
    fg, pal_lab, pal_rgb = subject_mask(small, c)

    rgba = np.asarray(src)
    rgb = rgba[..., :3].astype(np.float32)
    s = max(0, min(100, c.softness)) / 100

    # soft, upscaled mask
    m = Image.fromarray(fg.astype(np.uint8) * 255).filter(ImageFilter.MedianFilter(3))
    a_mask = np.asarray(m.filter(ImageFilter.GaussianBlur(1.0)).resize((w, h), Image.BILINEAR), np.float32) / 255
    alpha = (a_mask >= 0.5).astype(np.float32)

    # edge zone: a few working pixels either side of the outline (hair, fur, soft edges live here)
    ring = 4
    boxed = np.asarray(m.filter(ImageFilter.BoxBlur(ring)))
    sure_fg = boxed >= 254
    sure_bg = boxed <= 1
    zone_small = ~sure_fg & ~sure_bg
    if zone_small.any():
        srgb = np.asarray(small.convert("RGB"), np.float32)
        f_col = _spread(srgb, sure_fg, ring * 2)
        b_col = _spread(srgb, sure_bg, ring * 2)
        if f_col is not None and b_col is not None:
            zone = np.asarray(Image.fromarray(zone_small.astype(np.uint8) * 255)
                              .filter(ImageFilter.MaxFilter(3)).resize((w, h), Image.BILINEAR)) > 0
            F = _upscale(f_col, (w, h))[zone]
            B = _upscale(b_col, (w, h))[zone]
            I = rgb[zone]
            fb = F - B
            n2 = (fb * fb).sum(1)
            proj = np.clip(((I - B) * fb).sum(1) / np.maximum(n2, 1e-3), 0, 1)
            proj = _smoothstep(proj, 0.18 * (1 - s), 1 - 0.18 * (1 - s))
            # where subject and background colours are alike the colours can't decide: trust the mask
            conf = np.clip((np.sqrt(n2) - 12) / 28, 0, 1)
            a = conf * proj + (1 - conf) * np.clip((a_mask[zone] - 0.5) * 3 + 0.5, 0, 1)
            alpha[zone] = a
            # remove the background tint from semi-transparent edge pixels
            semi = (a > 0.08) & (a < 0.98)
            a3 = a[semi, None]
            I[semi] = np.clip(B[semi] + (I[semi] - B[semi]) / a3, 0, 255)
            I[a <= 0.08] = F[a <= 0.08]
            rgb[zone] = I

    if s > 0:
        r = (0.3 + 1.2 * s) * max(1.0, max(w, h) / 2000)
        alpha = np.asarray(Image.fromarray(np.round(alpha * 255).astype(np.uint8))
                           .filter(ImageFilter.GaussianBlur(r)), np.float32) / 255
    alpha *= rgba[..., 3].astype(np.float32) / 255

    sm = _stroke_masks((w, h), c.strokes)
    if sm is not None:
        er, kp = sm
        alpha = alpha * (1 - er)
        orig_a = rgba[..., 3].astype(np.float32) / 255
        alpha = alpha + (orig_a - alpha) * kp
        rgb = rgb + (rgba[..., :3].astype(np.float32) - rgb) * kp[..., None]

    out = np.dstack([np.clip(np.round(rgb), 0, 255).astype(np.uint8),
                     np.clip(np.round(alpha * 255), 0, 255).astype(np.uint8)])
    res = Image.fromarray(out, "RGBA")
    if c.trim:
        box = Image.fromarray((out[..., 3] > 8).astype(np.uint8) * 255).getbbox()
        if box:
            pad = max(2, round(max(w, h) * 0.01))
            res = res.crop((max(0, box[0] - pad), max(0, box[1] - pad), min(w, box[2] + pad), min(h, box[3] + pad)))
    return res


def removed_share(out: Image.Image) -> float:
    """Fraction of the picture that became transparent (for the notification)."""
    a = np.asarray(out.split()[3])
    return float((a < 128).mean())


def checker(size: tuple[int, int], cell: int = 10, dark: bool = False) -> Image.Image:
    """Checkerboard so transparency is visible in previews."""
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    on = ((yy // cell + xx // cell) % 2).astype(bool)
    a, b = ((58, 58, 66), (44, 44, 52)) if dark else ((255, 255, 255), (214, 214, 222))
    arr = np.where(on[..., None], np.array(a, np.uint8), np.array(b, np.uint8))
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def on_backdrop(cut: Image.Image, backdrop: str = "checker") -> Image.Image:
    """Composite a cut-out over a checkerboard / white / dark backdrop for display."""
    if backdrop == "white":
        base = Image.new("RGB", cut.size, (255, 255, 255))
    elif backdrop == "dark":
        base = Image.new("RGB", cut.size, (24, 24, 28))
    else:
        base = checker(cut.size, max(6, max(cut.size) // 60))
    base.paste(cut, (0, 0), cut)
    return base
