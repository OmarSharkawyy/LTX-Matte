"""The ComfyUI job for one LTX Matte render, built from the Alpha Gen workflow in alpha_gen_api.json.

The workflow is used as exported from ComfyUI. Per render, only its inputs change (plate, frame
count, size, prompts, seed, models, output names), a few viewer-only nodes are left out, and two
things are added: an EXR writer for the matte, and SAM3 nodes when an object is selected.

Pure Python with no Nuke dependency, so it can be tested on its own.
"""
import copy
import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name("alpha_gen_api.json")

SIZE_STEP = 32          # LTX Alpha Gen needs each side to be a multiple of 32
MAX_WIDTH, MAX_HEIGHT = 1920, 1088
MIN_SIDE = 128
MAX_FRAMES = 145        # longer clips leak RGB into the matte, per Lightricks

# Where each setting lives in the workflow.
LOAD_VIDEO = "9021"
FRAME_LIMIT = "9075"
SHORTER_LIMIT = "9069"
RESIZE_NODES = ("9052", "5548:5026")
POSITIVE, NEGATIVE = "5508", "5509"
SEED = "5516:4832"
PLATE_IMAGES = ["9052", 0]       # the plate as the workflow sized it
MATTE_IMAGES = ["5518:5538", 0]  # the decoded matte
MODEL_INPUTS = {
    "transformer": ("5004:5602", "unet_name"),
    "text_encoder": ("5004:5605", "clip_name"),
    "prompt_enhancer": ("5004:5604", "clip_name"),
    "video_vae": ("5004:5601", "vae_name"),
    "audio_vae": ("5004:5600", "vae_name"),
    "alpha_lora": ("5004:5606", "lora_name"),
}
LORA_STRENGTH = ("5004:5606", "strength_model")
API_CHECKPOINTS = ("5014:5504", "5014:5505")  # the optional LTX API encoders name the transformer too
VIDEO_OUTPUTS = {"9036": "Original", "9048": "Alpha", "9044": "Premult", "9070": "OverGreen",
                 "9047": "ContactSheet"}
VIEWER_ONLY = {"5544", "9045", "9087", "9089", "9090", "9091"}  # previews, timers, labels, notifications

MATTE_WRITER, SELECT_WRITER = "9600", "9601"
SAM_LOADER, SAM_SEGMENT, SAM_PROPAGATE, SAM_OUTPUT = "9610", "9611", "9612", "9613"
SAM_POINTS, SAM_BOXES, SAM_MASK_IMAGE = "9614", "9615", "9616"
SELECT_MODES = ("Auto", "Text", "Click", "Box", "Roto")
SAM_MODES = {"Text": "text", "Click": "point", "Box": "box"}


def template():
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def default_models():
    prompt = template()
    return {key: prompt[node]["inputs"][field] for key, (node, field) in MODEL_INPUTS.items()}


def frame_count(first, last):
    """The inclusive range LTX can use: 8n+1 frames, at most 145. Returns (first, last)."""
    if last < first:
        raise ValueError("Last must be at or after First.")
    span = min(last - first, MAX_FRAMES - 1) // 8 * 8
    if span == 0:
        raise ValueError("Select at least 9 frames: LTX renders clips of 9, 17, 25, ... frames.")
    return first, first + span


