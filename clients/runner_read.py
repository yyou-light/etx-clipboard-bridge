"""Read one file through a configured ETX Secure Read workflow."""

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import time
from urllib.parse import urlencode

from common import BridgeError, atomic_write, decoded, encoded, no_links, private_dir, regular, run


LIMIT = 64 * 1024 * 1024
CIPHER_LIMIT = 120 * 1024 * 1024
ARCHIVE_LIMIT = 110 * 1024 * 1024
WORKFLOW_TITLE = "ETX secure read (user_requested_file)"


def require(condition, code):
    if not condition:
        raise BridgeError(code)


def configuration(value):
    fields = {"repository", "ref", "workflow", "recipient_cert", "recipient_key"}
    require(isinstance(value, dict) and fields <= set(value) <= fields | {"gh", "openssl"},
            "RUNNER_CONFIG_FIELDS")
    require(all(isinstance(item, str) and item for item in value.values()), "RUNNER_CONFIG_VALUE")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", value["repository"]) is not None,
            "RUNNER_REPOSITORY_INVALID")
    require(value["repository"].split("/")[-1] not in (".", ".."), "RUNNER_REPOSITORY_INVALID")
    ref = value["ref"]
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", ref) is not None
            and ".." not in ref and "//" not in ref and not ref.endswith("/"), "RUNNER_REF_INVALID")
    require(re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.ya?ml", value["workflow"]) is not None,
            "RUNNER_WORKFLOW_INVALID")
    for field in ("recipient_cert", "recipient_key"):
        require(Path(value[field]).is_absolute(), "RUNNER_CREDENTIAL_PATH_INVALID")
    return value


