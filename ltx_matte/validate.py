"""Check a prompt against a ComfyUI server's /object_info without running it.

Every node type must exist, every required input must be given, every link must point at an
existing output of a matching type, and every list choice (models, modes) must be one the server offers.
"""


def _choices(spec):
    if isinstance(spec, (list, tuple)) and spec:
        if isinstance(spec[0], list):
            return spec[0]
        if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
            return spec[1].get("options")
    return None


def _type(spec):
    if isinstance(spec, (list, tuple)) and spec and isinstance(spec[0], str):
        return spec[0]
    return None


def problems(prompt, catalog, check_choices=True):
    """A list of readable problems; empty when the prompt fits the server."""
    found = []
    for node_id, step in prompt.items():
        kind = step.get("class_type")
        info = catalog.get(kind)
        if info is None:
            found.append(f"{node_id}: node type {kind} is not installed")
            continue
        declared = {**info.get("input", {}).get("optional", {}), **info.get("input", {}).get("required", {})}
        for name in info.get("input", {}).get("required", {}):
            if name not in step["inputs"]:
                found.append(f"{node_id} {kind}: required input '{name}' is missing")
        for name, value in step["inputs"].items():
            spec = declared.get(name)
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                source = prompt.get(value[0])
                if source is None:
                    found.append(f"{node_id} {kind}.{name}: links to missing node {value[0]}")
                    continue
                outputs = catalog.get(source["class_type"], {}).get("output", [])
                if not isinstance(value[1], int) or value[1] >= len(outputs):
                    found.append(f"{node_id} {kind}.{name}: output {value[1]} of {source['class_type']} does not exist")
                    continue
                wanted, given = _type(spec), outputs[value[1]]
                generic = ("*", "COMBO", "COMFY_MATCHTYPE_V3")  # template types accept whatever they are given
                if wanted and given and wanted not in generic and given not in generic \
                        and given not in wanted.split(",") and wanted not in given.split(","):  # noqa
                    found.append(f"{node_id} {kind}.{name}: expects {wanted}, gets {given} from {source['class_type']}")
            elif check_choices and spec is not None:
                options = _choices(spec)
                if options is not None and value not in options:
                    found.append(f"{node_id} {kind}.{name}: '{value}' is not one of the server's choices")
    return found
