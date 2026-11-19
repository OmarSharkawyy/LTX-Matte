"""Nuke controls for LTX Matte: pick a subject with SAM3, then pull its matte with LTX Alpha Gen.

A render happens in two steps, both in the background:

1. Find Subject (Text, Click or Box): the plate goes to ComfyUI and SAM3 tracks the chosen object
   through the shot. Its mask comes back as Select.####.exr and is shown on the node.
2. Queue Matte: the plate goes to ComfyUI with everything outside the subject replaced by flat
   grey, so LTX, which cannot be told what to matte, sees only the subject. Its matte comes back as
   Matte.####.exr, and the node multiplies it by the subject mask to remove anything else.

Auto skips both: LTX mattes whatever it sees as foreground. Roto uses the second input as the subject.
"""
import json
import shutil
import tempfile
import threading
import time
from pathlib import Path
import uuid

import nuke

from . import client, graph
from .common import (STOPPED, WATCH_POLL_SECONDS, cancel_remote, check_server, combo_choices,
                     discard_empty_run, discard_staging, ffmpeg_timeout, follow_first, graph_context,
                     node_ffmpeg, node_host, node_portable_root, parse_seed, run_ffmpeg, server_shares_files,
                     show_detected, start_edited, valid_host, version_folder, watch_request)

ACTIVE = {}          # node full name -> (token, stop event)
RUNS_FOLDER = "LTXMatteRenders"
MATTE, SELECT = "Matte", "Select"


# --- creating the node and its controls -------------------------------------------------------

def create():
    node = nuke.createNode("LTXMatte")
    first = int(nuke.frame())
    node["plate_first"].setValue(first)  # Place render at follows it
    node["plate_last"].setValue(first + 48)
    node["select_frame"].setValue(first)
    node["start_at"].setValue(first)
    node["start_follows"].setValue(True)
    for name, model in graph.default_models().items():
        if node.knob(name) is not None and not node[name].value():
            node[name].setValue(model)
    return node


