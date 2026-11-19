"""Checks for LTX Matte that need neither Nuke, ComfyUI nor a GPU:  python -I tests/smoke.py

The ComfyUI node definitions come from tests/fixtures/object_info.json, taken from a real ComfyUI
with ComfyUI-LTXVideo, ComfyUI-OCIO, VideoHelperSuite, KJNodes and ComfyUI-SAM3 installed.
"""
import json
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("nuke", types.ModuleType("nuke"))

from ltx_matte import graph, validate  # noqa: E402

CATALOG = json.loads((ROOT / "tests" / "fixtures" / "object_info.json").read_text(encoding="utf-8"))


def raises(call, text):
    try:
        call()
    except ValueError as error:
        assert text in str(error), (text, str(error))
    else:
        raise AssertionError(f"Expected an error mentioning: {text}")


def rules_test():
    assert graph.frame_count(1, 49) == (1, 49)
    assert graph.frame_count(1, 52) == (1, 49), "trimmed to 8n+1"
    assert graph.frame_count(2, 300) == (2, 146), "at most 145 frames"
    raises(lambda: graph.frame_count(5, 12), "at least 9")
    raises(lambda: graph.frame_count(9, 1), "at or after")
    for plate, shorter, expected in (((1920, 1080), 704, (1280, 704)), ((1920, 1080), 1088, (1920, 1088)),
                                     ((1920, 1080), 2000, (1920, 1088)), ((1080, 1920), 704, (704, 1280)), ((1080, 1920), 2000, (1088, 1920)),
                                     ((3840, 1600), 704, (1696, 704)), ((1000, 1000), 100, (128, 128))):
        size = graph.render_size(*plate, shorter)
        assert size == expected, (plate, shorter, size)
        assert size[0] % 32 == 0 and size[1] % 32 == 0 and max(size) <= 1920 and min(size) <= 1088
    scale, offset_x, offset_y = graph.fit(1920, 1080, 1280, 704)
    assert abs(scale - 704 / 1080) < 1e-9 and offset_y == 0 and abs(offset_x - (1280 - 1920 * scale) / 2) < 1e-9
    assert graph.to_render_pixels(960, 540, 1920, 1080, 1280, 704) == (640.0, 352.0), "centre stays centre"
    x, y = graph.to_render_pixels(0, 1080, 1920, 1080, 1280, 704)
    assert y == 0.0 and abs(x - offset_x) < 0.1, "Nuke's top left is the render's top left, after the side padding"


def prompt_test():
    models = graph.default_models()
    assert models["alpha_lora"].endswith("alpha-gen-0.9.safetensors"), models
    prompt = graph.build_prompt("plate_up.mp4", 25, 23.976, (1280, 704), 7, "pos", "neg", models, "job1")
    assert not graph.VIEWER_ONLY & prompt.keys(), "previews, timers and notifications are left out"
    assert "9089" not in prompt, "an unused LoadImage is pruned, so a missing file cannot fail the job"
    load = prompt[graph.LOAD_VIDEO]["inputs"]
    assert (load["video"], load["frame_load_cap"], load["skip_first_frames"], load["force_rate"]) == (
        "plate_up.mp4", 25, 0, 23.976)
    assert prompt[graph.FRAME_LIMIT]["inputs"]["value"] == 25
    for node in graph.RESIZE_NODES:
        assert prompt[node]["inputs"]["resize_type.shorter_size"] == 704, "the plate keeps its render size"
    assert prompt[graph.SEED]["inputs"]["noise_seed"] == 7
    assert (prompt[graph.POSITIVE]["inputs"]["value"], prompt[graph.NEGATIVE]["inputs"]["value"]) == ("pos", "neg")
    for node, name in graph.VIDEO_OUTPUTS.items():
        assert prompt[node]["inputs"]["filename_prefix"] == f"LTXMatte/job1/{name}"
    writer = prompt[graph.MATTE_WRITER]["inputs"]
    assert writer["images"] == graph.MATTE_IMAGES and writer["raw_data"] is True and writer["filename"] == "Matte"
    assert graph.SELECT_WRITER not in prompt and graph.SAM_SEGMENT not in prompt
    found = [problem for problem in validate.problems(prompt, CATALOG) if "'plate_up.mp4'" not in problem]
    assert found == [], found[:5]  # the plate is uploaded per render, so it is not in the server's list yet
    other = dict(models, alpha_lora="missing.safetensors")
    found = validate.problems(graph.build_prompt("plate.mp4", 9, 24, (512, 512), 1, "", "", other, "j"), CATALOG)
    assert any("missing.safetensors" in problem for problem in found), "a missing model is reported"
    raises(lambda: graph.build_prompt("p", 24, 24, (512, 512), 1, "", "", models, "j"), "frame count")
    raises(lambda: graph.build_prompt("p", 9, 24, (500, 512), 1, "", "", models, "j"), "multiple of 32")
    raises(lambda: graph.build_prompt("p", 9, 24, (1952, 512), 1, "", "", models, "j"), "1920x1088")


