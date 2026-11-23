import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("public_etx_bridge", ROOT / "etx_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
from common import BridgeError, atomic_write, encoded
import clipboard_read
from runner_read import RunnerRead, unpack_file


class ReadBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.request = "0123456789abcdef0123456789abcdef"
        self.remote = "/nfs/my-project/synthetic.txt"
        self.project = {"version": 1, "id": "example", "state_dir": str(self.root / "results"),
                        "read_roots": ["/nfs/my-project"]}
        self.runner = {"repository": "test-owner/test-runner", "ref": "testing",
                       "workflow": "secure-file.yml", "recipient_cert": str(self.root / "cert.pem"),
                       "recipient_key": str(self.root / "key.pem")}
        self.site = {"version": 1, "clipboard": True, "runner": self.runner}
        self.read_spec = {"path": self.remote, "mode": "hybrid", "kind": "text",
                          "expected_bytes": None, "timeout_seconds": 10}

    def test_site_values_are_parsed_independently(self):
        atomic_write(self.root / "site.json", encoded(self.site))
        atomic_write(self.root / "project.json", encoded(self.project))
        site, project = bridge.configured(self.root / "site.json", self.root / "project.json")
        self.assertEqual(bridge.plan(site, dict(self.read_spec, mode="runner"))["transport"], "runner")
        self.assertEqual(RunnerRead(site["runner"]).ref, "testing")
        self.assertEqual(project["id"], "example")

    def test_project_scope_is_checked_before_both_transports(self):
        for mode in ("clipboard", "runner"):
            with self.subTest(mode=mode), patch.object(clipboard_read, "read") as x11, \
                    patch.object(RunnerRead, "read") as runner:
                with self.assertRaisesRegex(BridgeError, "PROJECT_READ_SCOPE_REJECTED"):
                    bridge.execute(self.site, self.project, self.request,
                                   dict(self.read_spec, path="/nfs/my-project-other/file.txt", mode=mode))
                x11.assert_not_called()
                runner.assert_not_called()

    def test_runner_resume_uses_saved_run_and_original_request(self):
        attempt = []
        def read(_runner, rid, remote, directory, resume, run_id, remember, verify):
            attempt.append((rid, resume, run_id))
            if not resume:
                remember(42)
                raise BridgeError("RUNNER_READ_WAIT_TIMEOUT")
            source = directory / "file"
            source.write_bytes(b"synthetic\n")
            result = dict(verify(source, 10), local=str(source), source=remote,
                          request_id=rid, run_id=run_id, receipt=str(directory / "runner-receipt.json"))
            atomic_write(directory / "runner-receipt.json", encoded(result))
            return result
        with patch.object(RunnerRead, "read", read):
            with self.assertRaisesRegex(BridgeError, "WAIT_TIMEOUT"):
                bridge.execute(self.site, self.project, self.request, dict(self.read_spec, mode="runner"))
            result = bridge.execute(self.site, self.project, self.request)
            self.assertEqual(result, bridge.execute(self.site, self.project, self.request))
            self.assertEqual(attempt, [(self.request, False, None), (self.request, True, 42)])
            with self.assertRaisesRegex(BridgeError, "READ_CONTEXT_CHANGED"):
                bridge.execute(dict(self.site, runner=dict(self.runner, ref="new")), self.project, self.request)

    def test_unknown_clipboard_send_keeps_original_channel(self):
        with patch.object(bridge, "clipboard_cooling", return_value=False), \
                patch.object(clipboard_read, "read", side_effect=[
                    BridgeError("CLIPBOARD_RECEIPT_UNCERTAIN"),
                    clipboard_read.NotSent("ETX_QUEUE_WAIT_TIMEOUT"),
                ]) as clipboard, patch.object(RunnerRead, "read") as runner:
            with self.assertRaisesRegex(BridgeError, "UNCERTAIN"):
                bridge.execute(self.site, self.project, self.request, self.read_spec)
            with self.assertRaisesRegex(BridgeError, "WAIT_TIMEOUT"):
                bridge.execute(self.site, self.project, self.request)
            self.assertEqual([call.args[1] for call in clipboard.call_args_list], [self.request, self.request])
            runner.assert_not_called()

    def test_confirmed_unsent_attempt_can_fall_back_in_hybrid_mode(self):
        with patch.object(bridge, "clipboard_cooling", return_value=False), \
                patch.object(clipboard_read, "read", side_effect=clipboard_read.NotSent("ETX_QUEUE_WAIT_TIMEOUT")), \
                patch.object(RunnerRead, "read", return_value={"bytes": 4}) as runner:
            result = bridge.execute(self.site, self.project, self.request, self.read_spec)
            self.assertEqual((result["request_id"], result["transport"], result["fallback_from"]),
                             (self.request, "runner", "ETX_QUEUE_WAIT_TIMEOUT"))
            self.assertEqual(runner.call_args.args[0], self.request)

    def test_archive_binds_the_one_expected_source(self):
        archive = self.root / "payload.tar.gz"
        payload = b"test"
        index = encoded({"files": [{"path": "synthetic.txt", "bytes": len(payload)}],
                         "total_bytes": len(payload)})
        with tarfile.open(archive, "w:gz") as bundle:
            for name, data in (("./source/synthetic.txt", payload), ("./source-manifest.json", index)):
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                bundle.addfile(entry, io.BytesIO(data))
        with self.assertRaisesRegex(BridgeError, "UNEXPECTED_ENTRY"):
            unpack_file(archive, "/nfs/another/source.txt", self.root)
        file, size = unpack_file(archive, self.remote, self.root)
        self.assertEqual((file.read_bytes(), size), (payload, len(payload)))


if __name__ == "__main__":
    unittest.main()
