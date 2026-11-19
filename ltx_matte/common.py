"""Helpers shared by the LTX Matte bridge: render folders and versions, finding FFmpeg and the
ComfyUI folder on this computer, and talking to a ComfyUI server reliably.

Adapted from LTX Layout by the same author.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlparse

import nuke

from . import client

WATCH_POLL_SECONDS = 3     # pause between ComfyUI status checks
WATCH_RETRIES = 5          # transient network errors tolerated in a row per request
WATCH_RETRY_SECONDS = 3    # first pause before retrying; grows by this much each time
STOPPED = object()         # returned by watch_request when Cancel arrives while retrying
FFMPEG_QUICK_SECONDS = 12  # listing encoders/filters or probing a clip
FFMPEG_BASE_SECONDS = 180  # fixed allowance for an encode, plus the per-frame one below
FFMPEG_SECONDS_PER_FRAME = 5
_SYNCING = set()  # nodes whose Place render at is being set by follow_first, not by the user
IS_WINDOWS = os.name == "nt"
FFMPEG_EXE = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
_FOUND = {}
_CHECKED = {}


def new_render_folder(base, version=None):
    base.mkdir(parents=True, exist_ok=True)
    if version is not None:
        folder = version_folder(base, version)
        try:
            folder.mkdir()
        except FileExistsError as error:
            raise ValueError(f"{folder} already exists. Use Version Up Output or choose another version.") from error
        return folder
    number = max((int(item.name) for item in base.iterdir()
                  if item.is_dir() and item.name.isascii() and item.name.isdecimal()), default=0) + 1
    while True:
        path = base / f"{number:04d}"
        try:
            path.mkdir()
            return path
        except FileExistsError:
            number += 1


def version_folder(base, version):
    if version < 1:
        raise ValueError("Output version must be 1 or higher.")
    return base / f"{version:04d}"


@contextmanager
def graph_context(group):
    group.begin()
    try:
        yield
    finally:
        group.end()


def follow_first(node):
    """Keep Place render at on the Plate First frame, until the user gives it a frame of their own."""
    follows = node.knob("start_follows")
    if follows is not None and not follows.value():
        return
    key = node.fullName()
    _SYNCING.add(key)
    try:
        node["start_at"].setValue(int(node["plate_first"].value()))
    finally:
        _SYNCING.discard(key)


def start_edited(node):
    """A Place render at typed by the user stays put; typing the First frame again re-links it."""
    if node.fullName() in _SYNCING:
        return
    follows = node.knob("start_follows")
    if follows is not None:
        follows.setValue(int(node["start_at"].value()) == int(node["plate_first"].value()))


def parse_seed(text):
    text = str(text).strip()
    if not text:
        raise ValueError("Enter a Seed: a whole number such as 42.")
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"Seed must be a whole number such as 42, not '{text[:40]}'.") from None


def valid_host(value):
    host = value.strip().rstrip("/")
    parsed = urlparse(host)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Enter a valid ComfyUI URL, for example http://127.0.0.1:8188.")
    return host


def discard_empty_run(folder):
    """Remove a newly reserved run with no outputs, leaving any other files alone."""
    marker = folder / "ComfyRun.txt"
    try:
        if any(path != marker for path in folder.iterdir()):
            return
        marker.unlink(missing_ok=True)
        folder.rmdir()
    except OSError:
        pass


def comfy_base(value):
    """Return the ComfyUI folder that holds custom_nodes/output for a path, or None.

    Accepts the Portable folder, the ComfyUI folder, its main.py, or the
    ComfyUI Desktop data folder (Documents/ComfyUI).
    """
    if not value:
        return None
    path = Path(str(value).strip().strip('"')).expanduser()
    try:
        if path.is_file():
            path = path.parent
        for candidate in (path / "ComfyUI", path):
            if (candidate / "main.py").is_file() or (
                    (candidate / "custom_nodes").is_dir() and (candidate / "models").is_dir()):
                return candidate.resolve()
    except OSError:
        pass
    return None


def portable_root(override=""):
    """Validate a ComfyUI folder that the user selected by hand."""
    base = comfy_base(override)
    if base is None:
        raise ValueError("Choose the ComfyUI folder that contains main.py, or the Portable folder above it.")
    return base


def comfy_root_from_server(host):
    """Ask a ComfyUI server on this machine where it is installed."""
    key = host.strip().rstrip("/")
    cached = _FOUND.get(("comfy", key))
    if cached is not None and cached.is_dir():
        return cached
    candidates = []
    try:
        paths = client.json_request(key, "/internal/folder_paths", timeout=3) or {}
        for name in ("custom_nodes", "diffusion_models", "checkpoints", "loras"):
            candidates.extend(Path(item) for item in paths.get(name) or () if isinstance(item, str))
    except (OSError, RuntimeError, ValueError, AttributeError, TypeError):
        pass
    try:
        stats = client.json_request(key, "/system_stats", timeout=3) or {}
        argv = stats.get("system", {}).get("argv") or []
        if argv and isinstance(argv[0], str) and Path(argv[0]).is_absolute():
            candidates.append(Path(argv[0]))
    except (OSError, RuntimeError, ValueError, AttributeError, TypeError):
        pass
    for candidate in candidates:
        for folder in (candidate, *list(candidate.parents)[:3]):
            base = comfy_base(folder)
            if base is not None:
                _FOUND[("comfy", key)] = base
                return base
    return None


def find_comfy_root(override="", host=None):
    """Use a saved or environment path when valid, otherwise ask the local server."""
    for value in (override, os.environ.get("LTX_LAYOUT_COMFY_ROOT", "")):
        base = comfy_base(value)
        if base is not None:
            return base
    if host and is_local_server(host):
        return comfy_root_from_server(host)
    return None


def node_host(node):
    knob = node.knob("server")
    return knob.value().strip().rstrip("/") if knob is not None else ""


def node_portable_root(node, host=None):
    knob = node.knob("comfy_root")
    return find_comfy_root(knob.value() if knob is not None else "", host or node_host(node))


def child_dirs(folder):
    try:
        return sorted(item for item in folder.iterdir() if item.is_dir())
    except OSError:
        return []


def fixed_drives():
    if not IS_WINDOWS:
        return []
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        mask = kernel.GetLogicalDrives()
        drives = [f"{chr(65 + index)}:\\" for index in range(26) if mask >> index & 1]
        return [Path(drive) for drive in drives if kernel.GetDriveTypeW(drive) == 3]
    except (AttributeError, OSError):
        return [Path(os.environ.get("SystemDrive", "C:") + "\\")]


def scan_comfy_installs():
    """Yield ComfyUI folders in the usual install places on this machine."""
    home = Path.home()
    places = [home / "Desktop", home / "OneDrive" / "Desktop", home / "Downloads",
              home / "Documents", home, *fixed_drives()]
    skip = {"windows", "program files", "program files (x86)", "programdata", "users",
            "appdata", "$recycle.bin", "system volume information", "node_modules"}
    seen = set()
    for place in places:
        for child in child_dirs(place):
            name = child.name.casefold()
            nested = [] if name in skip or name.startswith(".") else child_dirs(child)
            for folder in ([child] if "comfy" in name else []) + [
                    item for item in nested if "comfy" in item.name.casefold()]:
                base = comfy_base(folder)
                if base is not None and base not in seen:
                    seen.add(base)
                    yield base


def bundled_ffmpeg(base):
    """FFmpeg shipped by imageio-ffmpeg inside a ComfyUI Python environment."""
    package = Path("site-packages/imageio_ffmpeg/binaries")
    folders = [base.parent / name / "Lib" / package for name in ("python_embeded", "python_embedded")]
    for env in (".venv", "venv", "python"):
        folders.append(base / env / "Lib" / package)
        folders.extend((base / env / "lib").glob(f"python3*/{package.as_posix()}"))
    for folder in folders:
        for item in sorted(folder.glob("ffmpeg*"), reverse=True):
            if item.suffix.lower() == ".exe" if IS_WINDOWS else os.access(item, os.X_OK):
                yield item


def registry_path_dirs():
    """PATH as saved in Windows now, which Nuke misses when FFmpeg was installed after login."""
    if not IS_WINDOWS:
        return
    try:
        import winreg
    except ImportError:
        return
    for hive, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value = winreg.QueryValueEx(handle, "Path")[0]
        except OSError:
            continue
        for part in os.path.expandvars(value).split(";"):
            if part.strip():
                yield Path(part.strip().strip('"'))


def known_ffmpeg_dirs():
    if not IS_WINDOWS:
        yield from map(Path, ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/snap/bin"))
        return
    env = os.environ.get
    home = Path.home()
    local = Path(env("LOCALAPPDATA", home / "AppData" / "Local"))
    yield local / "Microsoft/WinGet/Links"
    yield from (local / "Microsoft/WinGet/Packages").glob("*FFmpeg*/*/bin")
    yield Path(env("ProgramData", r"C:\ProgramData")) / "chocolatey/bin"
    yield home / "scoop/shims"
    yield home / "scoop/apps/ffmpeg/current/bin"
    for program_files in {env("ProgramFiles", r"C:\Program Files"), env("ProgramFiles(x86)", "")} - {""}:
        yield Path(program_files) / "ffmpeg/bin"
    for place in (*fixed_drives(), home / "Downloads", home / "Desktop", home / "Documents"):
        for folder in place.glob("ffmpeg*"):
            yield folder / "bin"
            yield from folder.glob("*/bin")


def ffmpeg_candidates(comfy_root=None, host=None):
    """Possible FFmpeg executables, best first. Slow searches run only when needed."""
    on_path = shutil.which("ffmpeg")
    if on_path:
        yield Path(on_path)
    for folder in registry_path_dirs():
        yield folder / FFMPEG_EXE
    for folder in known_ffmpeg_dirs():
        yield folder / FFMPEG_EXE
    roots = [comfy_root] if comfy_root is not None else []
    if host and is_local_server(host):
        roots.append(comfy_root_from_server(host))
    for root in roots:
        if root is not None:
            yield from bundled_ffmpeg(root)
    for root in scan_comfy_installs():
        yield from bundled_ffmpeg(root)
    try:
        import imageio_ffmpeg
        yield Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # Optional module; any failure only means no candidate.
        pass


def is_ffmpeg(path):
    """True when the file runs and reports itself as FFmpeg.

    A file that merely exists can be a stale path, a different program, or a build for another
    system (WinError 193), so each file is run once with -version. The answer is kept until the
    file changes.
    """
    try:
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            return False
        stat = path.stat()
    except OSError:
        return False
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _CHECKED:
        try:
            result = subprocess.run([str(path), "-version"], capture_output=True, text=True, errors="replace",
                                    check=False, timeout=FFMPEG_QUICK_SECONDS,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            _CHECKED[key] = result.stdout.lstrip().lower().startswith("ffmpeg version")
        except (OSError, subprocess.SubprocessError):
            _CHECKED[key] = False
    return _CHECKED[key]


def find_ffmpeg(override="", comfy_root=None, host=None):
    """Return a working FFmpeg: a saved choice when it runs, otherwise search this machine."""
    rejected = []
    for value in (override, os.environ.get("LTX_LAYOUT_FFMPEG", "")):
        value = value.strip().strip('"')
        if not value:
            continue
        if is_ffmpeg(value):
            return str(Path(value).expanduser().resolve())
        rejected.append(value)
    cached = _FOUND.get("ffmpeg")
    if cached is not None and is_ffmpeg(cached):
        return str(cached)
    for candidate in ffmpeg_candidates(comfy_root, host):
        try:
            if candidate.is_file() and is_ffmpeg(candidate):
                _FOUND["ffmpeg"] = candidate.resolve()
                return str(_FOUND["ffmpeg"])
        except OSError:
            continue
    skipped = "".join(f"\nSkipped because it is not a working FFmpeg: {value}" for value in rejected)
    raise ValueError("FFmpeg was not found on this computer. Install FFmpeg "
                     "(for example: winget install Gyan.FFmpeg) or start ComfyUI on this computer, "
                     "then try again." + skipped)


def node_ffmpeg(node, comfy_root=None, host=None):
    """Find FFmpeg for a gizmo, asking for the executable only if the search fails."""
    knob = node.knob("ffmpeg_path")
    saved = knob.value().strip().strip('"') if knob is not None else ""
    if saved and not is_ffmpeg(saved):
        knob.setValue("")  # A broken saved path must not keep winning over a working FFmpeg.
        saved = ""
    try:
        return find_ffmpeg(saved, comfy_root, host)
    except ValueError as error:
        ask = getattr(nuke, "ask", None)
        if knob is None or ask is None or not ask(f"{error}\n\nSelect the FFmpeg executable yourself?"):
            raise
        selected = nuke.getFilename("Select the FFmpeg executable", "*")
        if not selected or not Path(selected).is_file():
            raise
        if not is_ffmpeg(selected):
            raise ValueError(f"{selected} did not run as FFmpeg. Choose ffmpeg.exe from a full "
                             "Windows build, for example one installed with winget install Gyan.FFmpeg.") from error
        knob.setValue(str(Path(selected).resolve()))
        return knob.value()


def show_detected(node, ffmpeg, comfy_root):
    knob = node.knob("detected_paths")
    if knob is not None:
        knob.setValue(f"FFmpeg: {ffmpeg}<br>ComfyUI: {comfy_root or 'not found on this computer (optional)'}")


def ffmpeg_timeout(frames):
    return FFMPEG_BASE_SECONDS + FFMPEG_SECONDS_PER_FRAME * max(int(frames), 1)


def run_ffmpeg(args, timeout, task):
    """Run FFmpeg with a deadline so a hung process cannot keep the gizmo busy forever."""
    try:
        return subprocess.run(args, capture_output=True, text=True, errors="replace", check=False,
                              timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"FFmpeg did not finish within {timeout:g} seconds while trying to {task}, "
                           "so it was stopped. Try again, or select a different FFmpeg.") from None
    except OSError as error:
        raise RuntimeError(f"Could not run FFmpeg to {task}: {error}") from error


def ffmpeg_names(ffmpeg, listing):
    result = run_ffmpeg([ffmpeg, "-hide_banner", listing], FFMPEG_QUICK_SECONDS,
                        f"list its {listing.lstrip('-')}")
    if result.returncode:
        raise RuntimeError(f"FFmpeg {listing} failed. " + result.stderr.strip())
    return {columns[1] for line in (result.stdout + result.stderr).splitlines()
            if len(columns := line.split()) >= 3}


def check_server(host):
    stats = client.json_request(host, "/system_stats", timeout=5)
    if not isinstance(stats, dict) or "system" not in stats:
        raise RuntimeError("That URL did not return ComfyUI system information.")


def combo_choices(spec):
    if isinstance(spec, (list, tuple)) and spec:
        if isinstance(spec[0], list):
            return spec[0]
        if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
            options = spec[1].get("options")
            if isinstance(options, list):
                return options
    return None


def is_local_server(host):
    return urlparse(host).hostname in ("127.0.0.1", "localhost", "::1")


def server_shares_files(host):
    """False when a local-looking ComfyUI server clearly has its own filesystem.

    ComfyUI in WSL or Docker answers on 127.0.0.1, but a Windows path from Nuke
    means nothing there, so it cannot write EXRs straight into the render folder.
    """
    if not is_local_server(host):
        return False
    try:
        server_os = ((client.json_request(host, "/system_stats", timeout=3) or {}).get("system") or {}).get("os")
        if isinstance(server_os, str) and server_os != os.name:
            return False
    except Exception:  # Unknown is treated as shared, as before.
        pass
    try:
        paths = client.json_request(host, "/internal/folder_paths", timeout=3) or {}
        folders = [Path(item) for values in paths.values() if isinstance(values, list)
                   for item in values if isinstance(item, str)]
        if folders and not any(folder.is_dir() for folder in folders):
            return False
    except Exception:
        pass
    return True


def cancel_remote(host, job_id):
    client.json_request(host, "/queue", {"delete": [job_id]})
    client.json_request(host, "/interrupt", {"prompt_id": job_id})
    for _ in range(15):
        state = client.json_request(host, "/queue")
        if not any(item[1] == job_id for key in ("queue_running", "queue_pending")
                   for item in state.get(key, [])):
            return "Request canceled"
        time.sleep(1)
    return "Cancel sent; waiting for ComfyUI to stop"


def discard_staging(staging):
    try:
        staging.cleanup()
    except OSError:
        pass  # A locked temp file must not stop the render or hide its final status.


def watch_request(host, route, stop, notify):
    """GET a status route, retrying transient network errors a few times.

    Returns STOPPED if Cancel arrives while waiting to retry.
    """
    failures = 0
    while True:
        try:
            return client.json_request(host, route)
        except client.TransientError as error:
            failures += 1
            if failures > WATCH_RETRIES:
                raise RuntimeError(f"Lost contact with ComfyUI after {failures} attempts. "
                                   f"The render may still be running there. {error}") from error
            notify(f"Reconnecting to ComfyUI ({failures}/{WATCH_RETRIES})")
            if stop.wait(WATCH_RETRY_SECONDS * failures):
                return STOPPED


def history_prompt(entry):
    """The workflow ComfyUI stored with a finished job, or {}."""
    saved = entry.get("prompt")
    if isinstance(saved, (list, tuple)) and len(saved) > 2 and isinstance(saved[2], dict):
        return saved[2]
    return {}