def selection_test():
    models = graph.default_models()
    cases = {
        "Text": {"mode": "Text", "text": " doll ,, chainsaw ", "frame": 0},
        "Click": {"mode": "Click", "positive": [(640.0, 352.0)], "negative": [(100.0, 80.0)], "frame": 99},
        "Box": {"mode": "Box", "box": (500, 100, 800, 650), "frame": 3},
    }
    for mode, chosen in cases.items():
        select = graph.build_select_prompt("plate.mp4", 25, 24, (1280, 704), chosen, "LTXMatte/j/EXR")
        assert validate.problems(select, CATALOG) == [], (mode, validate.problems(select, CATALOG)[:5])
        assert select[graph.SELECT_WRITER]["inputs"]["filename"] == "Select"
        assert {step["class_type"] for step in select.values()} >= {"VHS_LoadVideo", "SAM3VideoSegmentation",
                                                                      "SAM3Propagate", "OCIOWrite"}
        assert "UNETLoader" not in {step["class_type"] for step in select.values()}, "finding a subject skips LTX"
        segment = select[graph.SAM_SEGMENT]["inputs"]
        assert segment["prompt_mode"] == graph.SAM_MODES[mode] and segment["video_frames"] == graph.PLATE_IMAGES
        assert select[graph.SAM_PROPAGATE]["inputs"]["direction"] == "both"
        with_matte = graph.build_prompt("plate.mp4", 25, 24, (1280, 704), 1, "", "", models, "j", chosen)
        assert validate.problems(with_matte, CATALOG) == []
    text = graph.build_select_prompt("plate.mp4", 25, 24, (1280, 704), cases["Text"], "x")
    prompts = sorted(step["inputs"]["text_prompt"] for step in text.values()
                     if step["class_type"] == "SAM3VideoSegmentation")
    assert prompts == ["chainsaw", "doll"], "each object is tracked on its own"
    joins = [step for step in text.values() if step["class_type"] == "MaskComposite"]
    assert len(joins) == 1 and joins[0]["inputs"]["operation"] == "or"
    assert text[graph.SAM_MASK_IMAGE]["inputs"]["mask"][0] != graph.SAM_OUTPUT, "the joined mask is written"
    click = graph.build_select_prompt("plate.mp4", 25, 24, (1280, 704), cases["Click"], "x")
    assert click[graph.SAM_SEGMENT]["inputs"]["frame_idx"] == 24, "the select frame is kept inside the range"
    points = click[graph.SAM_POINTS]["inputs"]
    assert json.loads(points["coordinates"]) == [{"x": 640.0, "y": 352.0}]
    assert json.loads(points["neg_coordinates"]) == [{"x": 100.0, "y": 80.0}]
    box = graph.build_select_prompt("plate.mp4", 25, 24, (1280, 704), cases["Box"], "x")
    assert json.loads(box[graph.SAM_BOXES]["inputs"]["bboxes"]) == [{"x1": 500, "y1": 100, "x2": 800, "y2": 650}]
    raises(lambda: graph.build_select_prompt("p", 9, 24, (512, 512), {"mode": "Text", "text": " , "}, "x"), "Type")
    raises(lambda: graph.build_select_prompt("p", 9, 24, (512, 512), {"mode": "Click", "positive": []}, "x"), "point")
    raises(lambda: graph.build_select_prompt("p", 9, 24, (512, 512), {"mode": "Box", "box": (5, 5, 6, 6)}, "x"), "box")
    raises(lambda: graph.build_select_prompt("p", 9, 24, (512, 512), None, "x"), "Text, Click or Box")


def validator_test():
    good = {"1": {"class_type": "MaskToImage", "inputs": {"mask": ["2", 0]}},
            "2": {"class_type": "SAM3VideoOutput", "inputs": {"masks": ["3", 0], "video_state": ["3", 2]}},
            "3": {"class_type": "SAM3Propagate", "inputs": {"sam3_model_config": ["4", 0], "video_state": ["5", 0]}},
            "4": {"class_type": "LoadSAM3Model", "inputs": {}},
            "5": {"class_type": "SAM3VideoSegmentation", "inputs": {"prompt_mode": "text"}}}
    assert validate.problems(good, CATALOG) == []
    bad = json.loads(json.dumps(good))
    bad["1"]["inputs"]["mask"] = ["4", 0]
    bad["5"]["inputs"]["prompt_mode"] = "lasso"
    bad["6"] = {"class_type": "NotInstalled", "inputs": {}}
    del bad["3"]["inputs"]["video_state"]
    found = "\n".join(validate.problems(bad, CATALOG))
    for text in ("expects MASK", "'lasso'", "NotInstalled", "'video_state' is missing"):
        assert text in found, (text, found)


def package_test():
    gizmo = (ROOT / "LTXMatte.gizmo").read_text(encoding="utf-8")
    assert gizmo.startswith("Gizmo {") and "selected true" not in gizmo
    for knob in ("select_mode", "subject_text", "find_subject", "queue_job", "isolate", "garb_expand", "view",
                 "size_short", "start_follows", "alpha_lora", "server", "halo_cut"):
        assert f" {knob} " in gizmo or f"{{{knob} " in gizmo or f" {knob}\n" in gizmo, knob
    for internal in ("SelectRead", "MatteRead", "ToRender", "PlateIn", "CleanMatte", "Output"):
        assert internal in gizmo, internal
    menu = (ROOT / "menu.py").read_text(encoding="utf-8")
    assert "bridge.create()" in menu and "comfyui_menu.png" in menu and "import ltx_matte" not in menu
    assert (ROOT / "ltx_matte" / "comfyui_menu.png").is_file()
    template = (ROOT / "ltx_matte" / "alpha_gen_api.json").read_text(encoding="utf-8")
    for private in ("Screenshot", "C:/", "D:/"):
        assert private not in template, private


def main():
    for name, step in (("frame and size rules", rules_test), ("matte job", prompt_test),
                       ("subject selection", selection_test), ("workflow checker", validator_test),
                       ("gizmo and menu", package_test)):
        step()
        print(f"ok     {name}")
    print("All LTX Matte checks passed.")


if __name__ == "__main__":
    main()
