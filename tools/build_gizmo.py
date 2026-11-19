"""Build LTXMatte.gizmo. Run inside Nuke:  Nuke -t tools/build_gizmo.py

The gizmo is a Group of ordinary Nuke nodes: the subject mask and the LTX matte come back as EXR
sequences into two Reads, are scaled back onto the plate, and are combined live, so Expand,
Softness and View change the result at once without rendering again.
"""
from pathlib import Path

import nuke

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = "from ltx_matte import bridge; bridge.{}(nuke.thisNode())"
MODES = ["Auto", "Text", "Click", "Box", "Roto"]
VIEWS = ["Final", "Matte", "Subject", "Isolated", "Plate"]


def knob(group, item, tooltip=None, start=True):
    if tooltip:
        item.setTooltip(tooltip)
    if start:
        item.setFlag(nuke.STARTLINE)
    else:
        item.clearFlag(nuke.STARTLINE)
    group.addKnob(item)
    return item


def button(group, name, label, call, tooltip, start=True):
    return knob(group, nuke.PyScript_Knob(name, label, BRIDGE.format(call)), tooltip, start)


def text(group, name, html):
    return knob(group, nuke.Text_Knob(name, "", html))


def hidden(group, name, kind=nuke.String_Knob):
    item = kind(name, name)
    item.setFlag(nuke.INVISIBLE)
    group.addKnob(item)
    return item


