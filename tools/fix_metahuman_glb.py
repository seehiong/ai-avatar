"""Post-fix a MetaHuman GLB from github.com/creategamecharacters/metahuman-to-glb for our viewer.

1. Face materials: UE's glTF exporter assigns the face mesh's materials by slot index, not by
   section, so on low-LOD MetaHumans the head gets the teeth material, the teeth an eye, etc.
   Reassign by geometry: biggest primitive = skin, the mouth-height one = teeth, the two small
   ones at eye height = eyes (+x = character's left).
2. Hair cards: the baked card colour has no alpha, so cards render as opaque strips. Add the
   coverage (R channel of *_CardsAtlas_Attribute.png) as alpha and switch to alpha-test (MASK).

3. Face textures: UE bakes them cropped to the wrong section's UVs; --dcc-maps swaps in the MetaHuman
   DCC export's full Head_*/Teeth_* maps (the left eye reuses the right eye's textures).
4. Unreferenced images are dropped and the binary chunk repacked.

Usage: python3 tools/fix_metahuman_glb.py in.glb out.glb --hair-attribute <..._CardsAtlas_Attribute.png>
           --dcc-maps <DCC export>/Maps
"""
import argparse
import io
import json
import struct

from PIL import Image


def read_glb(path):
    b = open(path, "rb").read()
    jl = struct.unpack("<I", b[12:16])[0]
    j = json.loads(b[20:20 + jl])
    bl = struct.unpack("<I", b[20 + jl:24 + jl])[0]
    return j, bytearray(b[28 + jl:28 + jl + bl])


def write_glb(path, j, binb):
    while len(binb) % 4: binb.append(0)
    js = json.dumps(j, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    j["buffers"][0]["byteLength"] = len(binb)
    js = json.dumps(j, separators=(",", ":")).encode(); js += b" " * (-len(js) % 4)
    out = struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(binb))
    out += struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(binb), 0x004E4942) + bytes(binb)
    open(path, "wb").write(out)


def image_bytes(j, binb, idx):
    bv = j["bufferViews"][j["images"][idx]["bufferView"]]
    o = bv.get("byteOffset", 0)
    return bytes(binb[o:o + bv["byteLength"]])


def add_image(j, binb, data, mime="image/png"):
    while len(binb) % 4: binb.append(0)
    j["bufferViews"].append({"buffer": 0, "byteOffset": len(binb), "byteLength": len(data)})
    binb.extend(data)
    j["images"].append({"bufferView": len(j["bufferViews"]) - 1, "mimeType": mime})
    return len(j["images"]) - 1


def _texture_slots(m):
    pb = m.get("pbrMetallicRoughness", {})
    slots = [pb.get("baseColorTexture"), pb.get("metallicRoughnessTexture"),
             m.get("normalTexture"), m.get("occlusionTexture"), m.get("emissiveTexture")]
    return [s for s in slots if s]


def fix_face(j):
    # The exporter hands section i the material of section i+1 (and section 0, the head, gets section 1's).
    # Seen on both the LOD5-7 build (4 sections) and the cinematic LOD0 (8 sections): rotate back by one.
    for mesh in j["meshes"]:
        prims = mesh["primitives"]
        if "FaceMesh" not in mesh["name"] or len(prims) < 2: continue
        names = lambda: [j["materials"][p["material"]]["name"] for p in prims]
        if "Skin" in names()[0]: continue                              # already fine
        if "Skin" not in names()[-1]:
            print("face: unexpected material order, left as is:", names()); continue
        mats = [p["material"] for p in prims]
        for p, m in zip(prims, mats[-1:] + mats[:-1]): p["material"] = m
        # UE fits a KHR_texture_transform to the section that wrongly got the material (e.g. skin scaled 3x35
        # for the tear-line strip): the real face UVs are 0..1, so drop them from the face materials
        for mi in set(mats):
            for slot in _texture_slots(j["materials"][mi]):
                slot.get("extensions", {}).pop("KHR_texture_transform", None)
                if slot.get("extensions") == {}: del slot["extensions"]
        counts = [j["accessors"][p["attributes"]["POSITION"]]["count"] for p in prims]
        print("face materials rotated:", list(zip(counts, names())))


def _encode(img, fmt):
    buf = io.BytesIO()
    if fmt == "JPEG": img.convert("RGB").save(buf, "JPEG", quality=92); return buf.getvalue(), "image/jpeg"
    img.save(buf, "PNG", optimize=True); return buf.getvalue(), "image/png"


def _set_texture(j, binb, slot, img, fmt="JPEG"):
    """Point a material texture slot at a new image (own texture entry, so shared textures stay intact)."""
    data, mime = _encode(img, fmt)
    src = add_image(j, binb, data, mime)
    old = j["textures"][slot["index"]]
    j["textures"].append({k: v for k, v in old.items() if k != "source"} | {"source": src})
    slot["index"] = len(j["textures"]) - 1
    slot.pop("extensions", None)


def _dx_to_gl(img):
    r, g, b = img.convert("RGB").split()
    return Image.merge("RGB", (r, g.point(lambda v: 255 - v), b))


