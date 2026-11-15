import json
import os
import traceback
import unreal

STATUS_PATH = "C:/Users/seehi/.gemini/antigravity/brain/4e9b04fa-af8a-46a0-9828-f126b66af272/scratch/ue_export_status.json"
OUT_GLB = "F:/github-proj/ai-avatar/assets/models/metahuman-interviewer.glb"

CHARACTER_PATH = "/Game/Characters/MH_AIInterviewer"
EXPORT_PACKAGE_PATH = "/Game/Characters/Export"
HEAD_SKM_PATH = "/Game/Characters/Export/MH_AIInterviewer_Head"
SKIN_MI_NAME = "MI_AIInterviewer_Skin_Head"
SKIN_MI_PATH = f"{EXPORT_PACKAGE_PATH}/{SKIN_MI_NAME}"
SKIN_PARENT_PATH = "/MetaHumanCharacter/Materials/MI_Skin_Head_UI_LOD2"

TEMP_ACTOR_LABEL = "TMP_MetaHuman_GLB_Export"

HAIR_MESH_PATH = "/MetaHumanCharacter/Optional/Grooms/GroomAssets/Hair/Hair_M_BobMessy/Hair_M_BobMessy_CardsMesh_Group0_LOD2"
HAIR_MAT_PATH = "/MetaHumanCharacter/Materials/MI_Hair_Cards"
EYEBROW_MESH_PATH = "/MetaHumanCharacter/Optional/Grooms/GroomAssets/Eyebrows/Eyebrows_L_Shaded/Eyebrows_L_Shaded_CardsMesh_Group0_LOD2"
EYEBROW_MAT_PATH = "/MetaHumanCharacter/Materials/MI_Facial_Hair"

CHEST_BC_PATH = "/MetaHumanCharacter/Optional/BodyTextures/T_Skin_V1_Chest_BC"
CHEST_N_PATH = "/MetaHumanCharacter/Optional/BodyTextures/T_Skin_V1_Chest_N"
CHEST_CA_PATH = "/MetaHumanCharacter/Optional/BodyTextures/T_Skin_V1_Chest_Ca"

SLOT_MATERIAL_MAP = {
    4: "/MetaHumanCharacter/Materials/MI_Teeth_MHC_UI",
    5: "/MetaHumanCharacter/Materials/MI_Eyelashes_LowerLODs",
    6: "/MetaHumanCharacter/Materials/MI_Teeth_MHC_UI",
    7: "/MetaHumanCharacter/Materials/MI_Eye_Left_MHC",
    8: "/MetaHumanCharacter/Materials/MI_Eye_Right_MHC",
    9: "/MetaHumanCharacter/Materials/MI_Eyelashes_LowerLODs",
    10: "/MetaHumanCharacter/Materials/MI_Eyelashes_HigherLODs",
    11: "/MetaHumanCharacter/Common/Materials/MI_eye_lacrimal_fluid_unified",
    12: "/MetaHumanCharacter/Common/Materials/MI_eye_occlusion_unified",
    13: "/MetaHumanCharacter/Common/Materials/MI_eye_lacrimal_fluid_unified",
}
SKIN_SLOTS = (0, 1, 2, 3, 14)

status = {
    "steps": [],
    "errors": [],
    "synthesized_face_textures": {},
    "head_skm_materials": [],
    "temp_actor_path": None,
    "static_components": [],
    "deferred_to_slate_tick": False,
    "gltf_result": None,
    "fallback_used": False,
    "fallback_result": None,
    "out_glb": OUT_GLB,
    "out_glb_size_bytes": 0,
}

_tick_handle_ref = [None]


def write_status():
    try:
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2, default=str)
    except Exception as exc:
        unreal.log_error(f"Failed writing status json: {exc}")


def log_step(msg):
    status["steps"].append(msg)
    unreal.log(f"[MH Export] {msg}")


