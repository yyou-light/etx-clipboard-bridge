"""ETX bridge entry for single-file read, route preview, and original receipt recovery."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time
import uuid


INSTALL = Path(__file__).resolve().parent
sys.path.insert(0, str(INSTALL / "clients"))

import clipboard_read
from common import BridgeError, ID_RE, atomic_write, decoded, digest, encoded, no_links, private_dir, regular
from etx_cli import Failure, text_file_meta
import runner_read


MAX_TEXT = 256 * 1024 * 1024
RUNNER_THRESHOLD = 4 * 1024 * 1024
HYBRID_WAIT = 15
DOCUMENTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}


def require(condition, code):
    if not condition:
        raise BridgeError(code)


def load(path):
    return decoded(regular(path, 65536).read_bytes(), 65536)


def remote_file(path):
    require(isinstance(path, str) and path.startswith("/") and
            not re.search(r"[\\\x00-\x1f\x7f]", path) and
            all(part not in ("", ".", "..") for part in path[1:].split("/")),
            "EXACT_ETX_PATH_REQUIRED")
    return path


def configured(site_file, project_file):
    site = load(site_file)
    project = load(project_file)
    require(isinstance(site, dict) and set(site) == {"version", "clipboard", "runner"}
            and type(site["version"]) is int and site["version"] == 1
            and type(site["clipboard"]) is bool, "SITE_CONFIG_INVALID")
    if site["runner"] is not None:
        runner_read.configuration(site["runner"])
    fields = {"version", "id", "state_dir", "read_roots"}
    require(isinstance(project, dict) and fields <= set(project) <= fields | {"read_paths"}
            and type(project["version"]) is int and project["version"] == 1
            and isinstance(project["id"], str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", project["id"])
            and isinstance(project["state_dir"], str) and Path(project["state_dir"]).is_absolute(),
            "PROJECT_CONFIG_INVALID")
    for key in ("read_roots", "read_paths"):
        values = project.get(key, [])
        require(isinstance(values, list), "PROJECT_READ_SCOPE_INVALID")
        for path in values:
            remote_file(path)
    require(project["read_roots"] or project.get("read_paths"), "PROJECT_READ_SCOPE_EMPTY")
    no_links(Path(project["state_dir"]))
    return site, project


def authorize(project, path):
    target = PurePosixPath(remote_file(path))
    require(path in project.get("read_paths", ()) or
            any(PurePosixPath(root) in target.parents for root in project["read_roots"]),
            "PROJECT_READ_SCOPE_REJECTED")
    require(target.suffix.lower() != ".fsdb", "FSDB_REQUIRES_NATIVE_WAVE_QUERY")


def mode(value):
    value = value.casefold()
    if value in {"剪切板", "剪贴板", "clipboard", "x11"}:
        return "clipboard"
    if value in {"runner", "github-runner", "github runner"}:
        return "runner"
    if value in {"hybrid", "offline"}:
        return value
    raise argparse.ArgumentTypeError("expected clipboard, Runner, hybrid, or offline")


def clipboard_cooling():
    try:
        health = load(INSTALL / ".work/x11/transport-queue/channel-health.json")
        return health.get("version") == 1 and health["not_before"] - time.time() >= HYBRID_WAIT
    except (BridgeError, OSError, KeyError, TypeError, AttributeError):
        return False


def plan(site, spec):
    size = spec["expected_bytes"]
    clipboard = site["clipboard"] and spec["kind"] == "text" and (size is None or size <= MAX_TEXT)
    runner = site["runner"] is not None and spec["path"].startswith("/nfs/") and len(spec["path"]) <= 1024
    runner = runner and (size is None or size <= runner_read.LIMIT)
    requested = spec["mode"]
    if requested in ("clipboard", "offline"):
        require(clipboard, "CLIPBOARD_READ_UNAVAILABLE")
        return {"transport": "clipboard", "reason": "explicit_clipboard", "fallback": None}
    if requested == "runner":
        require(runner, "RUNNER_READ_UNAVAILABLE")
        return {"transport": "runner", "reason": "explicit_runner", "fallback": None}
    require(clipboard or runner, "READ_NO_CAPABLE_TRANSPORT")
    if runner:
        if not clipboard:
            return {"transport": "runner", "reason": "runner_only", "fallback": None}
        if size is not None and size >= RUNNER_THRESHOLD:
            return {"transport": "runner", "reason": "large_transfer", "fallback": None}
        if clipboard_cooling():
            return {"transport": "runner", "reason": "clipboard_cooldown", "fallback": None}
    return {"transport": "clipboard", "reason": "small_text", "fallback": "runner" if runner else None}


def data_info(local, kind, size):
    path = regular(local, MAX_TEXT if kind == "text" else runner_read.LIMIT)
    require(type(size) is int and path.stat().st_size == size, "LOCAL_READ_SIZE_MISMATCH")
    if kind == "text":
        try:
            record = text_file_meta(path)
        except Failure as error:
            raise BridgeError("LOCAL_TEXT_FILE_INVALID") from error
        require(record["bytes"] == size, "LOCAL_READ_SIZE_MISMATCH")
        return {"bytes": size, "sha256": record["sha256"]}
    before = path.stat()
    checksum = runner_read.sha256_file(path)
    after = path.stat()
    require((before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_ino, after.st_size, after.st_mtime_ns), "LOCAL_READ_CHANGED")
    return {"bytes": size, "sha256": checksum}


@contextmanager
def claim(journal):
    with no_links(journal.with_suffix(".lock")).open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise BridgeError("READ_REQUEST_ALREADY_ACTIVE") from error
        yield


def completed_result(record, directory, request_id):
    spec = record["spec"]
    result = record["result"]
    require(isinstance(result, dict) and result.get("transport") == record["transport"]
            and result.get("request_id") == request_id and result.get("source") == spec["path"],
            "READ_RESULT_IDENTITY_MISMATCH")
    if record["transport"] == "clipboard":
        native = clipboard_read.result_for(INSTALL, request_id, spec["path"])
        require(all(result.get(key) == value for key, value in native.items()), "READ_RESULT_HASH_MISMATCH")
    else:
        native = load(directory / "runner-receipt.json")
        require(isinstance(native, dict) and native.get("request_id") == request_id
                and native.get("run_id") == record.get("run_id") and native.get("source") == spec["path"]
                and native.get("local") == str(directory / "file")
                and result.get("receipt") == str(directory / "runner-receipt.json")
                and all(result.get(key) == value for key, value in native.items()), "RUNNER_RECEIPT_MISMATCH")
        info = data_info(directory / "file", spec["kind"], result["bytes"])
        require(info["sha256"] == result["sha256"], "READ_RESULT_HASH_MISMATCH")
    return result


def execute(site, project, request_id, spec=None):
    require(ID_RE.fullmatch(request_id), "READ_REQUEST_ID_INVALID")
    directory = private_dir(Path(project["state_dir"]) / project["id"] / request_id)
    journal = directory / "request.json"
    site_hash = digest(json.dumps(site, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    with claim(journal):
        if spec is not None:
            require(not journal.exists(), "READ_REQUEST_EXISTS_USE_RESUME")
            authorize(project, spec["path"])
            route = plan(site, spec)
            record = {"request_id": request_id, "project": project["id"], "site_sha256": site_hash,
                      "spec": spec, "plan": route, "transport": route["transport"], "state": "ready"}
            atomic_write(journal, encoded(record))
        else:
            record = load(journal)
            require(record["request_id"] == request_id and record["project"] == project["id"]
                    and record["site_sha256"] == site_hash, "READ_CONTEXT_CHANGED")
            require(record["transport"] in ("clipboard", "runner")
                    and record["state"] in ("ready", "not_sent", "uncertain", "complete"),
                    "READ_JOURNAL_INVALID")
            spec = record["spec"]
            authorize(project, spec["path"])
        if record["state"] == "complete":
            return completed_result(record, directory, request_id)

        def remember(run_id):
            require(type(run_id) is int and run_id > 0 and record.get("run_id", run_id) == run_id,
                    "RUNNER_ORIGINAL_RUN_MISMATCH")
            record["run_id"] = run_id
            atomic_write(journal, encoded(record))

        while True:
            transport = record["transport"]
            uncertain = record["state"] == "uncertain"
            fallback = transport == "clipboard" and record["plan"]["fallback"] == "runner"
            record["state"] = "uncertain"
            atomic_write(journal, encoded(record))
            try:
                if transport == "clipboard":
                    result = clipboard_read.read(INSTALL, request_id, spec, HYBRID_WAIT if fallback else 600)
                else:
                    result = runner_read.RunnerRead(site["runner"]).read(
                        request_id, spec["path"], directory, uncertain, record.get("run_id"), remember,
                        lambda path, size: data_info(path, spec["kind"], size))
            except clipboard_read.NotSent as error:
                if not uncertain:
                    record["state"] = "not_sent"
                    if fallback and str(error) in clipboard_read.FALLBACK_ERRORS:
                        record.update(state="ready", transport="runner", fallback_from=str(error))
                        atomic_write(journal, encoded(record))
                        continue
                atomic_write(journal, encoded(record))
                raise
            result.update(ok=True, request_id=request_id, transport=transport, source=spec["path"],
                          journal=str(journal), fallback_from=record.get("fallback_from"))
            record.update(state="complete", result=result)
            atomic_write(journal, encoded(record))
            return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--site", required=True, type=Path)
    parser.add_argument("--project", required=True, type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "read"):
        command = actions.add_parser(action)
        command.add_argument("path")
        command.add_argument("--transport", default="hybrid", type=mode)
        command.add_argument("--kind", choices=("text", "binary"))
        command.add_argument("--expected-bytes", type=int)
        command.add_argument("--timeout-seconds", type=int, default=900)
        if action == "read":
            command.add_argument("--request-id")
    actions.add_parser("resume").add_argument("request_id")
    args = parser.parse_args(argv)
    request_id = getattr(args, "request_id", None)
    try:
        site, project = configured(args.site, args.project)
        if args.action == "resume":
            result = execute(site, project, request_id)
        else:
            authorize(project, args.path)
            require(args.expected_bytes is None or args.expected_bytes >= 0, "READ_SIZE_INVALID")
            require(1 <= args.timeout_seconds <= 3600, "READ_TIMEOUT_INVALID")
            document = PurePosixPath(args.path).suffix.lower() in DOCUMENTS
            spec = {"path": args.path, "mode": args.transport, "expected_bytes": args.expected_bytes,
                    "kind": args.kind or ("binary" if document else "text"),
                    "timeout_seconds": args.timeout_seconds}
            if args.action == "plan":
                result = {"ok": True, **plan(site, spec)}
            else:
                request_id = request_id or uuid.uuid4().hex
                result = execute(site, project, request_id, spec)
    except (BridgeError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        code = str(error) if isinstance(error, BridgeError) else type(error).__name__
        print(json.dumps({"ok": False, "request_id": request_id, "error": code}, ensure_ascii=True))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