def fix_face_textures(j, binb, maps):
    """UE's exporter bakes each face texture cropped to the UVs of the section that (wrongly) had the
    material, so skin/teeth images are garbage. Use the DCC export's full maps instead."""
    import os
    p = lambda n: os.path.join(maps, n)
    mats = {m["name"]: m for m in j["materials"]}
    get = lambda key: next((m for n, m in mats.items() if key in n), None)
    skin, teeth, eye_l, eye_r = get("Face_Skin"), get("Teeth"), get("EyeL"), get("EyeR")
    if skin and os.path.exists(p("Head_Basecolor.png")):
        pb = skin["pbrMetallicRoughness"]
        _set_texture(j, binb, pb["baseColorTexture"], Image.open(p("Head_Basecolor.png")))
        if skin.get("normalTexture"): _set_texture(j, binb, skin["normalTexture"], _dx_to_gl(Image.open(p("Head_Normal.png"))), "PNG")
        if os.path.exists(p("Head_SRMF.png")):                         # SRMF: G = roughness -> glTF MR (G rough, B metal)
            g = Image.open(p("Head_SRMF.png")).convert("RGB").split()[1]
            mr = Image.merge("RGB", (Image.new("L", g.size, 255), g, Image.new("L", g.size, 0)))
            if "metallicRoughnessTexture" not in pb: pb["metallicRoughnessTexture"] = dict(pb["baseColorTexture"])
            _set_texture(j, binb, pb["metallicRoughnessTexture"], mr)
            pb["roughnessFactor"] = 1.0; pb["metallicFactor"] = 0.0
        print("skin: DCC head maps")
    if teeth and os.path.exists(p("Teeth_Color.png")):
        _set_texture(j, binb, teeth["pbrMetallicRoughness"]["baseColorTexture"], Image.open(p("Teeth_Color.png")))
        if teeth.get("normalTexture"): _set_texture(j, binb, teeth["normalTexture"], _dx_to_gl(Image.open(p("Teeth_Normal.png"))), "PNG")
        print("teeth: DCC maps")
    if eye_l and eye_r:                                                # both eyes share one UV layout
        for k in ("pbrMetallicRoughness",):
            eye_l[k] = json.loads(json.dumps(eye_r[k]))
        if eye_r.get("normalTexture"): eye_l["normalTexture"] = dict(eye_r["normalTexture"])
        print("eyeL: uses eyeR textures")


def compact(j, binb):
    """Drop textures/images/bufferViews nothing references any more and repack the binary chunk."""
    used_tex = set()
    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("index"), int): used_tex.add(o["index"])   # any textureInfo in a material
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(j["materials"])
    tex_map = {o: n for n, o in enumerate(sorted(used_tex))}
    def remap(o):
        if isinstance(o, dict):
            if isinstance(o.get("index"), int):
                o["index"] = tex_map[o["index"]]
            for v in o.values(): remap(v)
        elif isinstance(o, list):
            for v in o: remap(v)
    remap(j["materials"])
    j["textures"] = [j["textures"][o] for o in sorted(used_tex)]
    used_img = sorted({t["source"] for t in j["textures"]})
    img_map = {o: n for n, o in enumerate(used_img)}
    for t in j["textures"]: t["source"] = img_map[t["source"]]
    j["images"] = [j["images"][o] for o in used_img]
    # bufferViews: keep those used by accessors, images and Draco
    used_bv = {a["bufferView"] for a in j["accessors"] if "bufferView" in a} | {im["bufferView"] for im in j["images"]}
    sparse = [a["sparse"][k] for a in j["accessors"] if "sparse" in a for k in ("indices", "values")]   # morph targets
    used_bv |= {s["bufferView"] for s in sparse}
    for m in j["meshes"]:
        for p in m["primitives"]:
            d = p.get("extensions", {}).get("KHR_draco_mesh_compression")
            if d: used_bv.add(d["bufferView"])
    order = sorted(used_bv); bv_map = {o: n for n, o in enumerate(order)}
    new = bytearray(); views = []
    for o in order:
        bv = dict(j["bufferViews"][o]); s = bv.get("byteOffset", 0)
        while len(new) % 4: new.append(0)
        chunk = binb[s:s + bv["byteLength"]]; bv["byteOffset"] = len(new); new.extend(chunk); views.append(bv)
    j["bufferViews"] = views
    for a in j["accessors"]:
        if "bufferView" in a: a["bufferView"] = bv_map[a["bufferView"]]
    for s in sparse: s["bufferView"] = bv_map[s["bufferView"]]
    for im in j["images"]: im["bufferView"] = bv_map[im["bufferView"]]
    for m in j["meshes"]:
        for p in m["primitives"]:
            d = p.get("extensions", {}).get("KHR_draco_mesh_compression")
            if d: d["bufferView"] = bv_map[d["bufferView"]]
    return new


def fix_hair(j, binb, attribute_png):
    for m in j["materials"]:
        if "Hair" not in m["name"] or "CardMat" not in m["name"]: continue
        tex = j["textures"][m["pbrMetallicRoughness"]["baseColorTexture"]["index"]]
        bc = Image.open(io.BytesIO(image_bytes(j, binb, tex["source"]))).convert("RGB")
        cov = Image.open(attribute_png).getchannel("R").resize(bc.size, Image.LANCZOS)
        # cards are thin strands: lift coverage a little so downsampling doesn't make the hair sparse
        rgba = bc.copy(); rgba.putalpha(cov.point(lambda v: min(255, int(v * 1.35))))
        buf = io.BytesIO(); rgba.save(buf, "PNG", optimize=True)
        tex["source"] = add_image(j, binb, buf.getvalue())
        m["alphaMode"] = "MASK"; m["alphaCutoff"] = 0.4
        print("hair alpha added:", m["name"], rgba.size)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("dst")
    ap.add_argument("--hair-attribute", help="<groom>_CardsAtlas_Attribute.png (R = coverage)")
    ap.add_argument("--dcc-maps", help="MetaHuman DCC export Maps/ folder (Head_Basecolor.png, ...)")
    args = ap.parse_args()
    j, binb = read_glb(args.src)
    fix_face(j)
    if args.dcc_maps: fix_face_textures(j, binb, args.dcc_maps)
    if args.hair_attribute: fix_hair(j, binb, args.hair_attribute)
    binb = compact(j, binb)
    write_glb(args.dst, j, binb)
    print("wrote", args.dst)