def add_controls(group):
    group.addKnob(nuke.Tab_Knob("matte_tab", "LTX Matte"))
    text(group, "guide", "Connect the Plate. Pick the subject, click <b>Find Subject</b>, then <b>Queue Matte</b>.")
    text(group, "range_title", "<b>Frame range</b>")
    knob(group, nuke.Int_Knob("plate_first", "First"), "First frame of the plate to matte.")
    knob(group, nuke.Int_Knob("plate_last", "Last"),
         "Last frame, inclusive. LTX takes 9, 17, 25 ... up to 145 frames, so the end may be trimmed.", start=False)
    button(group, "use_range", "Use Plate Range", "use_plate_range",
           "Copy the connected plate's frame range, up to 145 frames.", start=False)

    text(group, "subject_title", "<b>Subject</b>")
    knob(group, nuke.Enumeration_Knob("select_mode", "Select", MODES),
         "Auto: LTX mattes whatever it sees as foreground. Text: type what to matte. Click: place points on it "
         "in the Viewer. Box: draw a box around it in the Viewer. Roto: use the mask on the second input.")
    knob(group, nuke.String_Knob("subject_text", "Object"),
         "What to matte, in a few words: doll, horse, red car. Several with commas: rider, horse.")
    knob(group, nuke.Int_Knob("select_frame", "On frame"),
         "The frame where your points or box sit. The subject is tracked from there through the range.")
    for index in (1, 2, 3):
        knob(group, nuke.XY_Knob(f"p{index}", f"Point {index}"), "A point on the subject. Drag it in the Viewer.")
        knob(group, nuke.Boolean_Knob(f"p{index}_on", "use"), "Use this point.", start=False)
    knob(group, nuke.XY_Knob("n1", "Not this"), "A point on something that must not be in the matte.")
    knob(group, nuke.Boolean_Knob("n1_on", "use"), "Use this point.", start=False)
    knob(group, nuke.BBox_Knob("subject_box", "Box"), "A box around the subject. Drag its corners in the Viewer.")
    button(group, "find_subject", "Find Subject", "find_subject",
           "Track the subject through the range with SAM3 and show its mask (View > Subject).")
    knob(group, nuke.Boolean_Knob("isolate", "Isolate subject for LTX"),
         "LTX cannot be told what to matte, so everything outside the subject is replaced with flat grey "
         "before it renders. Turn off to send the plate unchanged.")
    knob(group, nuke.Double_Knob("iso_expand", "Isolate margin"),
         "Pixels kept around the subject when isolating it, so its edges reach LTX intact.")
    knob(group, nuke.Double_Knob("garb_expand", "Clean margin"),
         "The finished matte is cut to the subject plus this many pixels, which removes anything else LTX found.")
    knob(group, nuke.Double_Knob("garb_soft", "Clean softness"), "Softness of that cut, in pixels.", start=False)
    knob(group, nuke.Double_Knob("halo_cut", "Halo cut"),
         "Around the subject, matte values below this are removed and the rest stretched back to full. Hair "
         "strands are strong and stay; the faint ring LTX can leave at the edge of the isolated plate goes. "
         "0 keeps everything LTX gave.")
    knob(group, nuke.Boolean_Knob("garbage_on", "Clean matte with subject"),
         "Multiply the matte by the subject mask. Applies whenever a subject is selected.")

    text(group, "output_title", "<b>Output</b>")
    knob(group, nuke.Int_Knob("size_short", "Size"),
         "The plate's shorter side while LTX renders, a multiple of 32. The matte is scaled back to the plate. "
         "Larger is sharper and slower; LTX takes at most 1920x1088.")
    button(group, "size_down", "▼", "step_size_down", "32 pixels smaller.", start=False)
    button(group, "size_up", "▲", "step_size_up", "32 pixels larger.", start=False)
    knob(group, nuke.Text_Knob("size_info", "", "Connect the Plate to see the render size."), start=False)
    knob(group, nuke.Enumeration_Knob("view", "View", VIEWS),
         "Final: the plate with the matte as alpha, premultiplied. Matte: the alpha alone. Subject: the selected "
         "subject over the dimmed plate. Isolated: what LTX receives. Plate: the input unchanged.")
    knob(group, nuke.Int_Knob("start_at", "Place render at"),
         "Timeline frame where the results start. It follows First unless you type a different frame.")
    hidden(group, "start_follows", nuke.Boolean_Knob)
    knob(group, nuke.String_Knob("seed_text", "Seed"), "Noise seed. Change it for a variation.")
    knob(group, nuke.File_Knob("render_dir", "Render folder"),
         "Parent folder for numbered runs. Empty: an LTXMatteRenders folder next to the script.")
    knob(group, nuke.Int_Knob("output_version", "Version"), "The numbered run: version 1 uses 0001.")
    button(group, "version_up_output", "Version Up Output", "version_up", "Select the next unused version.", start=False)
    button(group, "queue_job", "Queue Matte", "queue", "Render the matte in ComfyUI.")
    button(group, "cancel_job", "Cancel Request", "cancel", "Stop this node's active job.", start=False)
    button(group, "reload", "Reload Results", "reload_results",
           "Load this version's Subject and Matte frames again, for example after reopening the script.", start=False)
    button(group, "read_matte", "Bring Matte", "bring_matte", "Create a Read of the Matte EXR frames.")
    button(group, "read_sheet", "Bring Contact Sheet", "bring_sheet", "Create a Read of the contact sheet video.",
           start=False)
    status = knob(group, nuke.String_Knob("job_status", "Status"))
    status.setEnabled(False)
    for name in ("job_id", "job_host", "job_folder", "sheet_path", "matte_path", "select_path"):
        hidden(group, name)

    group.addKnob(nuke.Tab_Knob("settings_tab", "Settings"))
    knob(group, nuke.String_Knob("server", "ComfyUI URL"), "Address of the ComfyUI server that renders.")
    hidden(group, "ffmpeg_path", nuke.File_Knob)
    hidden(group, "comfy_root", nuke.File_Knob)
    button(group, "check_setup", "Check Setup", "check_setup",
           "Find FFmpeg, then check the server, the workflow's nodes and models, and SAM3.")
    text(group, "detected_paths", "FFmpeg and the ComfyUI folder are found automatically. Click Check Setup to see them.")
    text(group, "prompt_title", "<b>Prompts</b><br>Lightricks trains Alpha Gen with an empty prompt, so these "
                                "rarely change the matte. They are kept because your workflow has them.")
    for name in ("positive", "negative"):
        item = nuke.Multiline_Eval_String_Knob(name, name)
        item.setFlag(nuke.INVISIBLE)
        group.addKnob(item)
    knob(group, nuke.PyCustom_Knob("prompt_editor", "",
                                   "__import__('ltx_matte.prompt_ui', fromlist=('PromptEditor',)).PromptEditor()"))
    knob(group, nuke.Double_Knob("lora_strength", "LoRA strength"), "Alpha Gen LoRA strength. Lightricks uses 1.0.")

    group.addKnob(nuke.Tab_Knob("models_tab", "Models"))
    for name, label in (("transformer", "Transformer"), ("text_encoder", "Text encoder"),
                        ("prompt_enhancer", "Prompt enhancer"), ("video_vae", "Video VAE"),
                        ("audio_vae", "Audio VAE"), ("alpha_lora", "Alpha Gen LoRA")):
        knob(group, nuke.String_Knob(name, label), "Filename on the ComfyUI server.")


