"""Read-only side of the ETX Secure Read workflow on a Linux Runner."""

import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile


MAX_FILE = 64 * 1024 * 1024
DOCUMENTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
TEXT = {".txt", ".md", ".log", ".sv", ".svh", ".v", ".vh", ".c", ".h", ".cc", ".cpp",
        ".hpp", ".py", ".sh", ".tcl", ".f", ".cfg", ".ini", ".json", ".yaml", ".yml",
        ".xml", ".csv", ".html", ".sdc", ".mk"}
TEXT_NAMES = {"BUILD", "Makefile", "README"}


class Rejected(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Rejected(code)


def exact_path(value):
    require(isinstance(value, str) and value.startswith("/nfs/") and len(value) <= 1024
            and not re.search(r"[\\\x00-\x1f\x7f]", value)
            and all(part not in ("", ".", "..") for part in value[1:].split("/")),
            "READ_REQUIRES_EXACT_NFS_PATH")
    return PurePosixPath(value)


def authorized_path(config, value):
    target = exact_path(value)
    require(target in config["read_paths"] or any(root in target.parents for root in config["read_roots"]),
            "READ_PATH_NOT_AUTHORIZED")
    require(target.suffix.lower() in DOCUMENTS | TEXT or target.name in TEXT_NAMES, "READ_TYPE_NOT_SUPPORTED")
    return target


@contextmanager
def directory(path):
    root = Path(path)
    require(root.is_absolute() and ".." not in root.parts, "BAD_ABSOLUTE_PATH")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in root.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def snapshot(path, limit, private=False):
    path = Path(path)
    with directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= limit,
                    "READ_FILE_TYPE_OR_SIZE")
            if private:
                require(before.st_uid == os.geteuid() and stat.S_IMODE(before.st_mode) == 0o600,
                        "READ_CONFIG_PERMISSIONS")
            data = source.read(limit + 1)
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            require(len(data) == before.st_size and identity(before) == identity(os.fstat(source.fileno()))
                    and identity(before) == identity(current), "READ_SOURCE_CHANGED")
    return data


def config_from(path):
    config = json.loads(snapshot(path, 65536, private=True))
    fields = {"version", "read_roots", "read_paths", "lock_root", "recipient_cert"}
    require(isinstance(config, dict) and fields <= set(config) <= fields | {"openssl"}
            and type(config["version"]) is int and config["version"] == 1, "READ_CONFIG_FIELDS")
    for field in ("read_roots", "read_paths"):
        require(isinstance(config[field], list), "READ_CONFIG_SCOPE")
        config[field] = [exact_path(path) for path in config[field]]
    require(config["read_roots"] or config["read_paths"], "READ_CONFIG_SCOPE_EMPTY")
    for field in ("lock_root", "recipient_cert"):
        require(isinstance(config[field], str) and Path(config[field]).is_absolute()
                and ".." not in Path(config[field]).parts, "READ_CONFIG_PATH")
    openssl = config.get("openssl", "openssl")
    require(isinstance(openssl, str) and openssl, "READ_CONFIG_OPENSSL")
    return config


@contextmanager
def shared_source_access(lock_root):
    import fcntl

    with directory(lock_root) as root, ExitStack() as locked:
        info = os.fstat(root)
        require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
                "READ_LOCK_DIRECTORY_PERMISSIONS")
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK

        def lock(name, mode):
            fd = os.open(name, flags, 0o600, dir_fd=root)
            locked.callback(os.close, fd)
            value = os.fstat(fd)
            require(stat.S_ISREG(value.st_mode) and value.st_uid == os.geteuid()
                    and value.st_nlink == 1 and stat.S_IMODE(value.st_mode) == 0o600, "READ_LOCK_REJECTED")
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return fd

        turnstile = lock("turnstile.lock", fcntl.LOCK_EX)
        lock("resource.lock", fcntl.LOCK_SH)
        fcntl.flock(turnstile, fcntl.LOCK_UN)
        yield


def validate_content(source, data):
    if source.suffix.lower() not in DOCUMENTS:
        require(b"\0" not in data, "READ_EXPECTS_UTF8")
        try:
            data.decode("utf-8")
        except UnicodeError as error:
            raise Rejected("READ_EXPECTS_UTF8") from error


def encrypted_bundle(data, name, work, recipient, openssl, run_id):
    cert = work / "recipient.pem"
    cert.write_bytes(recipient)
    archive = work / "payload.tar.gz"
    index = json.dumps({"files": [{"path": name, "bytes": len(data)}], "total_bytes": len(data)}).encode("utf-8")
    with tarfile.open(archive, "w:gz") as bundle:
        for path, content in (("source/" + name, data), ("source-manifest.json", index)):
            entry = tarfile.TarInfo(path)
            entry.size = len(content)
            entry.mode = 0o600
            bundle.addfile(entry, io.BytesIO(content))
    cipher = work / f"etx-secure-{run_id}.p7m"
    result = subprocess.run([openssl, "cms", "-encrypt", "-binary", "-aes256", "-outform", "DER",
                             "-in", str(archive), "-out", str(cipher), str(cert)],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    require(result.returncode == 0 and cipher.is_file() and 0 < cipher.stat().st_size <= 120 * 1024 * 1024,
            "READ_ENCRYPTION_FAILED")
    return cipher


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    env = os.environ
    require(re.fullmatch(r"[0-9a-f]{32}", env.get("ETX_READ_REQUEST_ID", "")), "READ_BAD_REQUEST_ID")
    require(env.get("ETX_READ_RESOURCE_TYPE") == "user_requested_file"
            and env.get("ETX_READ_INCLUDE_ASSETS", "").lower() == "false"
            and env.get("ETX_READ_AUTHORIZED", "").lower() == "true", "READ_OPERATION_NOT_AUTHORIZED")
    run_id = env.get("GITHUB_RUN_ID", "")
    require(re.fullmatch(r"[1-9][0-9]{0,19}", run_id), "READ_BAD_RUN_ID")
    config = config_from(args.config)
    source = authorized_path(config, env.get("ETX_READ_RESOURCE_ID"))
    with shared_source_access(config["lock_root"]):
        data = snapshot(source, MAX_FILE)
    validate_content(source, data)
    recipient = snapshot(config["recipient_cert"], 65536)
    temporary = Path(env["RUNNER_TEMP"])
    with directory(temporary) as fd:
        require(os.fstat(fd).st_uid == os.geteuid(), "READ_RUNNER_TEMP_OWNER")
    output_file = Path(env["GITHUB_OUTPUT"])
    with tempfile.TemporaryDirectory(prefix="etx-read-", dir=temporary) as name:
        cipher = encrypted_bundle(data, source.name, Path(name), recipient, config.get("openssl", "openssl"), run_id)
        ciphertext_sha = hashlib.sha256(cipher.read_bytes()).hexdigest()
        destination = Path(tempfile.mkdtemp(prefix="etx-read-artifact-", dir=temporary)) / cipher.name
        os.replace(cipher, destination)
    with directory(output_file.parent) as parent:
        fd = os.open(output_file.name, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, "a", encoding="utf-8") as output:
            output.write(f"ciphertext={destination}\n")
    print("CIPHERTEXT_SHA256=" + ciphertext_sha)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except BlockingIOError:
        print("SOURCE_RESOURCE_BUSY", file=sys.stderr)
        raise SystemExit(1)
    except (Rejected, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, Rejected) else type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
