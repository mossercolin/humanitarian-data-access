"""Acquisition pair publication: local synthetic responses only."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hdx_acquire_inspect as hdx


class AcquisitionTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data.csv"
        self.sidecar = self.root / "data.csv.acquisition.json"
        self.new = b"name,value\nnew,42\n"
        self.old = {self.data: b"old data\x00\xff", self.sidecar: b"old metadata\r\n"}
        network = patch.object(hdx, "open_http", side_effect=self.response)
        self.network = network.start()
        self.addCleanup(network.stop)

    def response(self, *args, **kwargs):
        response = io.BytesIO(self.new)
        response.headers = {"Content-Length": str(len(self.new))}
        response.geturl = lambda: "https://example.invalid/final"
        return response

    def acquire(self, force=True):
        return hdx.acquire("https://example.invalid/data", "dataset", "resource",
                           self.data, timeout=1, force=force)

    def seed(self, members):
        for path in members:
            path.write_bytes(self.old[path])

    def assert_original(self, members):
        self.assertEqual(set(self.root.iterdir()), set(members))
        for path in members:
            self.assertEqual(path.read_bytes(), self.old[path])

    def fail_publication(self, members, target):
        self.seed(members)
        replace = os.replace
        failed = False

        def injected(source, destination):
            nonlocal failed
            source, destination = Path(source), Path(destination)
            if destination == target and ".backup." not in source.name:
                failed = True
                if target == self.sidecar:
                    self.assertEqual(self.data.read_bytes(), self.new)
                raise OSError("synthetic publication failure")
            return replace(source, destination)

        with patch.object(hdx.os, "replace", side_effect=injected):
            with self.assertRaisesRegex(OSError, "synthetic publication failure"):
                self.acquire()
        self.assertTrue(failed)
        self.assert_original(members)

    def test_pair_restored_when_new_metadata_publication_fails(self):
        self.fail_publication((self.data, self.sidecar), self.sidecar)

    def test_data_only_restored_when_new_metadata_publication_fails(self):
        self.fail_publication((self.data,), self.sidecar)

    def test_sidecar_only_restored_when_new_metadata_publication_fails(self):
        self.fail_publication((self.sidecar,), self.sidecar)

    def test_new_pair_removed_when_metadata_publication_fails(self):
        self.fail_publication((), self.sidecar)

    def test_first_publication_failure_restores_each_prior_state(self):
        for members in ((self.data, self.sidecar), (self.data,), (self.sidecar,), ()):
            with self.subTest(members=members):
                self.fail_publication(members, self.data)
                for path in members:
                    path.unlink()

    def test_force_success_replaces_each_prior_state_with_matching_pair(self):
        for members in ((self.data, self.sidecar), (self.data,), (self.sidecar,), ()):
            with self.subTest(members=members):
                self.seed(members)
                result = self.acquire()
                metadata = json.loads(self.sidecar.read_text())
                self.assertEqual(self.data.read_bytes(), self.new)
                self.assertEqual(metadata["sha256"], hashlib.sha256(self.new).hexdigest())
                self.assertEqual(metadata["size_bytes"], len(self.new))
                self.assertEqual(metadata, {key: value for key, value in result.items()
                                            if key not in ("mode", "durable", "metadata_path",
                                                           "superseded_backups_retained")})
                self.assertEqual(set(self.root.iterdir()), {self.data, self.sidecar})
                self.data.unlink()
                self.sidecar.unlink()

    def test_nonforce_refuses_either_existing_member_before_network_or_publication(self):
        for members in ((self.data, self.sidecar), (self.data,), (self.sidecar,)):
            with self.subTest(members=members):
                self.seed(members)
                with patch.object(hdx.os, "replace") as replace:
                    with self.assertRaises(FileExistsError):
                        self.acquire(force=False)
                replace.assert_not_called()
                self.network.assert_not_called()
                self.assert_original(members)
                for path in members:
                    path.unlink()

    def test_metadata_staging_failure_leaves_old_pair_untouched(self):
        members = (self.data, self.sidecar)
        self.seed(members)
        with patch.object(hdx.json, "dump", side_effect=OSError("staging failure")), \
                patch.object(hdx.os, "replace") as replace:
            with self.assertRaisesRegex(OSError, "staging failure"):
                self.acquire()
        replace.assert_not_called()
        self.assert_original(members)

    def test_backup_failure_restores_already_moved_member(self):
        members = (self.data, self.sidecar)
        self.seed(members)
        replace = os.replace

        def injected(source, destination):
            if Path(source) == self.sidecar:
                raise OSError("backup failure")
            return replace(source, destination)

        with patch.object(hdx.os, "replace", side_effect=injected):
            with self.assertRaisesRegex(OSError, "backup failure"):
                self.acquire()
        self.assert_original(members)

    def test_both_candidates_complete_before_existing_members_move(self):
        members = (self.data, self.sidecar)
        self.seed(members)
        replace = os.replace
        checked = False

        def inspected(source, destination):
            nonlocal checked
            if Path(source) == self.data:
                candidates = set(self.root.iterdir()) - set(members) - {Path(destination)}
                self.assertEqual(len(candidates), 2)
                staged_metadata = next(path for path in candidates
                                       if path.name.startswith(self.sidecar.name + "."))
                staged_data = (candidates - {staged_metadata}).pop()
                self.assertEqual(staged_data.read_bytes(), self.new)
                metadata = json.loads(staged_metadata.read_text())
                self.assertEqual(metadata["sha256"], hashlib.sha256(self.new).hexdigest())
                for path in members:
                    self.assertEqual(path.read_bytes(), self.old[path])
                checked = True
            return replace(source, destination)

        with patch.object(hdx.os, "replace", side_effect=inspected):
            self.acquire()
        self.assertTrue(checked)


if __name__ == "__main__":
    unittest.main()
