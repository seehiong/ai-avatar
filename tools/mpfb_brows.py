"""Blender: replace a MetaHuman's eyebrow cards with the MPFB/MakeHuman eyebrow strip (CC0) used on
our MPFB heads - a slim, painted, alpha-masked brow instead of the broad strand cards.

Run on the metahuman-to-glb stage-02 .blend, before stage 03:
  blender --background v2.blend --python tools/mpfb_brows.py -- --mpfb <an MPFB GLB with eyebrow002, e.g. built with tools/build_mpfb_avatar.py> [--drop 6]

- the brow strip is lifted from the MPFB GLB, scaled/placed over the MetaHuman brow cards' footprint,
  then subdivided and projected onto the face skin (1.2 mm off the surface)
- every face shape key (ARKit) is copied onto it from the nearest skin vertex, so it raises/frowns
- the old brow cards are deleted; the new mesh is named Eyebrows_MPFB so the app binds it to the head
"""
import sys

import bmesh
import bpy
from mathutils import Vector
from mathutils.kdtree import KDTree

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
mpfb = argv[argv.index("--mpfb") + 1]
out = argv[argv.index("--out") + 1] if "--out" in argv else bpy.data.filepath
drop = float(argv[argv.index("--drop") + 1]) / 1000 if "--drop" in argv else 0.0   # mm, move the brow down

face = next(o for o in bpy.data.objects if o.type == "MESH" and "FaceMesh" in o.name)
cards = [o for o in bpy.data.objects if o.type == "MESH" and o.name.startswith("Eyebrows_")]
if not cards: sys.exit("no MetaHuman eyebrow cards found")
pts = [c.matrix_world @ v.co for c in cards for v in c.data.vertices]
lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
card_parent = cards[0].parent

# ---------------------------------------------------------------- bring in the MPFB brow
before = set(bpy.data.objects)
bpy.ops.import_scene.gltf(filepath=mpfb)
imported = [o for o in bpy.data.objects if o not in before]
src = next(o for o in imported if o.type == "MESH" and "eyebrow" in o.name.lower())
brow = src.copy(); brow.data = src.data.copy(); brow.name = brow.data.name = "Eyebrows_MPFB"
bpy.context.scene.collection.objects.link(brow)
brow.parent = None; brow.modifiers.clear(); brow.vertex_groups.clear()
if brow.data.shape_keys: brow.shape_key_clear()
brow.data.transform(src.matrix_world); brow.matrix_world.identity()
for o in imported: bpy.data.objects.remove(o, do_unlink=True)

# ---------------------------------------------------------------- fit over the MetaHuman brow footprint
bv = [v.co for v in brow.data.vertices]
blo = Vector((min(p.x for p in bv), min(p.y for p in bv), min(p.z for p in bv)))
bhi = Vector((max(p.x for p in bv), max(p.y for p in bv), max(p.z for p in bv)))
sx = (hi.x - lo.x) / (bhi.x - blo.x)                         # match the brow span
bc, mc = (blo + bhi) / 2, (lo + hi) / 2
for v in brow.data.vertices:
    p = v.co - bc
    v.co = Vector((mc.x + p.x * sx, mc.y + p.y * sx, mc.z - drop + p.z * sx))

# project onto the face *skin* (not eyes/lashes) and float 0.5 mm above it
# the skin is the section with the most faces - not the slot *named* Skin: UE's exporter shifts the face
# material slots by one (see fix_metahuman_glb.py), so "Skin" sits on the tear-line strip at this stage
counts = {}
for p in face.data.polygons: counts[p.material_index] = counts.get(p.material_index, 0) + 1
skin_slot = max(counts, key=counts.get)
bm = bmesh.new(); bm.from_mesh(face.data)
bmesh.ops.delete(bm, geom=[f for f in bm.faces if f.material_index != skin_slot], context="FACES")
skin = bpy.data.meshes.new("tmp_skin"); bm.to_mesh(skin); bm.free()
target = bpy.data.objects.new("tmp_skin", skin); target.matrix_world = face.matrix_world
bpy.context.scene.collection.objects.link(target)
# subdivide first: at 124 verts the flat triangles cut through the curved brow ridge between vertices
bm = bmesh.new(); bm.from_mesh(brow.data)
bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=3, use_grid_fill=True)
bm.to_mesh(brow.data); bm.free()
sw = brow.modifiers.new("fit", "SHRINKWRAP"); sw.target = target; sw.wrap_method = "NEAREST_SURFACEPOINT"
sw.wrap_mode = "OUTSIDE_SURFACE"; sw.offset = 0.0012
bpy.context.view_layer.objects.active = brow; brow.select_set(True)
bpy.ops.object.modifier_apply(modifier="fit")

# ---------------------------------------------------------------- expressions: copy face shape-key deltas
keys = face.data.shape_keys
if keys:
    fw = face.matrix_world; rot = fw.to_3x3()
    basis = keys.reference_key.data
    skin_vi = sorted({vi for p in face.data.polygons if p.material_index == skin_slot for vi in p.vertices})
    kd = KDTree(len(skin_vi))
    for vi in skin_vi: kd.insert(fw @ basis[vi].co, vi)
    kd.balance()
    # average over a few nearby skin vertices (smooth), and ignore the eye shapes: the skin just above the
    # lid follows eyeBlink/Squint/Wide, but real brows barely move on a blink - copying them made the brow
    # jump up and down with every blink
    near = []
    for v in brow.data.vertices:
        hits = kd.find_n(v.co, 6)
        w = [1.0 / (d + 1e-4) for _, _, d in hits]; t = sum(w)
        near.append([(i, wi / t) for (_, i, _), wi in zip(hits, w)])
    brow.shape_key_add(name="Basis")
    for kb in keys.key_blocks[1:]:
        sk = brow.shape_key_add(name=kb.name)
        if kb.name.startswith("eye"): continue                 # stays at the basis
        for v, nb in zip(brow.data.vertices, near):
            d = sum(((kb.data[i].co - basis[i].co) * wi for i, wi in nb), Vector())
            sk.data[v.index].co = v.co + rot @ d
    print(f"[brows] copied {len(keys.key_blocks) - 1} shape keys")

bpy.data.objects.remove(target, do_unlink=True); bpy.data.meshes.remove(skin)
for c in cards: bpy.data.objects.remove(c, do_unlink=True)
if card_parent:
    mw = brow.matrix_world.copy(); brow.parent = card_parent; brow.matrix_world = mw
for m in brow.data.materials: m.name = "Eyebrows_MPFB_Mat"
print(f"[brows] MPFB brow: {len(brow.data.vertices)} verts, span x {lo.x:.3f}..{hi.x:.3f}, z ~{mc.z:.3f}")
bpy.ops.wm.save_as_mainfile(filepath=out)
print("[brows] saved", out)
