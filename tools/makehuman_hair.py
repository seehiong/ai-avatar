"""Blender: replace a MetaHuman's groom (hair cards + helmet) with a CC0 MakeHuman hair mesh, fitted to the head.

Run on the metahuman-to-glb stage-02 .blend (after make_suit.py):
  blender --background v2.blend --python tools/makehuman_hair.py -- --hair <MPFB data>/hair/bob01 [--lift 2.5]

- The MakeHuman hair (.obj + its diffuse texture; check the .mhclo "# license" line - system assets and
  e.g. the toigo_* bobs are CC0, several community hairs are AGPL3) is scaled into the bounding box of
  the groom it replaces, so it sits where the UE hair sat.
- Every vertex is then kept at least --lift mm outside the face skin (nearest-surface test), so the skull
  never pokes through.
- The UE hair cards and helmet are removed; the new mesh is named Hair_MH_<name> so the app binds it to the
  head bone; its material is alpha-tested from the texture's alpha.
"""
import os
import sys

import bmesh
import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
hair_dir = argv[argv.index("--hair") + 1].rstrip("/")
lift = float(argv[argv.index("--lift") + 1]) / 1000 if "--lift" in argv else 0.0025
out = argv[argv.index("--out") + 1] if "--out" in argv else bpy.data.filepath
name = os.path.basename(hair_dir)

groom = [o for o in bpy.data.objects if o.type == "MESH" and o.name.startswith("Hair_")]
if not groom: sys.exit("no Hair_* groom objects to replace")
pts = [o.matrix_world @ v.co for o in groom if "Helmet" not in o.name for v in o.data.vertices] or \
      [o.matrix_world @ v.co for o in groom for v in o.data.vertices]
lo = Vector([min(p[i] for p in pts) for i in range(3)]); hi = Vector([max(p[i] for p in pts) for i in range(3)])
parent = groom[0].parent

# ---------------------------------------------------------------- import the MakeHuman hair
obj_path = next(os.path.join(hair_dir, f) for f in os.listdir(hair_dir) if f.endswith(".obj"))
before = set(bpy.data.objects)
bpy.ops.wm.obj_import(filepath=obj_path)                       # MakeHuman OBJ: Y-up -> Blender Z-up on import
hair = next(o for o in bpy.data.objects if o not in before and o.type == "MESH")
hair.data.transform(hair.matrix_world); hair.matrix_world.identity()
hair.name = hair.data.name = "Hair_MH_" + name

face = next(o for o in bpy.data.objects if o.type == "MESH" and "FaceMesh" in o.name)
counts = {}
for p in face.data.polygons: counts[p.material_index] = counts.get(p.material_index, 0) + 1
skin_slot = max(counts, key=counts.get)              # the skin section (UE shifts the slot names; see fix_metahuman_glb.py)
bm = bmesh.new(); bm.from_mesh(face.data)
bmesh.ops.delete(bm, geom=[f for f in bm.faces if f.material_index != skin_slot], context="FACES")
bm.transform(face.matrix_world)
tree = BVHTree.FromBMesh(bm)


def fitted(rot_deg):
    """Hair vertices turned rot_deg about the vertical axis, then scaled into the groom's footprint
    (uniform scale from width/depth, centred, crowns aligned)."""
    import math
    c_, s_ = math.cos(math.radians(rot_deg)), math.sin(math.radians(rot_deg))
    hv = [Vector((c_ * v.co.x - s_ * v.co.y, s_ * v.co.x + c_ * v.co.y, v.co.z)) for v in hair.data.vertices]
    hlo = Vector([min(p[i] for p in hv) for i in range(3)]); hhi = Vector([max(p[i] for p in hv) for i in range(3)])
    sc = 0.5 * ((hi.x - lo.x) / (hhi.x - hlo.x) + (hi.y - lo.y) / (hhi.y - hlo.y))
    ctr = Vector(((hlo.x + hhi.x) / 2, (hlo.y + hhi.y) / 2, hhi.z))
    return [Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, hi.z)) + (p - ctr) * sc for p in hv], sc


def inside(ps):
    n = 0
    for p in ps:
        loc, nrm, _, _ = tree.find_nearest(p)
        if loc is not None and (p - loc).dot(nrm) < 0: n += 1
    return n


# the OBJ importer's axis conversion doesn't always match MakeHuman's facing: try the four quarter turns and
# keep the one that overlaps the face least (a hair facing the wrong way has its fringe/back inside the face)
trials = {r: fitted(r) for r in (0, 90, 180, 270)}
scores = {r: inside(ps) for r, (ps, _) in trials.items()}
best = min(scores, key=scores.get)
ps, s = trials[best]
for v, p in zip(hair.data.vertices, ps): v.co = p
print(f"[mhhair] orientation: verts inside the head per turn {scores} -> {best} deg")

# ---------------------------------------------------------------- keep it outside the skull
pushed = 0
for v in hair.data.vertices:
    loc, nrm, _, _ = tree.find_nearest(v.co)
    if loc is None: continue
    d = (v.co - loc).dot(nrm)
    if d < lift:
        v.co = v.co + nrm * (lift - d); pushed += 1
bm.free()
print(f"[mhhair] {name}: {len(hair.data.vertices)} verts, scale {s:.4f}, pushed {pushed} verts out of the skull")

# ---------------------------------------------------------------- material: diffuse + alpha-tested
tex = next((os.path.join(hair_dir, f) for f in os.listdir(hair_dir)
            if f.lower().endswith(".png") and ("diffuse" in f.lower() or "hair" in f.lower()) and "normal" not in f.lower()
            and "spec" not in f.lower()), None)
mat = bpy.data.materials.new("MH_Hair_" + name); mat.use_nodes = True
bsdf = mat.node_tree.nodes["Principled BSDF"]
if tex:
    tn = mat.node_tree.nodes.new("ShaderNodeTexImage"); tn.image = bpy.data.images.load(tex); tn.image.pack()
    mat.node_tree.links.new(tn.outputs["Color"], bsdf.inputs["Base Color"])
    mat.node_tree.links.new(tn.outputs["Alpha"], bsdf.inputs["Alpha"])
bsdf.inputs["Roughness"].default_value = 0.6
mat.blend_method = "CLIP" if hasattr(mat, "blend_method") else None
mat.surface_render_method = "DITHERED" if hasattr(mat, "surface_render_method") else None
hair.data.materials.clear(); hair.data.materials.append(mat)
print(f"[mhhair] texture: {os.path.basename(tex) if tex else 'none'}")

for o in groom: bpy.data.objects.remove(o, do_unlink=True)
if parent:
    w = hair.matrix_world.copy(); hair.parent = parent; hair.matrix_world = w
bpy.ops.wm.save_as_mainfile(filepath=out)
print("[mhhair] saved", out)