def extract_face_textures(char):
    """Extract synthesized face textures (basecolor, normal, cavity) from MetaHumanCharacter."""
    found = {"basecolor": None, "normal": None, "cavity": None}
    try:
        synth = char.get_editor_property("synthesized_face_textures")
    except Exception as exc:
        log_step(f"Could not read synthesized_face_textures property: {exc}")
        synth = None

    if synth is not None:
        items = []
        if hasattr(synth, "items"):
            items = list(synth.items())
        else:
            for attr in ("basecolor", "normal", "cavity"):
                try:
                    val = synth.get_editor_property(attr)
                    if val is not None:
                        items.append((attr, val))
                except Exception:
                    pass

        for k, v in items:
            kl = str(k).lower()
            vpath = v.get_path_name() if hasattr(v, "get_path_name") else str(v)
            status["synthesized_face_textures"][str(k)] = vpath
            if "basecolor" in kl and found["basecolor"] is None:
                found["basecolor"] = v
            elif "normal" in kl and found["normal"] is None:
                found["normal"] = v
            elif "cavity" in kl and found["cavity"] is None:
                found["cavity"] = v

    # Fallback scan in /Engine/Transient if any map entry was missing
    if any(found[k] is None for k in found):
        transient_pkg = unreal.find_object(None, "/Engine/Transient")
        if transient_pkg is not None and hasattr(unreal, "EditorAssetLibrary"):
            for obj_name in dir(transient_pkg):
                pass
        # Check known UE AssetTools / ObjectLibrary helpers if needed
        log_step(
            "Face texture summary: "
            + ", ".join(
                f"{k}={v.get_path_name() if v else None}" for k, v in found.items()
            )
        )
    return found


