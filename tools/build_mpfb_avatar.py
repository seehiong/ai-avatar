"""Build an ARKit-52 + Oculus-viseme avatar GLB with MPFB, headless.

Scripted version of TalkingHead's blender/MPFB/MPFB.md GUI workflow:
  human from macros -> TalkingHead (Mixamo-style) rig -> skin/eyes/brows/lashes/
  teeth/tongue/hair/clothes (GameEngine PBR) -> export copy with Meta visemes +
  ARKit faceunits interpolated onto all parts -> material alpha fixes -> GLB.

Prerequisites (one-time, see tools/setup_mpfb.sh):
  - MPFB extension installed in Blender
  - asset packs: makehuman_system_assets, faceunits01, visemes02 (+ hair packs)
  - TalkingHead rig files (talkinghead.mpfbskel / talkinghead.mhw)

Usage:
  blender -b --python tools/build_mpfb_avatar.py -- \
      --out assets/models/mpfb.glb --rig-dir /path/to/talkinghead_rig \
      [--gender 1.0 --age 0.6 --skin young_caucasian_male --hair short02 \
       --clothes male_casualsuit04 --eyes brown --eyebrows eyebrow001 \
       --eyelashes eyelashes01 --save-blend out.blend]
"""

import argparse
import json
import os
import shutil
import sys

import bpy

from bl_ext.blender_org.mpfb.services import (AssetService, ExportService, FaceService,
                                               HumanService, LocationService, ObjectService,
                                               TargetService)


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--rig-dir", required=True, help="dir with talkinghead.mpfbskel and talkinghead.mhw")
    p.add_argument("--gender", type=float, default=1.0, help="0 female .. 1 male")
    p.add_argument("--age", type=float, default=0.6, help="0.5 = 25y, 1.0 = 90y")
    p.add_argument("--muscle", type=float, default=0.5)
    p.add_argument("--weight", type=float, default=0.5)
    p.add_argument("--height", type=float, default=0.5)
    p.add_argument("--skin", default="young_caucasian_male")
    p.add_argument("--eyes", default="brownlight", help="eye material in eyes/materials/")
    p.add_argument("--eyebrows", default="eyebrow010")
    p.add_argument("--brow-tint", default="4f3626", help="hex colour multiplied into brows/lashes; '' = none")
    p.add_argument("--eyelashes", default="eyelashes01")
    p.add_argument("--hair", default="short02")
    p.add_argument("--hair-tint", default="", help="hex colour multiplied into hair base colour; '' = none")
    p.add_argument("--clothes", default="male_casualsuit04")
    p.add_argument("--clothes-tint", default="", help="hex colour multiplied into clothes base colour; '' = none")
    p.add_argument("--beauty", action="store_true", help="Apply refined facial proportions (forehead, nose, jaw, lips, eyes)")
    p.add_argument("--fade-hairline", action="store_true", help="Fade hair boundary vertices (only for tight cap hairs like short02)")
    p.add_argument("--skin-type", default="MAKESKIN", choices=["MAKESKIN", "GAMEENGINE", "ENHANCED"],
                   help="MPFB2 skin shader mode (MAKESKIN preserves full bump/spec/roughness maps)")
    p.add_argument("--morph-normals", action="store_true", default=True,
                   help="Export morph target normals so FACS blendshapes shade accurately")
    p.add_argument("--save-blend", default=None)
    return p.parse_args(argv)


def user_data(*parts):
    return os.path.join(LocationService.get_user_data(), *parts)


def install_rig(rig_dir):
    """Equivalent of MakeRig 'Save rig to library' for the TalkingHead rig."""
    rigs = user_data("rigs")
    os.makedirs(rigs, exist_ok=True)
    with open(os.path.join(rig_dir, "talkinghead.mpfbskel")) as f:
        rig = json.load(f)
    rig["identifying_bones"] = ["LeftToe_End"]
    with open(os.path.join(rigs, "talkinghead.json"), "w") as f:
        json.dump(rig, f)
    shutil.copy(os.path.join(rig_dir, "talkinghead.mhw"), os.path.join(rigs, "weights.talkinghead.json"))
    AssetService.invalidate_custom_rig_cache()


def apply_mhmat(obj, mhmat_path):
    """Replace obj's materials with one built from an .mhmat (MPFB MakeSkin node tree)."""
    from bl_ext.blender_org.mpfb.entities.material.makeskinmaterial import MakeSkinMaterial
    from bl_ext.blender_org.mpfb.services import MaterialService
    while len(obj.data.materials) > 0:
        obj.data.materials.pop(index=0)
    material = MaterialService.create_empty_material(obj.name + ".eyes", obj)
    mhmat = MakeSkinMaterial()
    mhmat.populate_from_mhmat(mhmat_path)
    mhmat.apply_node_tree(material)


