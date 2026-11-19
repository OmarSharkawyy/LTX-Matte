import os

import nuke

# The menu entry loads the tool only when it is clicked, so a problem inside the tool shows
# a clear error at that moment instead of hiding the menu when Nuke starts.
COMMAND = "from ltx_matte import bridge; bridge.create()"

menu = nuke.menu("Nodes")
comfy = menu.findItem("ComfyUI")
if comfy is None:
    # The Nodes toolbar shows only icons, so a ComfyUI menu without one would be an invisible button.
    icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ltx_matte", "comfyui_menu.png")
    comfy = menu.addMenu("ComfyUI", icon=icon.replace(os.sep, "/"))

label = "LTX Matte"
if comfy.findItem(label) is None:
    index = -1
    for position, item in enumerate(comfy.items()):
        name = item.name()
        if name.startswith("LTX "):
            if name.casefold() > label.casefold():
                index = position
                break
            index = position + 1
    comfy.addCommand(label, COMMAND, index=index)