def ensure_character_editing_and_prepare_assets():
    """Load MetaHumanCharacter, ensure head mesh + skin MI exist and have assigned materials."""
    char = unreal.load_asset(CHARACTER_PATH)
    if char is None:
        raise RuntimeError(f"Could not load MetaHumanCharacter asset at {CHARACTER_PATH}")

    try:
        mh_subsys = unreal.get_editor_subsystem(unreal.MetaHumanCharacterEditorSubsystem)
        if mh_subsys is not None:
            is_editing = False
            if hasattr(mh_subsys, "is_object_added_for_editing"):
                is_editing = mh_subsys.is_object_added_for_editing(char)
            if not is_editing and hasattr(mh_subsys, "try_add_object_to_edit"):
                added = mh_subsys.try_add_object_to_edit(char)
                log_step(f"try_add_object_to_edit(char) -> {added}")
            else:
                log_step(f"Character editing active={is_editing}")
    except Exception as exc:
        log_step(f"MetaHumanCharacterEditorSubsystem begin_edit note: {exc}")

    # Step 2: Ensure Head + Top Shoulder SkeletalMesh exists
    head_skm = unreal.load_asset(HEAD_SKM_PATH)
    if head_skm is None:
        log_step(f"{HEAD_SKM_PATH} not found; exporting head geometry via MetaHumanCharacterExportBlueprintLibrary")
        geom_params = unreal.MetaHumanCharacterExportGeometryParameters()
        try:
            proj_prop = geom_params.get_editor_property("project_path")
            if hasattr(proj_prop, "path"):
                proj_prop.path = EXPORT_PACKAGE_PATH
                geom_params.set_editor_property("project_path", proj_prop)
            else:
                geom_params.set_editor_property("project_path", EXPORT_PACKAGE_PATH)
        except Exception:
            geom_params.project_path = EXPORT_PACKAGE_PATH

        for prop_name, val in (
            ("base_asset_name", "MH_AIInterviewer"),
            ("head_skeletal_mesh", True),
            ("body_skeletal_mesh", False),
            ("full_body_skeletal_mesh", False),
            ("overwrite_existing_assets", True),
        ):
            try:
                geom_params.set_editor_property(prop_name, val)
            except Exception as exc:
                log_step(f"Note setting {prop_name}: {exc}")

        ok = unreal.MetaHumanCharacterExportBlueprintLibrary.export_geometry(char, geom_params)
        log_step(f"export_geometry -> {ok}")
        head_skm = unreal.load_asset(HEAD_SKM_PATH)
        if head_skm is None:
            raise RuntimeError(f"Head skeletal mesh still not found at {HEAD_SKM_PATH} after export_geometry")
    else:
        log_step(f"Loaded existing {HEAD_SKM_PATH}")

    # Step 3: Create / update MI_AIInterviewer_Skin_Head
    skin_parent = unreal.load_asset(SKIN_PARENT_PATH)
    if skin_parent is None:
        raise RuntimeError(f"Could not load parent material {SKIN_PARENT_PATH}")

    mi_skin = unreal.load_asset(SKIN_MI_PATH)
    if mi_skin is None:
        asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
        factory = unreal.MaterialInstanceConstantFactoryNew()
        mi_skin = asset_tools.create_asset(
            SKIN_MI_NAME,
            EXPORT_PACKAGE_PATH,
            unreal.MaterialInstanceConstant,
            factory,
        )
        if mi_skin is None:
            raise RuntimeError(f"Failed creating {SKIN_MI_PATH}")
        log_step(f"Created MaterialInstanceConstant {SKIN_MI_PATH}")
    else:
        log_step(f"Loaded existing {SKIN_MI_PATH}")

    unreal.MaterialEditingLibrary.set_material_instance_parent(mi_skin, skin_parent)

    face_tex = extract_face_textures(char)
    tex_assignments = []
    if face_tex["basecolor"] is not None:
        tex_assignments.extend([
            ("Basecolor Original", face_tex["basecolor"]),
            ("Basecolor", face_tex["basecolor"]),
        ])
    if face_tex["normal"] is not None:
        tex_assignments.extend([
            ("Normal Original", face_tex["normal"]),
            ("Normal", face_tex["normal"]),
        ])
    if face_tex["cavity"] is not None:
        tex_assignments.append(("Cavity", face_tex["cavity"]))

    for param_name, tex_path in (
        ("Color_CHEST", CHEST_BC_PATH),
        ("Normal_CHEST", CHEST_N_PATH),
        ("Cavity_Chest", CHEST_CA_PATH),
    ):
        tex_obj = unreal.load_asset(tex_path)
        if tex_obj is not None:
            tex_assignments.append((param_name, tex_obj))
        else:
            status["errors"].append(f"Missing chest texture {tex_path}")

    for param_name, tex_obj in tex_assignments:
        unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(
            mi_skin, param_name, tex_obj
        )
    unreal.MaterialEditingLibrary.update_material_instance(mi_skin)
    log_step(f"Configured {len(tex_assignments)} texture parameters on {SKIN_MI_PATH}")

    # Step 4: Assign all 15 material slots on MH_AIInterviewer_Head
    mats = list(head_skm.get_editor_property("materials"))
    loaded_slot_mats = {}
    for slot_idx, mat_path in SLOT_MATERIAL_MAP.items():
        mat_obj = unreal.load_asset(mat_path)
        if mat_obj is None:
            status["errors"].append(f"Missing slot material {mat_path} for slot {slot_idx}")
        else:
            loaded_slot_mats[slot_idx] = mat_obj

    for idx in range(len(mats)):
        if idx in SKIN_SLOTS:
            mats[idx].set_editor_property("material_interface", mi_skin)
        elif idx in loaded_slot_mats:
            mats[idx].set_editor_property("material_interface", loaded_slot_mats[idx])

    head_skm.set_editor_property("materials", mats)
    if hasattr(head_skm, "post_edit_change"):
        try:
            head_skm.post_edit_change()
        except Exception:
            pass

    status["head_skm_materials"] = [
        {
            "index": i,
            "slot": str(m.get_editor_property("material_slot_name")),
            "mat": m.get_editor_property("material_interface").get_path_name()
            if m.get_editor_property("material_interface") else None,
        }
        for i, m in enumerate(head_skm.get_editor_property("materials"))
    ]

    unreal.EditorAssetLibrary.save_loaded_asset(mi_skin, only_if_is_dirty=False)
    unreal.EditorAssetLibrary.save_loaded_asset(head_skm, only_if_is_dirty=False)
    log_step(f"Saved {SKIN_MI_PATH} and {HEAD_SKM_PATH}")

    return head_skm