def find_asset(kind, name, ext):
    path = user_data(kind, name, name + ext)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    return path


BEAUTY_TARGETS = [
    ("forehead/forehead-scale-vert-decr.target.gz", 0.28),
    ("head/head-oval.target.gz", 0.28),
    ("head/head-fat-decr.target.gz", 0.15),
    ("chin/chin-width-decr.target.gz", 0.22),
    ("chin/chin-jaw-drop-decr.target.gz", 0.16),
    ("cheek/l-cheek-bones-incr.target.gz", 0.25),
    ("cheek/r-cheek-bones-incr.target.gz", 0.25),
    ("eyebrows/eyebrows-trans-up.target.gz", 0.18),
    ("eyebrows/eyebrows-angle-up.target.gz", 0.18),
    ("eyes/l-eye-scale-incr.target.gz", 0.14),
    ("eyes/r-eye-scale-incr.target.gz", 0.14),
    ("eyes/l-eye-height1-incr.target.gz", 0.12),
    ("eyes/r-eye-height1-incr.target.gz", 0.12),
    ("eyes/l-eye-corner2-up.target.gz", 0.14),
    ("eyes/r-eye-corner2-up.target.gz", 0.14),
    ("eyes/l-eye-bag-decr.target.gz", 0.35),
    ("eyes/r-eye-bag-decr.target.gz", 0.35),
    ("nose/nose-scale-horiz-decr.target.gz", 0.24),
    ("nose/nose-width2-decr.target.gz", 0.20),
    ("nose/nose-width3-decr.target.gz", 0.20),
    ("nose/nose-point-width-decr.target.gz", 0.24),
    ("nose/nose-point-up.target.gz", 0.12),
    ("mouth/mouth-angles-up.target.gz", 0.22),
    ("mouth/mouth-cupidsbow-incr.target.gz", 0.25),
    ("mouth/mouth-lowerlip-height-decr.target.gz", 0.18),
    ("mouth/mouth-upperlip-height-incr.target.gz", 0.10),
    ("mouth/mouth-scale-horiz-incr.target.gz", 0.08),
]


def build_human(a):
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)

    macros = TargetService.get_default_macro_info_dict()
    macros.update({"gender": a.gender, "age": a.age, "muscle": a.muscle,
                   "weight": a.weight, "height": a.height})
    basemesh = HumanService.create_human(macro_detail_dict=macros)
    if a.beauty:
        td = os.path.join(LocationService.get_mpfb_data(), "targets")
        for rel, w in BEAUTY_TARGETS:
            tp = os.path.join(td, rel)
            if os.path.exists(tp):
                TargetService.load_target(basemesh, tp, weight=w)
    HumanService.add_custom_rig(basemesh, "custom.talkinghead")

    HumanService.set_character_skin(find_asset("skins", a.skin, ".mhmat"), basemesh,
                                    skin_type=a.skin_type, material_instances=False)
    parts = [
        ("Eyes", find_asset("eyes", "high-poly", ".mhclo")),
        ("Eyebrows", find_asset("eyebrows", a.eyebrows, ".mhclo")),
        ("Eyelashes", find_asset("eyelashes", a.eyelashes, ".mhclo")),
        ("Teeth", find_asset("teeth", "teeth_base", ".mhclo")),
        ("Tongue", find_asset("tongue", "tongue01", ".mhclo")),
        ("Hair", find_asset("hair", a.hair, ".mhclo")),
        ("Clothes", find_asset("clothes", a.clothes, ".mhclo")),
    ]
    added = {}
    for asset_type, mhclo in parts:
        mat_type = "MAKESKIN" if asset_type in ("Eyes", "Teeth", "Tongue", "Hair") and a.skin_type == "MAKESKIN" else "GAMEENGINE"
        obj = HumanService.add_mhclo_asset(mhclo, basemesh, asset_type=asset_type,
                                           subdiv_levels=0, material_type=mat_type)
        added[asset_type] = obj.name.split(".")[-1]
        if asset_type == "Eyes":
            # Ensure iris/cornea MakeSkin material is explicitly applied
            apply_mhmat(obj, user_data("eyes", "materials", a.eyes + ".mhmat"))
        print("added", asset_type, os.path.basename(mhclo), "->", obj.name)
    return basemesh, added


