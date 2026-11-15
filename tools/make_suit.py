"""Blender: tailor an original business suit (charcoal jacket, white shirt + collar, navy tie, buttons)
onto a MetaHuman exported by metahuman-to-glb, so it moves exactly with the character.

Run on the stage-02 .blend, before stage 03:
  blender --background v2.blend --python tools/make_suit.py -- [--out v2.blend]

MetaHuman bodies have the skin under the clothing removed, so the torso surface IS the default T-shirt:
- jacket body = the T-shirt mesh, repainted in its own UV space (V-neck showing the shirt, tie, lapels,
  buttons, centre seam, shirt collar) and nudged out a few mm
- long sleeves = the body's arm skin (upper/lower arm, not hands), pushed out 7 mm, with white cuffs
- the shorts are darkened to read as trousers
Everything keeps its original skin weights and skeleton binding.
"""
import re
import sys

import bmesh
import bpy
import numpy as np

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
out = argv[argv.index("--out") + 1] if "--out" in argv else bpy.data.filepath
TEX = 2048
JACKET = np.array([0.105, 0.108, 0.122]); LAPEL = np.array([0.14, 0.143, 0.158])
SHIRT = np.array([0.90, 0.91, 0.92]); TIE = np.array([0.07, 0.09, 0.20]); BUTTON = np.array([0.04, 0.04, 0.045])


def weights(o, pattern):
    names = {g.index: g.name for g in o.vertex_groups}
    w = np.zeros(len(o.data.vertices))
    for v in o.data.vertices:
        for g in v.groups:
            if re.search(pattern, names[g.group]): w[v.index] += g.weight
    return w


def dominant(o):
    names = {g.index: g.name for g in o.vertex_groups}
    return [names[max(v.groups, key=lambda g: g.weight).group] if v.groups else "" for v in o.data.vertices]


def fabric(p, col):
    weave = 1 + 0.03 * np.sin(p[:, 0] * 2400) * np.sin(p[:, 2] * 2400) \
              + 0.02 * np.random.default_rng(0).standard_normal(len(p))
    return np.clip(col * weave[:, None], 0, 1)


def rasterize(o, paint, faces_ok=None, extra=()):
    """Paint a TEX x TEX texture in o's active UV space; paint(P, N, *extras) -> rgb per texel."""
    me = o.data; me.calc_loop_triangles()
    uv = me.uv_layers.active.data
    co = np.array([v.co[:] for v in me.vertices]); vn = np.array([v.normal[:] for v in me.vertices])
    img = np.zeros((TEX, TEX, 4), np.float32)
    for tri in me.loop_triangles:
        if faces_ok is not None and not faces_ok(tri.polygon_index): continue
        vi = list(tri.vertices); uvs = np.array([uv[l].uv[:] for l in tri.loops]) * TEX
        x0, y0 = np.floor(uvs.min(0)).astype(int); x1, y1 = np.ceil(uvs.max(0)).astype(int)
        if x1 - x0 > TEX // 2 or y1 - y0 > TEX // 2: continue
        gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        pts = np.stack([gx.ravel(), gy.ravel()], 1)
        a, b, c = uvs; v0, v1 = b - a, c - a; den = v0[0] * v1[1] - v1[0] * v0[1]
        if abs(den) < 1e-12: continue
        d = pts - a
        l1 = (d[:, 0] * v1[1] - v1[0] * d[:, 1]) / den; l2 = (v0[0] * d[:, 1] - d[:, 0] * v0[1]) / den
        l0 = 1 - l1 - l2; m = (l0 >= -0.03) & (l1 >= -0.03) & (l2 >= -0.03)
        if not m.any(): continue
        w = np.stack([l0[m], l1[m], l2[m]], 1)
        px = np.floor(pts[m]).astype(int)
        ok = (px[:, 0] >= 0) & (px[:, 0] < TEX) & (px[:, 1] >= 0) & (px[:, 1] < TEX)
        rgb = paint(w[ok] @ co[vi], w[ok] @ vn[vi], *[w[ok] @ e[vi] for e in extra])
        img[px[ok, 1], px[ok, 0], :3] = rgb; img[px[ok, 1], px[ok, 0], 3] = 1
    for _ in range(8):                                        # dilate islands so seams don't bleed
        empty = img[:, :, 3] == 0
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            sh = np.roll(np.roll(img, dy, 0), dx, 1)
            fill = empty & (sh[:, :, 3] > 0); img[fill] = sh[fill]; empty &= ~fill
    img[:, :, 3] = 1
    return img