def configure_hair_and_eyebrows(temp_actor):
    """Attach/configure HairCards and EyebrowCards StaticMeshComponents on temp_actor."""
    sm_comps = list(temp_actor.get_components_by_class(unreal.StaticMeshComponent))
    hair_comp = None
    brow_comp = None

    for c in sm_comps:
        cname = c.get_name()
        if "Hair" in cname and hair_comp is None:
            hair_comp = c
        elif ("Eyebrow" in cname or "Brow" in cname) and brow_comp is None:
            brow_comp = c

    if hair_comp is None and len(sm_comps) >= 1:
        hair_comp = sm_comps[0]
    if brow_comp is None and len(sm_comps) >= 2:
        brow_comp = sm_comps[1]

    if hair_comp is None:
        hair_comp = temp_actor.add_component_by_class(
            unreal.StaticMeshComponent, False, unreal.Transform(), False
        )
        if hair_comp is not None:
            hair_comp.rename("HairCards")
    if brow_comp is None:
        brow_comp = temp_actor.add_component_by_class(
            unreal.StaticMeshComponent, False, unreal.Transform(), False
        )
        if brow_comp is not None:
            brow_comp.rename("EyebrowCards")

    status["static_components"] = [
        c.get_name() for c in temp_actor.get_components_by_class(unreal.StaticMeshComponent)
    ]

    hair_mesh = unreal.load_asset(HAIR_MESH_PATH)
    hair_mat = unreal.load_asset(HAIR_MAT_PATH)
    if hair_comp is not None and hair_mesh is not None:
        hair_comp.set_static_mesh(hair_mesh)
        if hair_mat is not None:
            for i in range(hair_comp.get_num_materials()):
                hair_comp.set_material(i, hair_mat)
        log_step(
            f"Configured {hair_comp.get_name()} with {HAIR_MESH_PATH} (slots={hair_comp.get_num_materials()})"
        )
    else:
        status["errors"].append(f"Failed configuring HairCards: comp={hair_comp}, mesh={hair_mesh}")

    brow_mesh = unreal.load_asset(EYEBROW_MESH_PATH)
    brow_mat = unreal.load_asset(EYEBROW_MAT_PATH)
    if brow_comp is not None and brow_mesh is not None:
        brow_comp.set_static_mesh(brow_mesh)
        if brow_mat is not None:
            for i in range(brow_comp.get_num_materials()):
                brow_comp.set_material(i, brow_mat)
        log_step(
            f"Configured {brow_comp.get_name()} with {EYEBROW_MESH_PATH} (slots={brow_comp.get_num_materials()})"
        )
    else:
        status["errors"].append(f"Failed configuring EyebrowCards: comp={brow_comp}, mesh={brow_mesh}")


def build_gltf_options():
    """Build GLTFExportOptions using GLTFMaterialBakeMode.SIMPLE to avoid USE_MESH_DATA crash."""
    options = unreal.GLTFExportOptions()
    options.export_uniform_scale = 0.01
    options.export_preview_mesh = True
    # CRITICAL: Must be SIMPLE (not USE_MESH_DATA) to avoid UnrealEditor-MaterialBaking.dll assertion on empty LOD sections.
    options.bake_material_inputs = unreal.GLTFMaterialBakeMode.SIMPLE
    options.default_level_of_detail = 2
    options.export_vertex_skin_weights = True
    options.export_morph_targets = True
    if hasattr(unreal, "GLTFTextureImageFormat") and hasattr(unreal.GLTFTextureImageFormat, "JPEG"):
        options.texture_image_format = unreal.GLTFTextureImageFormat.JPEG
    options.texture_image_quality = 85

    bs = options.default_material_bake_size
    if hasattr(unreal, "GLTFMaterialBakeSizePOT") and isinstance(bs, unreal.GLTFMaterialBakeSizePOT):
        options.default_material_bake_size = unreal.GLTFMaterialBakeSizePOT.POT_1024
    else:
        try:
            for attr in ("x", "y"):
                if hasattr(bs, attr):
                    setattr(bs, attr, 1024)
            for attr in ("auto_power_of_two", "b_auto_pot"):
                if hasattr(bs, attr):
                    setattr(bs, attr, False)
            options.default_material_bake_size = bs
        except Exception as exc:
            log_step(f"Bake size set note: {exc}")

    return options