def defaults(group):
    from ltx_matte import graph
    values = {"plate_first": 1, "plate_last": 49, "select_frame": 1, "select_mode": "Text", "subject_text": "",
              "p1_on": True, "isolate": True, "iso_expand": 12, "garb_expand": 24, "garb_soft": 12,
              "garbage_on": True, "halo_cut": 0.35, "size_short": 704, "view": "Final", "start_at": 1, "start_follows": True,
              "seed_text": "42", "output_version": 1, "server": "http://127.0.0.1:8188", "job_status": "Idle",
              "lora_strength": 1.0, "positive": "", "negative": ""}
    values.update(graph.default_models())
    for name, value in values.items():
        group[name].setValue(value)
    group["p1"].setValue((960, 540))
    group["n1"].setValue((200, 540))
    group["subject_box"].setValue((700, 200, 1200, 900))
    group["knobChanged"].setValue("from ltx_matte import bridge; bridge.knob_changed(nuke.thisNode(), nuke.thisKnob())")


def build_internals(group):
    with group:
        plate = nuke.nodes.Input(name="Plate", number=0)
        roto = nuke.nodes.Input(name="Roto", number=1)
        plate_in = nuke.nodes.Dot(name="PlateIn", inputs=[plate])

        def read(name):
            node = nuke.nodes.Read(name=name)
            node["on_error"].setValue("black")
            node["raw"].setValue(True)
            return node

        def fit_to_plate(name, source):
            # The inverse of the export's fit: scale the render back up so it fills the plate, centred.
            node = nuke.nodes.Reformat(name=name, inputs=[source])
            node["type"].setValue("to box")
            node["box_fixed"].setValue(True)
            node["box_width"].setExpression("PlateIn.width")
            node["box_height"].setExpression("PlateIn.height")
            node["resize"].setValue("fill")
            node["center"].setValue(True)
            return node

        def red_to_alpha(name, source):
            node = nuke.nodes.Expression(name=name, inputs=[source])
            node["expr3"].setValue("r")
            return node

        subject_sam = red_to_alpha("SubjectFromSam", fit_to_plate("SelectFit", read("SelectRead")))
        subject = nuke.nodes.Switch(name="SubjectSource", inputs=[subject_sam, roto])
        subject["which"].setExpression("parent.select_mode == 4")

        iso_grow = nuke.nodes.Dilate(name="IsolateGrow", inputs=[subject], channels="alpha")
        iso_grow["size"].setExpression("parent.iso_expand")
        iso_soft = nuke.nodes.Blur(name="IsolateSoft", inputs=[iso_grow], channels="alpha")
        iso_soft["size"].setExpression("parent.iso_expand / 2")
        fill = nuke.nodes.Expression(name="IsolateFill", inputs=[plate_in])
        for index in range(3):
            fill[f"expr{index}"].setValue("0.18")  # flat linear mid grey
        to_render = nuke.nodes.Keymix(name="ToRender", inputs=[fill, plate_in, iso_soft])
        to_render["maskChannel"].setValue("rgba.alpha")

        matte = red_to_alpha("MatteAlpha", fit_to_plate("MatteFit", read("MatteRead")))
        garb_grow = nuke.nodes.Dilate(name="CleanGrow", inputs=[subject], channels="alpha")
        garb_grow["size"].setExpression("parent.garb_expand")
        garb_soft = nuke.nodes.Blur(name="CleanSoft", inputs=[garb_grow], channels="alpha")
        garb_soft["size"].setExpression("parent.garb_soft")
        # Inside the subject the LTX matte is kept as it is. In the band around it, only strong values
        # survive Halo cut: hair strands are strong, while the faint ring LTX sometimes leaves where the
        # isolated plate meets the grey is weak. Outside the band everything goes.
        core = nuke.nodes.Blur(name="CoreSoft", inputs=[subject], channels="alpha", size=3)
        with_core = nuke.nodes.Copy(name="PackCore", inputs=[matte, core], from0="rgba.alpha", to0="rgba.red")
        with_band = nuke.nodes.Copy(name="PackBand", inputs=[with_core, garb_soft], from0="rgba.alpha",
                                    to0="rgba.green")
        cleaned = nuke.nodes.Expression(name="CleanMatte", inputs=[with_band])
        cleaned["temp_name0"].setValue("h")
        cleaned["temp_expr0"].setValue("parent.halo_cut")
        cleaned["expr3"].setValue("r * a + (1 - r) * g * clamp((a - h) / max(1 - h, 0.001))")
        use_clean = nuke.nodes.Switch(name="UseClean", inputs=[matte, cleaned])
        use_clean["which"].setExpression("parent.garbage_on && parent.select_mode != 0")

        with_alpha = nuke.nodes.Copy(name="MatteToPlate", inputs=[plate_in, use_clean], from0="rgba.alpha",
                                     to0="rgba.alpha")
        final = nuke.nodes.Premult(name="Final", inputs=[with_alpha])
        matte_view = nuke.nodes.Expression(name="MatteView", inputs=[use_clean])
        for index in range(3):
            matte_view[f"expr{index}"].setValue("a")
        subject_on_plate = nuke.nodes.Copy(name="SubjectOnPlate", inputs=[plate_in, subject], from0="rgba.alpha",
                                           to0="rgba.alpha")
        subject_view = nuke.nodes.Expression(name="SubjectView", inputs=[subject_on_plate])
        for index, channel in enumerate("rgb"):
            subject_view[f"expr{index}"].setValue(f"{channel} * (0.12 + 0.88 * a)")
        view = nuke.nodes.Switch(name="View", inputs=[final, matte_view, subject_view, to_render, plate_in])
        view["which"].setExpression("parent.view")
        nuke.nodes.Output(inputs=[view])
        x = 0
        for node in group.nodes():
            node.setXYpos(x, (x // 10) * 3)
            x += 110


def main():
    group = nuke.nodes.Group(name="LTXMatte")
    add_controls(group)
    defaults(group)
    build_internals(group)
    group["tile_color"].setValue(0x2E6E5Aff)
    group["label"].setValue("LTX Matte")
    for node in nuke.allNodes():
        node.setSelected(False)
    group.setSelected(True)
    text_path = ROOT / "LTXMatte.gizmo"
    nuke.nodeCopy(str(text_path))
    body = text_path.read_text(encoding="utf-8")
    start = body.index("Group {")
    gizmo = "Gizmo {" + body[start + len("Group {"):]
    gizmo = gizmo.replace(" name LTXMatte\n", "", 1).replace(" selected true\n", "", 1)
    text_path.write_text(gizmo, encoding="utf-8", newline="\n")
    print("wrote", text_path, len(gizmo), "bytes")


main()
