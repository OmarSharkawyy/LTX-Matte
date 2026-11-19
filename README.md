# LTX Matte

Pull a clean alpha matte for one subject in a shot, without leaving Nuke. Pick the subject by typing what it is, clicking on it or drawing a box, and LTX Matte isolates it and renders its matte with the LTX-2.5 Alpha Gen model in ComfyUI. Hair, fur and soft edges come from LTX; anything else in the frame is left out.

Made by [Omar El Sharkawy](https://github.com/OmarSharkawyy).

| Gizmo | What it does |
| --- | --- |
| **LTX Matte** | Finds the subject you choose with SAM3, isolates it, and renders its alpha matte with LTX-2.5 Alpha Gen, then brings the matte back onto the plate as a premultiplied RGBA. |

## How it picks one subject

LTX-2.5 Alpha Gen cannot be told what to matte: Lightricks trains it with an empty prompt, and it has no mask input, so it mattes whatever it sees as foreground. In a shot with a puppet and the people holding it, it mattes all of them. LTX Matte solves that in two steps:

1. **Find Subject.** SAM3 finds the subject from a word, a click or a box and tracks it through the frame range. Its mask comes back to the node, so you can check it before any long render.
2. **Queue Matte.** Everything outside the subject, plus a small margin, is replaced with flat grey before the plate goes to LTX. With nothing else left in the frame, LTX mattes only the subject, and keeps the soft edges SAM3 cannot draw. The finished matte is then cut to the subject mask once more, which removes anything that still came through.

### Tested on a real shot

A puppet on a green screen, held on rods by three puppeteers whose hands touch it (25 frames, rendered at 1280x704 on an RTX 2060):

- **Without isolation**, LTX matted the doll and all three people as one shape.
- **Find Subject** with `doll, chainsaw` picked the doll and the chainsaw in its hand on every frame, and left out the people and the rods (54 seconds).
- **With isolation**, LTX matted only the doll and its chainsaw, with every strand of hair, and none of the people, including the hands and rods touching it.

## Requirements

- **Nuke on Windows.** Developed against **Nuke 16.0v5**.
- **ComfyUI** with these custom nodes: [ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo), [ComfyUI-VideoHelperSuite](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite), [ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes), [ComfyUI-OCIO](https://github.com/SlavaSexton/ComfyUI-OCIO) for the EXR frames, and [ComfyUI-SAM3](https://github.com/PozzettiAndrea/ComfyUI-SAM3) for choosing a subject. Without SAM3, the Auto and Roto modes still work.
- **Models**, under `ComfyUI/models`:

| Field | File | Folder |
| --- | --- | --- |
| Transformer | `ltx-2.5-22b-distilled-transformer-bf16.safetensors` | `diffusion_models/` |
| Text encoder | `gemma4-12b-with-proj-ltx-2.5-bf16.safetensors` | `text_encoders/` |
| Prompt enhancer | `gemma4_e2b_it_bf16.safetensors` | `text_encoders/` |
| Video VAE | `ltx-2.5-video-vae-bf16.safetensors` | `vae/` |
| Audio VAE | `ltx-2.5-audio-vae-bf16.safetensors` | `vae/` |
| Alpha Gen LoRA | `ltx-2.5-22b-ic-lora-alpha-gen-0.9.safetensors` from [Lightricks/LTX-2.5-22b-IC-LoRA-Alpha-Gen](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Alpha-Gen) | `loras/` |

  The LTX-2.5 files are in [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5). SAM3 downloads its own model the first time it runs.
- **FFmpeg** with `libx264`. The gizmo finds it by itself, including the copy bundled with ComfyUI.

## Install

1. Download `LTX_Matte_Nuke16.zip` from the [latest release](https://github.com/OmarSharkawyy/LTX-Matte/releases/latest), or the repository from **Code → Download ZIP**, and extract it. Both give a folder named `LTX-Matte-main`. Copy it into `%USERPROFILE%\.nuke`.
2. Open `%USERPROFILE%\.nuke\init.py`, or create it, and add:

   ```python
   import nuke
   nuke.pluginAddPath("./LTX-Matte-main")
   ```

3. Start ComfyUI, restart Nuke, and add **ComfyUI → LTX Matte**.
4. Open **Settings** and click **Check Setup**. It checks the server, every node and model the workflow needs, and whether SAM3 is there.

## Use

1. Connect the **Plate**. Click **Use Plate Range**, or set **First** and **Last**. LTX takes 9, 17, 25 ... up to 145 frames; a longer range is trimmed. The plate's own frame rate is used (23.976 stays 23.976 in a 24 fps project), and every frame is sent and loaded as it is, never resampled, so the matte lines up frame for frame.
2. Choose how to **Select** the subject:

   | Select | How |
   | --- | --- |
   | **Text** | Type what it is in **Object**: `doll`, `horse`, `red car`. For several things together, separate them with commas: `doll, chainsaw`. Each is found on its own and the masks are joined, because SAM3 misses objects when they are asked for in one search. |
   | **Click** | Go to the **On frame** frame and drag **Point 1** onto the subject in the Viewer. Add Points 2 and 3 for a large subject, and **Not this** on something that must stay out. |
   | **Box** | Drag the **Box** around the subject in the Viewer on the **On frame** frame. |
   | **Roto** | Connect your own Roto or mask to the second input, **Roto**. No Find Subject step. |
   | **Auto** | No selection: LTX mattes whatever it sees as foreground. |

3. Click **Find Subject** (Text, Click, Box). **Detect threshold** (0.15) is how sure SAM3 must be: lower it when part of the subject is missing, raise it when other things get picked too. On the test shot, 0.3 (SAM3's own default) missed the toy chainsaw, 0.15 found it, and 0.1 also took a puppeteer as "doll". When it finishes, **View** switches to **Subject**: the subject is bright and the rest of the plate dimmed. If it picked the wrong thing, change the word, the points or the box and find it again.
4. Click **Queue Matte**. **View** → **Isolated** shows what LTX receives. When the matte is back, **View** switches to **Final**: the plate with the matte as alpha, premultiplied.
5. Adjust without rendering again. **View** → **Matte** shows the alpha alone.
   - Inside the subject mask, the LTX matte is kept as it is.
   - Around it, in a band of **Clean margin** pixels softened by **Clean softness**, only matte values above **Halo cut** stay, stretched back to full strength. Hair strands are strong and stay; the faint ring LTX can leave where the isolated plate meets the grey is weak and goes. **Halo cut** 0 keeps everything LTX gave.
   - Outside the band, everything is removed.
6. **Bring Matte** creates a Read of the EXR frames; **Bring Contact Sheet** creates a Read of the review video from the workflow (original and alpha above, premultiplied and over green below).

**Size** sets the plate's shorter side while LTX renders, in steps of 32 with **▼ ▲**. The matte is scaled back to the full plate. Larger is sharper and slower; LTX renders at most 1920x1088.

**Isolate margin** is how much of the plate around the subject LTX still sees, so its edges arrive intact. Raise it for hair or motion blur that reaches past the SAM3 mask; lower it when something else sits close to the subject.

## Output

Each run has a numbered folder under **Render folder**, starting at `0001`. **Version Up Output** picks the next one.

| File | Contents |
| --- | --- |
| `EXR/Select.####.exr` | The subject mask from SAM3, at the render size. |
| `EXR/Matte.####.exr` | The LTX matte at the render size, written as raw float values. |
| `Plate.mp4`, `Isolated.mp4` | What was sent to ComfyUI. |
| `ContactSheet.mp4` | The workflow's 2×2 review video. |

The node reads both EXR sequences, scales them back to the plate and combines them live, so nothing is baked into the frames on disk. **Reload Results** reads them again after you reopen a script.

## The workflow

`ltx_matte/alpha_gen_api.json` is the Alpha Gen workflow exported from ComfyUI as an API prompt. LTX Matte sends it as it is and changes only its inputs for each render: the plate, frame count, size, seed, prompts, models and output names. It adds an EXR writer for the matte, and for Find Subject a short job of its own: the workflow's plate loading followed by SAM3. Previews, timers and notifications in the workflow are left out, since nobody sees them when Nuke sends the job.

Lightricks trains Alpha Gen with an empty prompt, so the Positive and Negative prompts in **Settings** rarely change the matte. They are kept because the workflow has them.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| LTX Matte is missing from Nuke | `menu.py` and `LTXMatte.gizmo` must be directly inside `.nuke/LTX-Matte-main`, and the `init.py` line must point at that folder. Restart Nuke. |
| Check Setup lists missing nodes or models | Install the custom nodes and models above in the ComfyUI that the URL points at, and restart ComfyUI. |
| Subject selection is not available | ComfyUI-SAM3 is not installed on that server. Auto and Roto still work. |
| Find Subject picks the wrong object | Use more words (`doll in red hat`), switch to Click and add a **Not this** point, or draw a Box. |
| Queue Matte asks for Find Subject first | The subject, frame range or size changed since the last Find Subject. Run it again. |
| Edges are cut off | Raise **Isolate margin** and **Clean margin**, then queue a new version. |
| Another object still shows in the matte | Lower **Clean margin**, or check View → Subject: the subject mask may include it. |
| A faint grey ring around the subject | Raise **Halo cut** (0.35 by default). |
| Part of the subject is missing in some frames | SAM3 lost it there: check View → Subject frame by frame. Name that part as well (`doll, chainsaw`), or use Box or Click. |
| `WinError 193` or another FFmpeg error | The FFmpeg found does not run on this computer. Install `winget install Gyan.FFmpeg` and run Check Setup again. |

## Tests

```
python -I tests/smoke.py
```

They check the frame and size rules, the matte job and the subject-finding job for every Select mode against ComfyUI's node definitions (saved in `tests/fixtures`), and the gizmo. Nuke, ComfyUI and a GPU are not needed.

## License

MIT © 2026 Omar El Sharkawy. You may use, change and share LTX Matte freely, as long as the copyright notice and the [LICENSE](LICENSE) stay with every copy. The LTX-2.5 models, the Alpha Gen LoRA and SAM3 are installed separately and have their own licenses.