def knob_changed(node, knob):
    name = knob.name()
    if name == "plate_first":
        follow_first(node)
    elif name == "start_at":
        start_edited(node)
    elif name == "size_short":
        snapped = graph.SIZE_STEP * max(graph.MIN_SIDE // graph.SIZE_STEP, round(knob.value() / graph.SIZE_STEP))
        if snapped != knob.value():
            knob.setValue(snapped)
        show_size(node)
    if name in ("select_mode", "showPanel", "inputChange"):
        show_mode(node)
        show_size(node)


def show_mode(node):
    """Show only the controls of the chosen way to select the subject."""
    mode = node["select_mode"].value()
    for knob, modes in (("subject_text", ("Text",)), ("select_frame", ("Click", "Box")),
                        ("find_subject", ("Text", "Click", "Box")),
                        ("p1", ("Click",)), ("p2", ("Click",)), ("p3", ("Click",)),
                        ("p1_on", ("Click",)), ("p2_on", ("Click",)), ("p3_on", ("Click",)),
                        ("n1", ("Click",)), ("n1_on", ("Click",)), ("subject_box", ("Box",)),
                        ("isolate", ("Text", "Click", "Box", "Roto"))):
        if node.knob(knob) is not None:
            node[knob].setVisible(mode in modes)


def step_size(node, steps):
    knob = node["size_short"]
    knob.setValue(max(graph.MIN_SIDE, int(knob.value()) + steps * graph.SIZE_STEP))


def step_size_down(node):
    step_size(node, -1)


def step_size_up(node):
    step_size(node, 1)


def plate_fps(node):
    """The plate's own frame rate, such as 23.976, or the project's when the plate does not say."""
    plate = node.input(0)
    try:
        rate = float((plate.metadata() or {}).get("input/frame_rate") or 0) if plate is not None else 0
    except (TypeError, ValueError, RuntimeError):
        rate = 0
    return rate if 1 <= rate <= 240 else float(nuke.root()["fps"].value())


def plate_format(node):
    plate = node.input(0)
    if plate is None:
        raise ValueError("Connect the Plate arrow first.")
    fmt = plate.format()
    return fmt.width(), fmt.height()


def render_size(node):
    width, height = plate_format(node)
    return graph.render_size(width, height, int(node["size_short"].value()))


def show_size(node):
    knob = node.knob("size_info")
    if knob is None:
        return
    try:
        width, height = render_size(node)
        knob.setValue(f"Renders at {width}x{height}, then scaled back to the plate.")
    except ValueError:
        knob.setValue("Connect the Plate to see the render size.")


def use_plate_range(node):
    plate = node.input(0)
    if plate is None:
        nuke.message("Connect the Plate arrow first.")
        return
    first, last = int(plate.firstFrame()), int(plate.lastFrame())
    node["plate_first"].setValue(first)
    node["plate_last"].setValue(min(last, first + graph.MAX_FRAMES - 1))
    if last - first + 1 > graph.MAX_FRAMES:
        nuke.message(f"LTX Alpha Gen takes at most {graph.MAX_FRAMES} frames, so the range was set to "
                     f"{first}-{first + graph.MAX_FRAMES - 1}. Render the rest with another node or version.")


# --- render folders ------------------------------------------------------------------------------

def render_base(node):
    chosen = node["render_dir"].value().strip()
    if chosen:
        return Path(chosen).expanduser().resolve()
    project = Path(nuke.root().name())
    if project.suffix.lower() == ".nk":
        return (project.parent / RUNS_FOLDER).resolve()
    return Path.home() / "Documents" / RUNS_FOLDER


def run_folder(node):
    return version_folder(render_base(node), int(node["output_version"].value()))


def has_sequence(folder, name):
    return (folder / "EXR" / f"{name}.0001.exr").is_file()


def version_up(node):
    try:
        base = render_base(node)
        version = max(1, int(node["output_version"].value()) + 1)
        while version_folder(base, version).exists():
            version += 1
        node["output_version"].setValue(version)
    except (OSError, ValueError) as error:
        nuke.message(str(error))


# --- what to send ----------------------------------------------------------------------------------

def frame_range(node):
    return graph.frame_count(int(node["plate_first"].value()), int(node["plate_last"].value()))


def selection(node, size, first, last):
    """The SAM3 selection for the chosen mode, in render pixels, or None for Auto and Roto."""
    mode = node["select_mode"].value()
    if mode not in graph.SAM_MODES:
        return None
    plate_width, plate_height = plate_format(node)
    to_render = lambda x, y: graph.to_render_pixels(x, y, plate_width, plate_height, *size)
    frame = int(node["select_frame"].value()) if mode != "Text" else first
    if not first <= frame <= last:
        raise ValueError(f"Select on a frame inside the range {first}-{last}.")
    chosen = {"mode": mode, "frame": frame - first, "text": node["subject_text"].value()}
    if mode == "Click":
        chosen["positive"] = [to_render(*node[name].value()) for name in ("p1", "p2", "p3")
                              if node[name + "_on"].value()]
        chosen["negative"] = [to_render(*node["n1"].value())] if node["n1_on"].value() else []
    elif mode == "Box":
        x, y, right, top = node["subject_box"].value()
        x1, y2 = to_render(x, y)
        x2, y1 = to_render(right, top)
        chosen["box"] = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    return chosen


def export_plate(node, internal, destination, first, last, size):
    """Write the gizmo's internal `internal` node at the render size, fitted and padded, as PNG frames."""
    width, height = size
    reformat = writer = None
    node.begin()
    try:
        reformat = nuke.nodes.Reformat(inputs=[node.node(internal)])
        for name, value in (("type", "to box"), ("box_width", width), ("box_height", height),
                            ("box_fixed", True), ("resize", "fit"), ("center", True), ("black_outside", True)):
            reformat[name].setValue(value)
        writer = nuke.nodes.Write(inputs=[reformat])
        writer["file"].setValue(destination.as_posix())
        writer["file_type"].setValue("png")
        writer["channels"].setValue("rgb")
        nuke.execute(writer, first, last)
    finally:
        if writer is not None:
            nuke.delete(writer)
        if reformat is not None:
            nuke.delete(reformat)
        node.end()


def encode(ffmpeg, sequence, first, last, fps, destination):
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(fps),
            "-start_number", str(first), "-i", str(sequence), "-frames:v", str(last - first + 1),
            "-an", "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(destination)]
    result = run_ffmpeg(args, ffmpeg_timeout(last - first + 1), "encode the plate")
    if result.returncode:
        raise RuntimeError("Could not encode the plate. " + result.stderr.strip())


def ocio_colorspaces(host):
    """Any valid colorspace pair: the EXRs are written as raw data, so the pair does not change them."""
    info = client.json_request(host, "/object_info/OCIOWrite") or {}
    required = info.get("OCIOWrite", {}).get("input", {}).get("required", {})
    sources = combo_choices(required.get("input_colorspace")) or []
    targets = combo_choices(required.get("output_colorspace")) or []
    if not sources or not targets:
        raise RuntimeError("The ComfyUI server needs ComfyUI-OCIO's OCIO Write node to write the EXRs.")
    return sources[0], targets[0]


# --- the two steps ---------------------------------------------------------------------------------

def find_subject(node):
    """Step 1: track the chosen subject with SAM3 and show its mask."""
    if node["select_mode"].value() not in graph.SAM_MODES:
        nuke.message("Find Subject is for Text, Click and Box. Auto needs no subject; Roto uses the second input.")
        return
    start(node, "select")


def queue(node):
    """Step 2: render the matte, with the subject isolated when one was chosen."""
    start(node, "matte")


def start(node, kind):
    if node.fullName() in ACTIVE:
        nuke.message("This node is already waiting for ComfyUI.")
        return
    staging = folder = None
    created = started = False
    try:
        first, last = frame_range(node)
        size = render_size(node)
        mode = node["select_mode"].value()
        chosen = selection(node, size, first, last)
        seed = parse_seed(node["seed_text"].value())
        fps = plate_fps(node)
        host = valid_host(node["server"].value())
        comfy_root = node_portable_root(node, host)
        ffmpeg = node_ffmpeg(node, comfy_root, host)
        show_detected(node, ffmpeg, comfy_root)
        folder = run_folder(node)
        if has_sequence(folder, MATTE):
            raise ValueError(f"{folder} already has a matte. Click Version Up Output for a new one.")
        if kind == "matte" and mode in graph.SAM_MODES:
            saved = read_selection_info(folder)
            if not has_sequence(folder, SELECT) or saved != selection_key(chosen, first, last, size):
                raise ValueError("Click Find Subject first: the matte uses the subject it finds, and the subject, "
                                 "frame range or size changed since the last search.")
        if kind == "matte" and mode == "Roto" and node.input(1) is None:
            raise ValueError("Roto mode uses the second input: connect a Roto or another mask to Roto.")
        check_server(host)
        created = not folder.exists()
        folder.mkdir(parents=True, exist_ok=True)
        job_name = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        (folder / "ComfyRun.txt").write_text(job_name, encoding="utf-8")
        staging = tempfile.TemporaryDirectory(prefix="input_", dir=folder)
        sequence = Path(staging.name) / "Plate.%04d.png"
        isolate = kind == "matte" and mode != "Auto" and node["isolate"].value()
        source = "ToRender" if isolate else "PlateIn"
        node["job_status"].setValue(f"Exporting {'the isolated ' if isolate else ''}plate {first}-{last}")
        export_plate(node, source, sequence, first, last, size)
        settings = dict(kind=kind, size=size, frames=last - first + 1, first=first, last=last, fps=fps, seed=seed,
                        selection=chosen, job_name=job_name, isolate=isolate,
                        positive=node["positive"].value(), negative=node["negative"].value(),
                        models={key: node[key].value().strip() for key in graph.MODEL_INPUTS},
                        lora_strength=float(node["lora_strength"].value()))
        start_worker(node, host, folder, settings, (staging, ffmpeg, sequence))
        started = True
    except (OSError, ValueError, RuntimeError) as error:
        node["job_status"].setValue("Stopped")
        nuke.message(str(error))
    finally:
        if not started:
            if staging is not None:
                discard_staging(staging)
            if created and folder is not None:
                discard_empty_run(folder)


def selection_key(chosen, first, last, size):
    return json.dumps({"selection": chosen, "first": first, "last": last, "size": list(size)}, sort_keys=True)


def read_selection_info(folder):
    try:
        return (folder / "Selection.json").read_text(encoding="utf-8")
    except OSError:
        return None


def start_worker(node, host, folder, settings, export):
    key = node.fullName()
    token = uuid.uuid4().hex
    stop = threading.Event()
    ACTIVE[key] = (token, stop)
    try:
        node["job_status"].setValue("Connecting to ComfyUI")
        context = (key, token, nuke.root().name())
        threading.Thread(target=run, args=(context, host, folder, settings, export, stop),
                         daemon=True, name="LTXMatte").start()
    except Exception:
        ACTIVE.pop(key, None)
        raise


def cancel(node):
    active = ACTIVE.get(node.fullName())
    if active is None:
        nuke.message("This node has no active request.")
        return
    active[1].set()
    node["job_status"].setValue("Cancel requested")


# --- the background job ----------------------------------------------------------------------------

def run(context, host, folder, settings, export, stop):
    staging, ffmpeg, sequence = export
    kind = settings["kind"]
    job_id = None
    try:
        name = "Plate.mp4" if kind == "select" else ("Isolated.mp4" if settings["isolate"] else "Plate.mp4")
        video = Path(staging.name) / name
        publish(context, "Encoding the plate")
        encode(ffmpeg, sequence, settings["first"], settings["last"], settings["fps"], video)
        video = Path(shutil.copyfile(video, folder / name))
        discard_staging(staging)
        if stop.is_set():
            return publish(context, "Request canceled", terminal=True)
        publish(context, "Uploading the plate")
        uploaded = client.upload(host, video)
        local = server_shares_files(host)
        exr_folder = (folder / "EXR").as_posix() if local else f"LTXMatte/{settings['job_name']}/EXR"
        prompt = build(settings, uploaded, exr_folder, ocio_colorspaces(host))
        reply = client.json_request(host, "/prompt", {"prompt": prompt, "client_id": context[1]})
        job_id = reply["prompt_id"]
        publish(context, "Queued in ComfyUI", job=(job_id, host, folder))
        entry = wait(context, host, job_id, stop)
        if entry is None:
            return
        publish(context, "Collecting the results")
        writer = graph.SELECT_WRITER if kind == "select" else graph.MATTE_WRITER
        sequence_name = SELECT if kind == "select" else MATTE
        first_exr = collect(host, folder, entry, writer, sequence_name, exr_folder, local, stop)
        if first_exr is None:
            return publish(context, "Canceled while collecting", terminal=True)
        if kind == "select":
            key = selection_key(settings["selection"], settings["first"], settings["last"], settings["size"])
            (folder / "Selection.json").write_text(key, encoding="utf-8")
            publish(context, "Subject found: check it with View > Subject, then Queue Matte",
                    result={"select": first_exr}, terminal=True)
        else:
            sheet = None
            item = video_output(entry, graph.outputs_of(prompt)["videos"].get("ContactSheet"))
            if item is not None:
                sheet = folder / "ContactSheet.mp4"
                client.download(host, item, sheet)
            publish(context, "Complete", result={"matte": first_exr, "sheet": sheet}, terminal=True)
    except Exception as error:  # every failure ends as a readable status on the node
        publish(context, f"Error: {error}", terminal=True)


def build(settings, uploaded, exr_folder, colorspaces):
    if settings["kind"] == "select":
        return graph.build_select_prompt(uploaded, settings["frames"], settings["fps"], settings["size"],
                                         settings["selection"], exr_folder, colorspaces)
    return graph.build_prompt(uploaded, settings["frames"], settings["fps"], settings["size"], settings["seed"],
                              settings["positive"], settings["negative"], settings["models"],
                              settings["job_name"], None, exr_folder, colorspaces, settings["lora_strength"])


def wait(context, host, job_id, stop):
    """Follow the job until ComfyUI finishes it; None when canceled."""
    started, last_status = time.monotonic(), None

    def reconnecting(text):
        nonlocal last_status
        last_status = None
        publish(context, text)

    while time.monotonic() - started < 12 * 3600:
        if stop.is_set():
            publish(context, cancel_remote(host, job_id), terminal=True)
            return None
        history = watch_request(host, f"/history/{job_id}", stop, reconnecting)
        if history is STOPPED:
            continue
        entry = (history or {}).get(job_id)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                details = next((data for event, data in reversed(status.get("messages", []))
                                if event in {"execution_error", "execution_interrupted"}), {})
                raise RuntimeError(details.get("exception_message") or "ComfyUI stopped with an error.")
            if status.get("completed"):
                return entry
        queue_state = watch_request(host, "/queue", stop, reconnecting)
        if queue_state is STOPPED:
            continue
        running = any(item[1] == job_id for item in (queue_state or {}).get("queue_running", []))
        current = "Rendering in ComfyUI" if running else "Queued in ComfyUI"
        if current != last_status:
            publish(context, current)
            last_status = current
        stop.wait(WATCH_POLL_SECONDS)
    raise RuntimeError("Stopped watching after 12 hours. Check the job in ComfyUI.")


def collect(host, folder, entry, writer, name, exr_folder, local, stop):
    """Make sure every frame of an EXR sequence is in folder/EXR, downloading it when ComfyUI is remote."""
    try:
        count = int(entry["outputs"][writer]["count"][0])
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError(f"ComfyUI finished without writing the {name} frames.") from None
    target = folder / "EXR"
    if not local:
        target.mkdir(parents=True, exist_ok=True)
        for frame in range(1, count + 1):
            if stop.is_set():
                return None
            filename = f"{name}.{frame:04d}.exr"
            client.download(host, {"filename": filename, "subfolder": exr_folder, "type": "output"}, target / filename)
    if count < 1 or not all((target / f"{name}.{frame:04d}.exr").is_file() for frame in range(1, count + 1)):
        raise RuntimeError(f"ComfyUI reported the {name} frames, but some are missing.")
    return target / f"{name}.0001.exr"


def video_output(entry, node_id):
    """The first file a VHS Video Combine node reported, or None."""
    for records in (entry.get("outputs", {}).get(node_id, {}) or {}).values():
        if isinstance(records, list):
            for item in records:
                if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(".mp4"):
                    return item
    return None


# --- results back on the node ----------------------------------------------------------------------

def publish(context, status, job=None, result=None, terminal=False):
    try:
        nuke.executeInMainThread(apply_update, args=(context, status, job, result, terminal))
    except Exception:
        if terminal:
            release(context)


def release(context):
    key, token, _ = context
    active = ACTIVE.get(key)
    if active is not None and active[0] == token:
        ACTIVE.pop(key, None)


def apply_update(context, status, job, result, terminal):
    key, token, project = context
    active = ACTIVE.get(key)
    if active is None or active[0] != token:
        return
    if terminal:
        ACTIVE.pop(key, None)
    if nuke.root().name() != project:
        return
    with graph_context(nuke.root()):
        node = nuke.toNode(key)
    if node is None or node.knob("job_status") is None:
        return
    node["job_status"].setValue(status.splitlines()[0])
    if status.startswith("Error:"):
        nuke.message(status)
    if job:
        job_id, host, folder = job
        node["job_id"].setValue(job_id)
        node["job_host"].setValue(host)
        node["job_folder"].setValue(folder.as_posix())
    for kind, path in (result or {}).items():
        if path is None:
            continue
        if kind == "sheet":
            node["sheet_path"].setValue(path.as_posix())
            continue
        node[f"{kind}_path"].setValue(path.as_posix())
        load_sequence(node.node("SelectRead" if kind == "select" else "MatteRead"), path,
                      int(node["plate_first"].value()))
        if kind == "select":
            node["view"].setValue("Subject")
        else:
            node["view"].setValue("Final")


def load_sequence(reader, first_exr, start_at):
    stem = first_exr.name.split(".")[0]
    frames = sorted(int(item.name.split(".")[1]) for item in first_exr.parent.glob(f"{stem}.*.exr")
                    if item.name.split(".")[1].isdigit())
    if not frames:
        raise ValueError(f"No {stem} frames in {first_exr.parent}.")
    pattern = (first_exr.parent / f"{stem}.%04d.exr").as_posix()
    raw = reader.knob("raw")
    if raw is not None:
        raw.setValue(True)
    reader["file"].fromUserText(f"{pattern} {frames[0]}-{frames[-1]}")
    for knob, value in (("first", frames[0]), ("last", frames[-1]),
                        ("origfirst", frames[0]), ("origlast", frames[-1])):
        reader[knob].setValue(value)
    reader["frame_mode"].setValue("start at")
    reader["frame"].setValue(str(start_at))
    reader["reload"].execute()


def reload_results(node):
    """Load the current version's Select and Matte frames, for example after reopening a script."""
    folder = run_folder(node)
    found = False
    for kind, name in (("select", SELECT), ("matte", MATTE)):
        if has_sequence(folder, name):
            load_sequence(node.node("SelectRead" if kind == "select" else "MatteRead"),
                          folder / "EXR" / f"{name}.0001.exr", int(node["plate_first"].value()))
            node[f"{kind}_path"].setValue((folder / "EXR" / f"{name}.0001.exr").as_posix())
            found = True
    if not found:
        nuke.message(f"No Select or Matte frames in {folder} yet.")


def bring(node, name, x_offset):
    """A Read of this version's Matte frames or contact sheet, next to the node."""
    folder = run_folder(node)
    path = folder / "EXR" / f"{name}.0001.exr" if name == MATTE else folder / "ContactSheet.mp4"
    if not path.is_file():
        nuke.message(f"{path.name} is not in {folder} yet.")
        return None
    parent_name = node.fullName().rpartition(".")[0]
    with graph_context(nuke.root()):
        parent = nuke.toNode(parent_name) if parent_name else nuke.root()
    with graph_context(parent):
        reader = nuke.nodes.Read()
        if name == MATTE:
            load_sequence(reader, path, int(node["start_at"].value()))
        else:
            reader["file"].fromUserText(path.as_posix())
            reader["frame_mode"].setValue("start at")
            reader["frame"].setValue(str(int(node["start_at"].value())))
        reader.setXYpos(node.xpos() + x_offset, node.ypos() + 85)
    return reader


def bring_matte(node):
    return bring(node, MATTE, -150)


def bring_sheet(node):
    return bring(node, "ContactSheet", 150)


# --- Check Setup ----------------------------------------------------------------------------------

def verify_setup(host, models):
    from . import validate
    check_server(host)
    catalog = client.json_request(host, "/object_info") or {}
    colorspaces = ocio_colorspaces(host)
    prompt = graph.build_prompt("plate.mp4", 9, 24, (512, 512), 42, "", "", models, "check", None,
                                "check", colorspaces)
    found = [problem for problem in validate.problems(prompt, catalog) if "plate.mp4" not in problem]
    if found:
        raise RuntimeError("The matte workflow does not fit this ComfyUI:\n" + "\n".join(found[:12]))
    lines = [f"Matte workflow: {len(prompt)} nodes, models and node types found."]
    select = graph.build_select_prompt("plate.mp4", 9, 24, (512, 512), {"mode": "Text", "text": "x"},
                                       "check", colorspaces)
    missing = [problem for problem in validate.problems(select, catalog) if "plate.mp4" not in problem]
    lines.append("Subject selection (SAM3): ready." if not missing else
                 "Subject selection (SAM3) is not available, so only Auto and Roto work:\n" + "\n".join(missing[:6]))
    return "\n".join(lines)


def check_setup(node):
    try:
        host = valid_host(node["server"].value())
        root = node_portable_root(node, host)
        ffmpeg = node_ffmpeg(node, root, host)
        models = {key: node[key].value().strip() for key in graph.MODEL_INPUTS}
    except (OSError, ValueError) as error:
        nuke.message(str(error))
        return
    show_detected(node, ffmpeg, root)
    found = f"FFmpeg: {ffmpeg}\nComfyUI folder: {root or 'not found on this computer (optional)'}\n\n"

    def check():
        try:
            report = verify_setup(host, models)
        except Exception as error:
            report = f"Setup check failed:\n{error}"
        nuke.executeInMainThread(nuke.message, args=(found + report,))

    threading.Thread(target=check, daemon=True, name="LTXMatteSetup").start()


def host_of(node):
    return node["job_host"].value() or node_host(node)