def export_copy(basemesh):
    """Same steps as MPFB's 'Create export copy' operator with TalkingHead's options."""
    collection = bpy.data.collections.new("export copy")
    bpy.context.scene.collection.children.link(collection)
    copy_root = ExportService.create_character_copy(basemesh, name_suffix="_export",
                                                    place_in_collection=collection)
    new_basemesh = ObjectService.find_object_of_type_amongst_nearest_relatives(copy_root)
    TargetService.bake_targets(new_basemesh)
    FaceService.load_targets(new_basemesh, load_microsoft_visemes=False,
                             load_meta_visemes=True, load_arkit_faceunits=True)
    FaceService.interpolate_targets(new_basemesh)
    ExportService.bake_modifiers_remove_helpers(new_basemesh, bake_masks=True, bake_subdiv=True,
                                                remove_helpers=True, also_proxy=True)
    copy_root.name = "Armature"
    return collection


def recolor_image(node, tint):
    import numpy as np
    src = node.image
    img = src.copy()
    img.name = src.name + ".tinted"
    px = np.empty(len(src.pixels), dtype=np.float32)
    src.pixels.foreach_get(px)
    px = px.reshape(-1, 4)
    # keep a little of the original luminance variation for strand detail
    lum = px[:, :3].mean(axis=1, keepdims=True)
    px[:, :3] = np.clip(np.array(tint, dtype=np.float32) * (0.75 + lum), 0, 1)
    img.pixels.foreach_set(px.ravel())
    img.pack()
    node.image = img


def feather_alpha(node, radius=3, passes=2):
    """Box-blur an image's alpha channel (keeps RGB) to soften cut-out edges."""
    import numpy as np
    src = node.image
    w, h = src.size
    img = src.copy()
    img.name = src.name + ".feathered"
    px = np.empty(len(src.pixels), dtype=np.float32)
    src.pixels.foreach_get(px)
    px = px.reshape(h, w, 4)
    a = px[:, :, 3]
    k = 2 * radius + 1
    for _ in range(passes):
        for axis in (0, 1):
            pad = np.pad(a, [(radius, radius) if i == axis else (0, 0) for i in range(2)], mode="edge")
            c = np.cumsum(pad, axis=axis, dtype=np.float64)
            c = np.concatenate([np.zeros_like(c.take([0], axis=axis)), c], axis=axis)
            a = ((c.take(range(k, c.shape[axis]), axis=axis) - c.take(range(0, c.shape[axis] - k), axis=axis)) / k
                 ).astype(np.float32)
    px[:, :, 3] = np.minimum(px[:, :, 3], a * 1.15)   # only soften edges, never add hair where there was none
    img.pixels.foreach_set(px.ravel())
    img.pack()
    node.image = img


def fade_mesh_border(obj, width=0.015, skin=None):
    """Vertex-colour alpha ramp 0 -> 1 over `width` metres from the mesh's open border.

    A hair cap ends in a hard line on the forehead; glTF multiplies COLOR_0 alpha into the
    material, so the cap fades into the skin (soft hairline) without thinning the hair elsewhere.
    """
    import bmesh
    from mathutils import kdtree
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    border = [v.co.copy() for v in bm.verts if v.is_boundary]
    if not border:
        bm.free()
        return
    kd = kdtree.KDTree(len(border))
    for i, co in enumerate(border):
        kd.insert(co, i)
    kd.balance()
    def smooth(x):
        x = min(1.0, max(0.0, x))
        return x * x * (3 - 2 * x)

    # The visible hairline is mostly where the cap dips *into* the forehead skin (a depth
    # intersection), so also fade by distance to the body surface - but only on the front/lower
    # part of the cap, otherwise the whole scalp would go see-through.
    bvh, zs = None, [v.co.z for v in bm.verts]
    if skin is not None:
        from mathutils.bvhtree import BVHTree
        bvh = BVHTree.FromObject(skin, bpy.context.evaluated_depsgraph_get())
        z_top, z_low = max(zs), min(zs)
        y_mid = sum(v.co.y for v in bm.verts) / len(bm.verts)
    alpha = []
    for v in bm.verts:
        a = smooth(kd.find(v.co)[2] / width)
        if bvh is not None and v.co.y < y_mid and v.co.z < z_top - 0.35 * (z_top - z_low):
            hit = bvh.find_nearest(v.co)
            if hit[0] is not None:
                a = min(a, smooth((hit[3] - 0.001) / 0.006))   # within ~1-7 mm of skin -> fade
        alpha.append(a)
    bm.free()
    attr = obj.data.color_attributes.new("hairline", "FLOAT_COLOR", "POINT")
    for i, a in enumerate(alpha):
        attr.data[i].color = (1.0, 1.0, 1.0, a)
    obj.data.color_attributes.active_color = attr
    obj.data.color_attributes.render_color_index = obj.data.color_attributes.active_color_index