def render_size(plate_width, plate_height, shorter):
    """The render size for a plate: its aspect, shorter side near `shorter`, both sides /32, within 1920x1088
    (or 1088x1920 for a portrait plate).

    The plate is fitted into this box and padded, never stretched, as Lightricks recommends.
    """
    if plate_width <= 0 or plate_height <= 0:
        raise ValueError("The plate has no size.")
    aspect = plate_width / plate_height
    shorter = max(MIN_SIDE, int(shorter))
    if aspect >= 1:
        height = shorter
        width = height * aspect
    else:
        width = shorter
        height = width / aspect
    long_limit, short_limit = (MAX_WIDTH, MAX_HEIGHT) if aspect >= 1 else (MAX_HEIGHT, MAX_WIDTH)
    scale = min(1.0, long_limit / width, short_limit / height)
    width, height = width * scale, height * scale
    snap = lambda value: max(MIN_SIDE, -(-int(round(value)) // SIZE_STEP) * SIZE_STEP)  # round up: pad, never crop
    return min(snap(width), long_limit), min(snap(height), short_limit)


def fit(plate_width, plate_height, width, height):
    """(scale, x offset, y offset) of a plate fitted and centred in a width x height render."""
    scale = min(width / plate_width, height / plate_height)
    return scale, (width - plate_width * scale) / 2, (height - plate_height * scale) / 2


def to_render_pixels(x, y, plate_width, plate_height, width, height):
    """A Nuke plate position (origin bottom left) as render pixels (origin top left)."""
    scale, offset_x, offset_y = fit(plate_width, plate_height, width, height)
    return round(x * scale + offset_x, 1), round((plate_height - y) * scale + offset_y, 1)


def selection_nodes(selection, frames):
    """SAM3 nodes that track the selected object through the plate, and the mask they give."""
    mode = SAM_MODES[selection["mode"]]
    frame = min(max(int(selection.get("frame", 0)), 0), frames - 1)
    nodes = {
        SAM_LOADER: {"class_type": "LoadSAM3Model", "inputs": {"precision": "auto", "compile": False}},
        SAM_SEGMENT: {"class_type": "SAM3VideoSegmentation", "inputs": {
            "prompt_mode": mode, "video_frames": PLATE_IMAGES, "frame_idx": frame,
            "score_threshold": float(selection.get("threshold", 0.3))}},
        SAM_PROPAGATE: {"class_type": "SAM3Propagate", "inputs": {
            "sam3_model_config": [SAM_LOADER, 0], "video_state": [SAM_SEGMENT, 0],
            "start_frame": frame, "end_frame": -1, "direction": "both"}},
        SAM_OUTPUT: {"class_type": "SAM3VideoOutput", "inputs": {
            "masks": [SAM_PROPAGATE, 0], "video_state": [SAM_PROPAGATE, 2], "scores": [SAM_PROPAGATE, 1],
            "obj_id": -1, "plot_all_masks": True}},
        SAM_MASK_IMAGE: {"class_type": "MaskToImage", "inputs": {"mask": [SAM_OUTPUT, 0]}},
    }
    segment = nodes[SAM_SEGMENT]["inputs"]
    if mode == "text":
        terms = [part.strip() for part in str(selection.get("text", "")).split(",") if part.strip()]
        if not terms:
            raise ValueError("Type what to select, for example: horse, or: car, person.")
        segment["text_prompt"] = terms[0]
        # SAM3 misses objects when several are asked for in one text prompt (tested: "doll, chainsaw"
        # found almost nothing), so each one is tracked on its own and the masks are joined.
        combined = [SAM_OUTPUT, 0]
        for index, term in enumerate(terms[1:], start=1):
            base = 9650 + index * 10
            seg, prop, out, join = (str(base + offset) for offset in range(4))
            nodes[seg] = {"class_type": "SAM3VideoSegmentation", "inputs": dict(segment, text_prompt=term)}
            nodes[prop] = {"class_type": "SAM3Propagate", "inputs": dict(
                nodes[SAM_PROPAGATE]["inputs"], video_state=[seg, 0])}
            nodes[out] = {"class_type": "SAM3VideoOutput", "inputs": dict(
                nodes[SAM_OUTPUT]["inputs"], masks=[prop, 0], video_state=[prop, 2], scores=[prop, 1])}
            nodes[join] = {"class_type": "MaskComposite", "inputs": {
                "destination": combined, "source": [out, 0], "x": 0, "y": 0, "operation": "or"}}
            combined = [join, 0]
        nodes[SAM_MASK_IMAGE]["inputs"]["mask"] = combined
    elif mode == "point":
        positive = [{"x": x, "y": y} for x, y in selection.get("positive", ())]
        if not positive:
            raise ValueError("Place at least one Select point on the object in the Viewer.")
        nodes[SAM_POINTS] = {"class_type": "SAM3PointCollector", "inputs": {
            "image": PLATE_IMAGES, "points_store": "{}",
            "coordinates": json.dumps(positive),
            "neg_coordinates": json.dumps([{"x": x, "y": y} for x, y in selection.get("negative", ())])}}
        segment["positive_points"], segment["negative_points"] = [SAM_POINTS, 0], [SAM_POINTS, 1]
    else:
        box = selection.get("box")
        if not box or box[2] - box[0] < 2 or box[3] - box[1] < 2:
            raise ValueError("Draw the Select box around the object in the Viewer.")
        x1, y1, x2, y2 = box
        nodes[SAM_BOXES] = {"class_type": "SAM3BBoxCollector", "inputs": {
            "image": PLATE_IMAGES, "neg_bboxes": "[]",
            "bboxes": json.dumps([{"x1": x1, "y1": y1, "x2": x2, "y2": y2}])}}
        segment["positive_boxes"], segment["negative_boxes"] = [SAM_BOXES, 0], [SAM_BOXES, 1]
    return nodes


def exr_writer(images, filename, folder, colorspaces):
    """OCIOWrite as a raw float EXR sequence: the matte's values exactly as LTX decoded them."""
    source, target = colorspaces
    return {"class_type": "OCIOWrite", "inputs": {
        "images": images, "profile": "none", "input_colorspace": source, "output_colorspace": target,
        "container": "sequence", "still_format": "exr", "bit_depth": "16f", "compression": "zip",
        "video_codec": "prores_4444", "auto_range": False, "first_frame": 1, "last_frame": 0,
        "start_number": 1, "source_start": 1, "raw_data": True, "colorspace_in_name": False,
        "auto_colorspace": False, "output_folder": folder, "filename": filename,
        "write_audio": False, "write_sidecar": False}}


def build_prompt(plate, frames, fps, size, seed, positive, negative, models, job_name,
                 selection=None, exr_folder=None, colorspaces=("sRGB - Display", "ACEScg"),
                 lora_strength=1.0):
    """The ComfyUI prompt for one render.

    plate: the uploaded plate video name. size: (width, height) the plate was exported at, both
    multiples of 32. exr_folder: where OCIOWrite writes the EXRs (a path on the ComfyUI machine, or a
    folder inside its output folder). selection: None for Auto or Roto, else a dict with mode
    Text/Click/Box and its text, points or box in render pixels, and the frame to select on.
    """
    width, height = size
    if width % SIZE_STEP or height % SIZE_STEP or min(width, height) < MIN_SIDE:
        raise ValueError("The render size must be at least 128 and a multiple of 32 on each side.")
    if max(width, height) > MAX_WIDTH or min(width, height) > MAX_HEIGHT:
        raise ValueError("LTX Alpha Gen renders at most 1920x1088 (or 1088x1920).")
    if (frames - 1) % 8 or not 9 <= frames <= MAX_FRAMES:
        raise ValueError("The frame count must be 9, 17, 25, ... up to 145.")
    if not 0 <= seed < 2**64:
        raise ValueError("Seed must be between 0 and 2^64 - 1.")
    prompt = template()
    for node in VIEWER_ONLY:
        prompt.pop(node, None)
    load = prompt[LOAD_VIDEO]["inputs"]
    # force_rate 0 loads every frame as it is. Any other value makes VideoHelperSuite resample by time,
    # which drops or repeats frames whenever it differs from the clip's own rate (23.976 against 24).
    load.update(video=plate, force_rate=0, frame_load_cap=frames, skip_first_frames=0,
                select_every_nth=1, custom_width=0, custom_height=0)
    prompt[FRAME_LIMIT]["inputs"]["value"] = frames
    shorter = min(width, height)
    prompt[SHORTER_LIMIT]["inputs"]["value"] = shorter
    for node in RESIZE_NODES:  # the plate already has its render size, so these keep it as it is
        prompt[node]["inputs"]["resize_type.shorter_size"] = shorter
    prompt[POSITIVE]["inputs"]["value"] = positive
    prompt[NEGATIVE]["inputs"]["value"] = negative
    prompt[SEED]["inputs"]["noise_seed"] = int(seed)
    for key, (node, field) in MODEL_INPUTS.items():
        prompt[node]["inputs"][field] = models[key]
    prompt[LORA_STRENGTH[0]]["inputs"][LORA_STRENGTH[1]] = float(lora_strength)
    for node in API_CHECKPOINTS:
        if node in prompt:
            prompt[node]["inputs"]["ckpt_name"] = models["transformer"]
    for node, name in VIDEO_OUTPUTS.items():
        prompt[node]["inputs"]["filename_prefix"] = f"LTXMatte/{job_name}/{name}"
    exr_folder = exr_folder or f"LTXMatte/{job_name}/EXR"
    prompt[MATTE_WRITER] = exr_writer(MATTE_IMAGES, "Matte", exr_folder, colorspaces)
    if selection and selection.get("mode") in SAM_MODES:
        prompt.update(selection_nodes(selection, frames))
        prompt[SELECT_WRITER] = exr_writer([SAM_MASK_IMAGE, 0], "Select", exr_folder, colorspaces)
    return prune(prompt)


def build_select_prompt(plate, frames, fps, size, selection, exr_folder, colorspaces=("sRGB - Display", "ACEScg")):
    """The ComfyUI prompt that only finds the subject: the workflow's own plate loading, then SAM3.

    It loads and sizes the plate exactly as the matte job does, so the subject mask lines up with the matte.
    """
    if not selection or selection.get("mode") not in SAM_MODES:
        raise ValueError("Finding a subject needs Text, Click or Box.")
    width, height = size
    full = template()
    prompt = {node: full[node] for node in (LOAD_VIDEO, FRAME_LIMIT, PLATE_IMAGES[0])}
    prompt[LOAD_VIDEO]["inputs"].update(video=plate, force_rate=0, frame_load_cap=frames,
                                        skip_first_frames=0, select_every_nth=1, custom_width=0, custom_height=0)
    prompt[FRAME_LIMIT]["inputs"]["value"] = frames
    resize = prompt[PLATE_IMAGES[0]]["inputs"]
    resize["resize_type.shorter_size"] = min(width, height)
    if isinstance(resize.get("resize_type.shorter_size"), list):  # never linked to a node left out here
        resize["resize_type.shorter_size"] = min(width, height)
    prompt.update(selection_nodes(selection, frames))
    prompt[SELECT_WRITER] = exr_writer([SAM_MASK_IMAGE, 0], "Select", exr_folder, colorspaces)
    return prune(prompt)


def prune(prompt):
    """Leave out every node that no output depends on (such as a LoadImage with a missing file)."""
    outputs = [node for node, step in prompt.items()
               if step["class_type"] in ("VHS_VideoCombine", "OCIOWrite", "SaveImage")]
    keep, pending = set(), list(outputs)
    while pending:
        node = pending.pop()
        if node in keep or node not in prompt:
            continue
        keep.add(node)
        for value in prompt[node]["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                pending.append(value[0])
    return {node: copy.deepcopy(step) for node, step in prompt.items() if node in keep}


def outputs_of(prompt):
    """The files a render produces: EXR writer ids and the video outputs that are kept."""
    return {"matte": MATTE_WRITER, "select": SELECT_WRITER if SELECT_WRITER in prompt else None,
            "videos": {name: node for node, name in VIDEO_OUTPUTS.items() if node in prompt}}