def make_material(name, img, rough=0.78):
    im = bpy.data.images.new(name + "_BaseColor", TEX, TEX, alpha=False)
    im.pixels.foreach_set(img.ravel()); im.file_format = "PNG"; im.pack()
    mat = bpy.data.materials.new(name); mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    tn = mat.node_tree.nodes.new("ShaderNodeTexImage"); tn.image = im
    mat.node_tree.links.new(tn.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = rough; bsdf.inputs["Metallic"].default_value = 0.0
    return mat


def push(o, mm, mask=None):
    for v in o.data.vertices:
        if mask is None or mask[v.index]:
            v.co = v.co + v.normal * (mm / 1000)


# ---------------------------------------------------------------- jacket from the T-shirt
outfit = next(o for o in bpy.data.objects if o.type == "MESH" and "Outfit" in o.name)
ms = [m.name if m else "" for m in outfit.data.materials]
shirt_slot = next(i for i, n in enumerate(ms) if "Shirt" in n)
shirt_faces = {p.index for p in outfit.data.polygons if p.material_index == shirt_slot}
on_shirt = np.zeros(len(outfit.data.vertices), bool)
for p in outfit.data.polygons:
    if p.index in shirt_faces: on_shirt[list(p.vertices)] = True
co = np.array([v.co[:] for v in outfit.data.vertices]); vn = np.array([v.normal[:] for v in outfit.data.vertices])
sc, sn = co[on_shirt], vn[on_shirt]
front_c = sc[(sn[:, 1] < -0.3) & (np.abs(sc[:, 0]) < 0.03)]
neck_top = front_c[:, 2].max(); shirt_bot = sc[:, 2].min()
V_DEPTH = 0.30 * (neck_top - shirt_bot); V_HALF = 0.085; v_bot = neck_top - V_DEPTH
neck_w = weights(outfit, r"neck|spine_05")
print(f"[suit] shirt z {shirt_bot:.3f}..{neck_top:.3f}  V bottom {v_bot:.3f}")


def paint_jacket(p, n, nk):
    x, z = p[:, 0], p[:, 2]; ax = np.abs(x)
    fr = n[:, 1] < -0.15
    col = np.repeat(JACKET[None], len(p), 0)
    t = np.clip((z - v_bot) / V_DEPTH, 0, 1)
    hw = 0.006 + V_HALF * t
    in_v = fr & (z > v_bot) & (ax < hw)
    col[fr & (z > v_bot - 0.025) & (ax >= hw) & (ax < hw + 0.03 + 0.02 * t)] = LAPEL      # lapels
    col[fr & (z > v_bot) & (np.abs(ax - hw) < 0.0022)] = JACKET * 0.5                    # lapel edge
    col[in_v] = SHIRT
    tie = in_v & ((ax < 0.009 + 0.012 * (1 - t)) | ((z > neck_top - 0.035) & (ax < 0.014)))
    col[tie] = TIE
    col[tie & (np.mod((z + x * 0.8) * 90, 1.0) < 0.12)] = TIE * 1.8                      # tie stripe
    col[fr & (z < v_bot) & (ax < 0.0016)] = JACKET * 0.6                                 # front closure
    for bz in (v_bot - 0.035, v_bot - 0.12):
        col[fr & (np.hypot(x - 0.012, z - bz) < 0.0075)] = BUTTON
    col[(z > neck_top - 0.022) & ~fr | (z > neck_top - 0.012)] = SHIRT                  # shirt collar band
    return fabric(p, col)


img = rasterize(outfit, paint_jacket, faces_ok=lambda i: i in shirt_faces, extra=(neck_w,))
outfit.data.materials[shirt_slot] = make_material("Suit_Jacket_Mat", img)
push(outfit, 3, on_shirt)                                     # a touch fuller than the T-shirt
for m in outfit.data.materials:                               # shorts -> dark trousers
    if m and "Short" in m.name and m.use_nodes:
        p = m.node_tree.nodes.get("Principled BSDF")
        if p:
            for l in list(p.inputs["Base Color"].links): m.node_tree.links.remove(l)
            p.inputs["Base Color"].default_value = (0.03, 0.03, 0.035, 1)

# ---------------------------------------------------------------- long sleeves from the arm skin
body = next(o for o in bpy.data.objects if o.type == "MESH" and "BodyMesh" in o.name)
sl = body.copy(); sl.data = body.data.copy(); sl.name = sl.data.name = "Suit_Sleeves"
for c in body.users_collection: c.objects.link(sl)
dom = dominant(sl)
keep = np.array([bool(re.search(r"upperarm|lowerarm|wrist_outer", d)) for d in dom])
bm = bmesh.new(); bm.from_mesh(sl.data)
bmesh.ops.delete(bm, geom=[f for f in bm.faces if not all(keep[v.index] for v in f.verts)], context="FACES")
bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
bm.to_mesh(sl.data); bm.free()
push(sl, 7)
hand_w = weights(sl, r"hand|wrist")


def paint_sleeve(p, n, hw):
    col = np.repeat(JACKET[None], len(p), 0)
    col[hw > 0.10] = SHIRT                                    # shirt cuff peeking out at the wrist
    return fabric(p, col)


img = rasterize(sl, paint_sleeve, extra=(hand_w,))
sl.data.materials.clear(); sl.data.materials.append(make_material("Suit_Sleeve_Mat", img))
print(f"[suit] sleeves: {len(sl.data.vertices)} verts")

bpy.ops.wm.save_as_mainfile(filepath=out)
print("[suit] saved", out)