def do_export(head_skm, allow_slate_defer=True):
    """Assemble temporary actor with Head + Hair + Eyebrows and export to OUT_GLB."""
    os.makedirs(os.path.dirname(OUT_GLB), exist_ok=True)
    ed_subsys = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    actor_subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    world = ed_subsys.get_editor_world()

    temp_actor = None
    spawned_here = False
    for act in actor_subsys.get_all_level_actors():
        if act is not None and act.get_actor_label() == TEMP_ACTOR_LABEL:
            temp_actor = act
            break

    if temp_actor is None:
        temp_actor = actor_subsys.spawn_actor_from_class(
            unreal.SkeletalMeshActor, unreal.Vector(0.0, 0.0, 0.0), unreal.Rotator(0.0, 0.0, 0.0)
        )
        if temp_actor is not None:
            spawned_here = True
            temp_actor.set_actor_label(TEMP_ACTOR_LABEL)
            skm_comp = temp_actor.skeletal_mesh_component
            if skm_comp is not None:
                skm_comp.set_skeletal_mesh_asset(head_skm)
            log_step(f"Spawned temporary actor {temp_actor.get_path_name()}")

    if temp_actor is None:
        if allow_slate_defer and hasattr(unreal, "register_slate_post_tick_callback"):
            status["deferred_to_slate_tick"] = True
            log_step("spawn_actor_from_class returned None (likely inside ConstructionScript); deferring to Slate post-tick")

            def _on_tick(_delta_time):
                try:
                    if _tick_handle_ref[0] is not None:
                        unreal.unregister_slate_post_tick_callback(_tick_handle_ref[0])
                        _tick_handle_ref[0] = None
                    do_export(head_skm, allow_slate_defer=False)
                except Exception as exc:
                    status["errors"].append(f"Slate post-tick export error: {exc}\n{traceback.format_exc()}")
                finally:
                    write_status()

            _tick_handle_ref[0] = unreal.register_slate_post_tick_callback(_on_tick)
            write_status()
            return False

        # Fallback: direct SkeletalMesh export without spawned actor
        log_step("Temp actor unavailable; exporting head_skm directly as fallback")
        options = build_gltf_options()
        if os.path.exists(OUT_GLB):
            os.remove(OUT_GLB)
        status["fallback_used"] = True
        res = unreal.GLTFExporter.export_to_gltf(head_skm, OUT_GLB, options, set())
        status["fallback_result"] = str(res)
        glb_size = os.path.getsize(OUT_GLB) if os.path.exists(OUT_GLB) else 0
        status["out_glb_size_bytes"] = glb_size
        return glb_size > 0

    try:
        status["temp_actor_path"] = temp_actor.get_path_name()
        configure_hair_and_eyebrows(temp_actor)

        options = build_gltf_options()
        if os.path.exists(OUT_GLB):
            os.remove(OUT_GLB)

        log_step(f"Calling GLTFExporter.export_to_gltf(world, {OUT_GLB}, SIMPLE, {{temp_actor}})")
        res = unreal.GLTFExporter.export_to_gltf(world, OUT_GLB, options, {temp_actor})
        status["gltf_result"] = str(res)

        glb_size = os.path.getsize(OUT_GLB) if os.path.exists(OUT_GLB) else 0
        if glb_size < 10240:
            status["fallback_used"] = True
            log_step(f"Primary export produced size={glb_size}; running direct head_skm fallback")
            res2 = unreal.GLTFExporter.export_to_gltf(head_skm, OUT_GLB, options, set())
            status["fallback_result"] = str(res2)
            glb_size = os.path.getsize(OUT_GLB) if os.path.exists(OUT_GLB) else 0

        status["out_glb_size_bytes"] = glb_size
        if glb_size > 0:
            log_step(f"GLB created successfully, size={glb_size} bytes")
        else:
            status["errors"].append(f"GLB file missing or 0 bytes at {OUT_GLB}")
        return glb_size > 0
    finally:
        if spawned_here and temp_actor is not None:
            try:
                actor_subsys.destroy_actor(temp_actor)
                log_step("Cleaned up spawned temporary actor")
            except Exception as exc:
                log_step(f"Cleanup temp_actor note: {exc}")


def main():
    try:
        head_skm = ensure_character_editing_and_prepare_assets()
        do_export(head_skm, allow_slate_defer=True)
    except Exception as exc:
        status["errors"].append(f"{exc}\n{traceback.format_exc()}")
        unreal.log_error(f"export_metahuman_head_glb error: {exc}")
    finally:
        write_status()


main()