def sha256_file(path):
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(256 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def unpack_file(archive, remote_path, work):
    """Extract the one expected regular file after checking the bundle manifest."""
    expected = PurePosixPath(remote_path).name
    payload = work / "file.part"
    manifest = None
    size = None
    seen = set()
    try:
        with tarfile.open(archive, "r|gz") as bundle:
            for entry in bundle:
                name = entry.name.removeprefix("./").rstrip("/")
                require(name not in seen, "RUNNER_BUNDLE_DUPLICATE_ENTRY")
                seen.add(name)
                if name in (".", "source", "") and entry.isdir():
                    continue
                require(entry.isfile(), "RUNNER_BUNDLE_ENTRY_UNSAFE")
                if name == "source-manifest.json":
                    require(0 <= entry.size <= 16384, "RUNNER_MANIFEST_SIZE")
                    with bundle.extractfile(entry) as source:
                        manifest = decoded(source.read(), 16384)
                elif name == "source/" + expected:
                    require(0 <= entry.size <= LIMIT, "RUNNER_FILE_TOO_LARGE")
                    with bundle.extractfile(entry) as source, payload.open("xb") as output:
                        shutil.copyfileobj(source, output, 256 * 1024)
                    size = entry.size
                else:
                    raise BridgeError("RUNNER_BUNDLE_UNEXPECTED_ENTRY")
    except (tarfile.TarError, EOFError) as error:
        raise BridgeError("RUNNER_BUNDLE_INVALID") from error
    require(isinstance(manifest, dict) and size is not None and payload.stat().st_size == size,
            "RUNNER_BUNDLE_INCOMPLETE")
    files = manifest.get("files")
    require(isinstance(files, list) and len(files) == 1 and isinstance(files[0], dict)
            and files[0].get("path") == expected and type(files[0].get("bytes")) is int
            and files[0]["bytes"] == size and manifest.get("total_bytes") == size,
            "RUNNER_MANIFEST_MISMATCH")
    return payload, size


class RunnerRead:
    def __init__(self, route):
        self.route = route
        self.repository = route["repository"]
        self.ref = route["ref"]
        self.workflow = route["workflow"]
        self.gh = route.get("gh", "gh")

    def api(self, path):
        return decoded(run([self.gh, "api", "--method", "GET", f"repos/{self.repository}/{path}"],
                           b"", timeout=30))

    def original_run(self, request_id, run_id, workflow_id, remember):
        title = f"{WORKFLOW_TITLE} [{request_id}]"
        query = urlencode({"event": "workflow_dispatch", "branch": self.ref, "per_page": 100})
        lookup = time.monotonic() + 30
        while run_id is None:
            runs = self.api(f"actions/workflows/{workflow_id}/runs?{query}")["workflow_runs"]
            matches = [item["id"] for item in runs if item.get("display_title") == title]
            require(len(matches) <= 1, "RUNNER_REQUEST_AMBIGUOUS")
            if matches:
                run_id = matches[0]
                remember(run_id)
                break
            require(time.monotonic() < lookup, "RUNNER_ORIGINAL_RUN_NOT_DISCOVERED")
            time.sleep(2)

        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            try:
                result = self.api(f"actions/runs/{run_id}")
            except BridgeError:
                # A dispatch can return an ID before the Run is visible.
                if time.monotonic() >= lookup:
                    raise BridgeError("RUNNER_ORIGINAL_RUN_UNAVAILABLE") from None
            else:
                if result.get("display_title") == title:
                    require(result.get("id") == run_id and result.get("event") == "workflow_dispatch"
                            and result.get("workflow_id") == workflow_id
                            and result.get("head_branch") == self.ref,
                            "RUNNER_ORIGINAL_RUN_MISMATCH")
                    if result.get("status") == "completed":
                        require(result.get("conclusion") == "success", "RUNNER_READ_REMOTE_FAILED")
                        return run_id
                else:
                    require(result.get("status") != "completed" and time.monotonic() < lookup,
                            "RUNNER_ORIGINAL_RUN_MISMATCH")
            time.sleep(2)
        raise BridgeError("RUNNER_READ_WAIT_TIMEOUT")

    def ciphertext_digest(self, run_id):
        deadline = time.monotonic() + 25
        pattern = rb"\bCIPHERTEXT_SHA256=([0-9a-f]{64})[ \t\r]*$"
        while True:
            try:
                output = run([self.gh, "run", "view", run_id, "--repo", self.repository, "--log"], b"",
                             timeout=40)
                values = set(re.findall(pattern, output, re.M))
            except BridgeError:
                values = set()
            require(len(values) <= 1, "RUNNER_CIPHERTEXT_HASH_AMBIGUOUS")
            if values:
                return values.pop().decode("ascii")
            require(time.monotonic() < deadline, "RUNNER_CIPHERTEXT_HASH_UNAVAILABLE")
            time.sleep(2)

    def read(self, request_id, remote_path, directory, resume, run_id, remember, verify_output):
        recipient = regular(self.route["recipient_cert"], 65536)
        key = regular(self.route["recipient_key"], 65536, private=True)
        workflow = self.api(f"actions/workflows/{self.workflow}")
        workflow_id = workflow.get("id")
        require(type(workflow_id) is int and workflow_id > 0
                and workflow.get("path") == ".github/workflows/" + self.workflow,
                "RUNNER_WORKFLOW_MISMATCH")
        if not resume:
            dispatch = run([self.gh, "workflow", "run", self.workflow, "--repo", self.repository,
                            "--ref", self.ref, "-f", f"request_id={request_id}",
                            "-f", "resource_type=user_requested_file", "-f", f"resource_id={remote_path}",
                            "-f", "include_assets=false", "-f", "explicit_user_request=true"], b"", timeout=40)
            prefix = re.escape(f"https://github.com/{self.repository}/actions/runs/".encode("ascii"))
            match = re.search(prefix + rb"([0-9]+)\b", dispatch, re.I)
            if match:
                run_id = int(match[1])
                remember(run_id)
        run_id = self.original_run(request_id, run_id, workflow_id, remember)
        directory = private_dir(directory)
        native = directory / "runner-receipt.json"
        with tempfile.TemporaryDirectory(prefix="runner-asset-", dir=directory) as name:
            work = Path(name)
            asset = f"etx-secure-read-{run_id}"
            run([self.gh, "run", "download", run_id, "--repo", self.repository, "--name", asset, "--dir", work],
                b"", timeout=120)
            cipher = regular(work / f"etx-secure-{run_id}.p7m", CIPHER_LIMIT)
            require(list(work.iterdir()) == [cipher], "RUNNER_ARTIFACT_UNEXPECTED_FILES")
            cipher_hash = self.ciphertext_digest(run_id)
            require(sha256_file(cipher) == cipher_hash, "RUNNER_CIPHERTEXT_HASH_MISMATCH")
            archive = work / "payload.tar.gz"
            run([self.route.get("openssl", "openssl"), "cms", "-decrypt", "-binary", "-inform", "DER",
                 "-recip", recipient, "-inkey", key, "-in", cipher, "-out", archive], b"", timeout=60)
            regular(archive, ARCHIVE_LIMIT)
            payload, size = unpack_file(archive, remote_path, work)
            output = no_links(directory / "file")
            result = verify_output(payload, size)
            os.replace(payload, output)
            result.update(local=str(output), source=remote_path, request_id=request_id,
                          run_id=run_id, run_url=f"https://github.com/{self.repository}/actions/runs/{run_id}",
                          repository=self.repository, workflow=self.workflow, workflow_id=workflow_id,
                          cipher_sha256=cipher_hash, receipt=str(native))
            atomic_write(native, encoded(result))
            return result