def inflate(obj, distance):
    """Push a mesh outward along its vertex normals (clothes clear the skin under deformation)."""
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.normal_update()
    for v in bm.verts:
        v.co += v.normal * distance
    bm.to_mesh(obj.data)
    bm.free()


def clean_hair_weights(hair_obj):
    """Ensure hair deforms cleanly with Head/Neck only (never torn by arm/shoulder poses)."""
    drop = ("shoulder", "arm", "hand", "breast", "clavicle", "spine")
    vgs = hair_obj.vertex_groups
    has_drop = any(any(k in g.name.lower() for k in drop) for g in vgs)
    if not has_drop:
        return
    all_idx = [g.index for g in vgs]
    head_vg = vgs.get("Head") or vgs.new(name="Head")
    neck_vg = vgs.get("Neck") or vgs.new(name="Neck")
    zs = [v.co.z for v in hair_obj.data.vertices]
    z_top, z_low = (max(zs), min(zs)) if zs else (1.54, 1.11)
    chin_z = z_top - 0.20
    ramp = max(0.04, chin_z - z_low)

    for v in hair_obj.data.vertices:
        for gi in all_idx:
            vgs[gi].remove([v.index])
        t = max(0.0, min(1.0, (chin_z - v.co.z) / ramp))
        # Smoothstep ramp below chin from 100% Head to 60% Head / 40% Neck
        nw = 0.40 * (t * t * (3.0 - 2.0 * t))
        head_vg.add([v.index], 1.0 - nw, "REPLACE")
        if nw > 1e-4:
            neck_vg.add([v.index], nw, "REPLACE")


def clear_mesh_over_surface(obj, surface_obj, clearance=0.007):
    """Lift any vertices buried inside or touching inflated clothes cleanly outward."""
    import bpy
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    dg = bpy.context.evaluated_depsgraph_get()
    bvh = BVHTree.FromObject(surface_obj, dg)
    inv = obj.matrix_world.inverted()
    deltas = []
    any_hit = False
    for v in obj.data.vertices:
        world_co = obj.matrix_world @ v.co
        loc, norm, _, dist = bvh.find_nearest(world_co)
        delta = Vector((0.0, 0.0, 0.0))
        if loc is not None and dist < 0.016:
            diff = world_co - loc
            signed_dist = diff.dot(norm)
            # Only push outward if inside clothes (signed_dist < 0) or closer than clearance
            if signed_dist < clearance:
                push_dir = norm if norm.length > 1e-6 else Vector((0.0, -1.0, 0.4)).normalized()
                push = clearance - signed_dist
                delta = (inv.to_3x3() @ (push_dir * push))
                any_hit = True
        deltas.append(delta)
    if not any_hit:
        return
    if obj.data.shape_keys and obj.data.shape_keys.key_blocks:
        for kb in obj.data.shape_keys.key_blocks:
            for i, d in enumerate(deltas):
                if d.length_squared > 0:
                    kb.data[i].co += d
    else:
        for v, d in zip(obj.data.vertices, deltas):
            if d.length_squared > 0:
                v.co += d
    obj.data.update()


def _base_tex_node(bsdf):
    bc = bsdf.inputs["Base Color"]
    if bc.links:
        n = bc.links[0].from_node
        if n.type == "TEX_IMAGE" and n.image is not None:
            return n
    al = bsdf.inputs["Alpha"]
    if al.links:
        n = al.links[0].from_node
        if n.type == "TEX_IMAGE" and n.image is not None:
            return n
    return None


