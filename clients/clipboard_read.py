"""Use the shipped Windows clipboard client for one exact text file."""

import os
from pathlib import Path
import re
import subprocess

from common import BridgeError, atomic_write, decoded, encoded, no_links, private_dir, regular
from etx_cli import text_file_meta, Failure


class NotSent(BridgeError):
    """The clipboard entry proved this attempt ended before sending."""


FALLBACK_ERRORS = {
    "ETX_QUEUE_WAIT_TIMEOUT", "ETX_CHANNEL_COOLDOWN",
    "CLIPBOARD_PAIRING_REQUIRED", "CLIPBOARD_WINDOWS_REQUIRED",
    "CLIPBOARD_READ_FAILED", "CLIPBOARD_OPEN_BUSY", "CLIPBOARD_IO_PROCESS_EXITED",
    "CLIPBOARD_USER_PRIORITY_YIELD", "CLIPBOARD_FORMAT_NOT_RESTORABLE",
    "CLIPBOARD_NOT_PLAIN_TEXT", "CLIPBOARD_SIZE_LIMIT",
}


def result_for(root, request_id, remote):
    directory = root / ".work/x11/session" / request_id
    receipt = directory / "routed-read.json"
    local = directory / "file.txt"
    reply = decoded(regular(receipt).read_bytes())
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        raise BridgeError("CLIPBOARD_RECEIPT_INCOMPLETE")
    calls = reply.get("timings", {}).get("rpc", ())
    if not any(call.get("op") == "read_batch" and call.get("id") == request_id for call in calls):
        raise BridgeError("CLIPBOARD_REQUEST_MISMATCH")
    files = reply.get("result", {}).get("files")
    if not isinstance(files, list) or len(files) != 1:
        raise BridgeError("CLIPBOARD_RECEIPT_INVALID")
    item = files[0]
    if item.get("path") != remote or Path(item.get("local", "")) != local:
        raise BridgeError("CLIPBOARD_SOURCE_MISMATCH")
    try:
        info = text_file_meta(local)
    except Failure as error:
        raise BridgeError("CLIPBOARD_LOCAL_FILE_INVALID") from error
    if (type(item.get("bytes")) is not int or item["bytes"] != info["bytes"]
            or item.get("sha256") != info["sha256"]):
        raise BridgeError("CLIPBOARD_FILE_HASH_MISMATCH")
    return {"local": str(local), "bytes": info["bytes"], "sha256": info["sha256"],
            "receipt": str(receipt)}


def read(root, request_id, spec, queue_seconds):
    if os.name != "nt":
        raise NotSent("CLIPBOARD_WINDOWS_REQUIRED")
    private = root / ".private/x11/session"
    session_config = private / "client.json"
    if not session_config.is_file():
        raise NotSent("CLIPBOARD_PAIRING_REQUIRED")
    regular(session_config, 16384, private=True)
    request = private_dir(private / "requests" / request_id) / "routed-read.json"
    response = no_links(root / ".work/x11/session" / request_id / "routed-read.json")
    manifest = {"id": request_id, "files": [
        {"path": spec["path"], "cached_file": None, "cached_sha256": None, "output_name": "file.txt"},
    ], "timeout_seconds": spec["timeout_seconds"]}
    if request.exists():
        if decoded(regular(request, 16384).read_bytes(), 16384) != manifest:
            raise BridgeError("CLIPBOARD_REQUEST_CONFLICT")
    else:
        atomic_write(request, encoded(manifest))
    if response.exists():
        previous = decoded(regular(response).read_bytes())
        if isinstance(previous, dict) and previous.get("ok") is True:
            return result_for(root, request_id, spec["path"])
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
    wrapper = regular(root / "clients/Invoke-EtxX11.ps1")
    try:
        completed = subprocess.run([
            str(powershell), "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(wrapper),
            "-Mode", "SessionReadFiles", "-SessionConfig", str(session_config),
            "-SessionRequest", str(request), "-SessionOutputName", f"{request_id}/{response.name}",
            "-QueueWaitTimeoutSeconds", str(queue_seconds),
        ], stdin=subprocess.DEVNULL, capture_output=True, timeout=queue_seconds + spec["timeout_seconds"] + 340)
    except subprocess.TimeoutExpired as error:
        raise BridgeError("CLIPBOARD_READ_TIMEOUT_QUERY_SAME_ID") from error
    if completed.returncode:
        for line in completed.stdout.splitlines():
            if line.startswith(b"ETX_QUEUE_ERROR="):
                error = decoded(line.split(b"=", 1)[1], 16384)
                code = error.get("error")
                if (error.get("phase") == "queue" and error.get("not_sent") is True
                        and isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", code)):
                    raise NotSent(code)
        if response.exists():
            error = decoded(regular(response).read_bytes()).get("error")
            if isinstance(error, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", error):
                raise BridgeError(error)
        raise BridgeError("CLIPBOARD_RECEIPT_UNCERTAIN")
    return result_for(root, request_id, spec["path"])
