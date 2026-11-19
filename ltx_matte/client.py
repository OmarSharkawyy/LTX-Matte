"""HTTP transport for a running ComfyUI server."""
import http.client
import json
from pathlib import Path
import shutil
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Gateway and rate-limit replies that usually clear up on their own.
TRANSIENT_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})


class TransientError(RuntimeError):
    """A network failure that may succeed if the same request is repeated."""


def request(host, route, data=None, headers=None, timeout=30):
    url = host.rstrip("/") + route
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            details = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        except ValueError:
            details = raw
        failure = TransientError if error.code in TRANSIENT_HTTP_CODES else RuntimeError
        raise failure(f"ComfyUI HTTP {error.code} on {route}\n{details[:2500]}") from error
    except urllib.error.URLError as error:
        raise TransientError(f"Could not reach ComfyUI at {host}: {error.reason}") from error
    except (OSError, http.client.HTTPException) as error:
        # Timeouts and resets raised while connecting or reading the status line.
        raise TransientError(f"Could not reach ComfyUI at {host}: {error}") from error


def json_request(host, route, body=None, *, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else None
    with request(host, route, data, headers, timeout=timeout) as reply:
        try:
            payload = reply.read()
        except (OSError, http.client.HTTPException) as error:
            raise TransientError(f"ComfyUI stopped replying on {route}: {error}") from error
        try:
            return json.loads(payload) if payload.strip() else None
        except ValueError as error:
            raise TransientError(f"ComfyUI sent an unreadable reply on {route}.") from error


def upload(host, path):
    boundary = uuid.uuid4().hex
    filename = uuid.uuid4().hex + path.suffix.lower()
    header = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("ascii")
    trailer = f"\r\n--{boundary}--\r\n".encode("ascii")
    with request(host, "/upload/image", header + path.read_bytes() + trailer,
                 {"Content-Type": f"multipart/form-data; boundary={boundary}"}, 120) as reply:
        result = json.load(reply)
    return "/".join(part for part in (result.get("subfolder"), result["name"]) if part)


def find_output(entry, node_id, suffix):
    for records in entry.get("outputs", {}).get(node_id, {}).values():
        if isinstance(records, list):
            for item in records:
                if (isinstance(item, dict) and item.get("type") == "output"
                        and Path(item.get("filename", "")).suffix.lower() == suffix):
                    return item
    raise RuntimeError(f"ComfyUI completed but did not report the {suffix} output.")


def download(host, item, destination):
    params = {key: item.get(key, "") for key in ("filename", "subfolder", "type")}
    part = destination.with_name(destination.name + ".part")
    with request(host, "/view?" + urllib.parse.urlencode(params), timeout=120) as reply:
        with part.open("wb") as stream:
            shutil.copyfileobj(reply, stream)
    part.replace(destination)