def fix_materials(objs, cutout_names, alpha_cut=0.2, tint=None, hair_name=None, hair_tint=None,
                  clothes_name=None, clothes_tint=None):
    """glTF alpha: cutout for hair/brows/lashes, opaque for everything else."""
    for o in objs:
        if o.type != "MESH":
            continue
        if "high-poly" in o.name or "low-poly" in o.name:
            continue  # eyes: keep alpha (BLEND) so the cornea shell stays see-through
        cutout = any(k.lower() in o.name.lower() for k in cutout_names)
        for slot in o.material_slots:
            m = slot.material
            if m is None or not m.use_nodes:
                continue
            nt = m.node_tree
            bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if bsdf is None:
                continue
            if cutout:  # hair cards look plastic/white under rim light when glossy
                bsdf.inputs["Roughness"].default_value = 0.9
                for link in list(bsdf.inputs["Roughness"].links):
                    nt.links.remove(link)
            base_node = _base_tex_node(bsdf)
            if hair_name and hair_name.lower() in o.name.lower() and base_node is not None:
                # hair cards have hard alpha edges -> jagged hairline; feather the alpha so
                # alpha-to-coverage in the client can fade it out softly
                feather_alpha(base_node)
                if hair_tint:
                    recolor_image(base_node, hair_tint)
            if clothes_tint and clothes_name and clothes_name.lower() in o.name.lower() and base_node is not None:
                recolor_image(base_node, clothes_tint)
            if tint and any(k in o.name.lower() for k in ("eyebrow", "eyelash")) and base_node is not None:
                # brow/lash textures are near-black alpha masks; recolour the pixels
                # (keeping alpha) so they match the hair instead of looking drawn on
                recolor_image(base_node, tint)
            alpha_in = bsdf.inputs["Alpha"]
            if not alpha_in.links:
                continue
            src = alpha_in.links[0].from_socket
            for link in list(alpha_in.links):
                nt.links.remove(link)
            if cutout:
                gt = nt.nodes.new("ShaderNodeMath")
                gt.operation = "GREATER_THAN"
                # low threshold keeps the soft tips of brows/lashes/hair strands
                gt.inputs[1].default_value = alpha_cut
                nt.links.new(src, gt.inputs[0])
                nt.links.new(gt.outputs[0], alpha_in)
            else:
                alpha_in.default_value = 1.0


def export_glb(collection, out, cutout_names, tint=None, hair_name=None, clothes_name=None,
               clothes_inflate=0.004, morph_normals=True, hair_tint=None, clothes_tint=None,
               fade_hairline=False):
    bpy.ops.object.select_all(action="DESELECT")
    objs = list(collection.all_objects)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = next(o for o in objs if o.type == "ARMATURE")
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    clothes_objs = []
    for o in objs:
        if clothes_name and o.type == "MESH" and clothes_name.lower() in o.name.lower() \
                and not (o.data.shape_keys and len(o.data.shape_keys.key_blocks) > 1):
            inflate(o, clothes_inflate)
            clothes_objs.append(o)
    for o in objs:
        if hair_name and o.type == "MESH" and hair_name.lower() in o.name.lower():
            clean_hair_weights(o)
            for co in clothes_objs:
                clear_mesh_over_surface(o, co, clearance=0.007)
            if fade_hairline or "short" in hair_name.lower():
                skin = next((b for b in objs if b.type == "MESH" and b.name.lower().startswith(("human.body", "human_export", "base"))
                             and len(b.data.vertices) > 10000), None)
                print("hairline fade vs skin:", skin.name if skin else None)
                fade_mesh_border(o, skin=skin)
    fix_materials(objs, cutout_names, tint=tint, hair_name=hair_name, hair_tint=hair_tint,
                  clothes_name=clothes_name, clothes_tint=clothes_tint)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    bpy.ops.export_scene.gltf(filepath=os.path.abspath(out), export_format="GLB",
                              use_selection=True, export_animations=False,
                              export_morph=True, export_morph_normal=morph_normals,
                              export_vertex_color="ACTIVE",
                              export_image_format="JPEG", export_jpeg_quality=92)


def main():
    a = parse_args()
    install_rig(a.rig_dir)
    basemesh, added = build_human(a)
    collection = export_copy(basemesh)
    if a.save_blend:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(a.save_blend))
    parse_hex = lambda s: tuple(int(s[i:i + 2], 16) / 255 for i in (0, 2, 4)) if s else None
    tint = parse_hex(a.brow_tint)
    hair_tint = parse_hex(a.hair_tint)
    clothes_tint = parse_hex(a.clothes_tint)
    hair_key = added.get("Hair", a.hair)
    brow_key = added.get("Eyebrows", a.eyebrows)
    lash_key = added.get("Eyelashes", a.eyelashes)
    clothes_key = added.get("Clothes", a.clothes)
    export_glb(collection, a.out, [hair_key, brow_key, lash_key], tint,
               hair_name=hair_key, clothes_name=clothes_key, morph_normals=a.morph_normals,
               hair_tint=hair_tint, clothes_tint=clothes_tint, fade_hairline=a.fade_hairline)
    print("WROTE", a.out)


main()
