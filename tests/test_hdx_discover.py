import io
import json
import unittest
from unittest.mock import patch

import hdx_discover


class _Response(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class HdxDiscoverTests(unittest.TestCase):
    def test_preserves_scope_identity_and_bounds_candidates_and_resources(self):
        payload = {
            "success": True,
            "result": {
                "count": 12,
                "results": [
                    {
                        "id": "dataset-native-id",
                        "name": "catalog-slug",
                        "title": "Water points",
                        "notes": "  Compact   description. ",
                        "dataset_date": "[2024-01-01 TO 2024-12-31]",
                        "resources": [
                            {"id": "r1", "name": "points", "format": "CSV", "url": "https://example/r1"},
                            {"id": "r2", "name": "map", "format": "GeoJSON", "url": "https://example/r2"},
                        ],
                    },
                    {"id": "ignored", "resources": []},
                ],
            },
        }
        with patch.object(hdx_discover, "open_http", return_value=_Response(json.dumps(payload).encode())):
            output = hdx_discover.discover("water point functionality", 1, 1, [], 5)
        self.assertEqual(output["search"]["terms"], "water point functionality")
        self.assertEqual(output["search"]["filters"], [])
        self.assertEqual(output["counts"], {"catalog_matches": 12, "returned_candidates": 1})
        self.assertEqual(output["candidates"][0]["dataset_id"], "dataset-native-id")
        self.assertEqual(output["candidates"][0]["resources"]["items"][0]["id"], "r1")
        self.assertTrue(output["bounded"]["candidates_truncated"])
        self.assertTrue(output["candidates"][0]["resources"]["truncated"])

    def test_zero_results_emit_only_scoped_negative_claim(self):
        payload = {"success": True, "result": {"count": 0, "results": []}}
        with patch.object(hdx_discover, "open_http", return_value=_Response(json.dumps(payload).encode())):
            output = hdx_discover.discover("nonexistent probe", 3, 2, ["organization:test"], 5)
        self.assertEqual(output["counts"]["catalog_matches"], 0)
        self.assertEqual(
            output["scoped_negative"],
            "No HDX catalog packages were returned for this search scope.",
        )
        self.assertEqual(output["search"]["filters"], ["organization:test"])


class HdxStdoutTests(unittest.TestCase):
    def result(self, *, title="Water points", resource_name="points"):
        payload = {
            "success": True,
            "result": {
                "count": 1,
                "results": [{"id": "candidate-secret-marker", "title": title,
                             "resources": [{"id": "resource-secret-marker", "name": resource_name}]}],
            },
        }
        with patch.object(hdx_discover, "open_http", return_value=_Response(json.dumps(payload).encode())):
            return hdx_discover.discover("água", 1, 1, ["organization:test"], 5)

    def run_main(self, result, *args):
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        stderr = io.StringIO()
        with patch.object(hdx_discover, "discover", return_value=result), \
             patch.object(hdx_discover.sys, "stdout", stdout), \
             patch.object(hdx_discover.sys, "stderr", stderr):
            status = hdx_discover.main(["água", *args])
        return status, stdout.buffer.getvalue(), stderr.getvalue()

    def normal_bytes(self, result):
        return (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")

    def test_small_result_is_byte_for_byte_unchanged(self):
        result = self.result()
        status, emitted, error = self.run_main(result)
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(emitted, self.normal_bytes(result))

    def test_oversized_candidate_or_resource_is_omitted_with_context(self):
        limit = hdx_discover.DEFAULT_STDOUT_BYTES_LIMIT
        for field in ("title", "resource_name"):
            with self.subTest(field=field):
                result = self.result(**{field: "界" * (limit // 2)})
                status, emitted, error = self.run_main(result)
                envelope = json.loads(emitted)
                self.assertEqual((status, error), (0, ""))
                self.assertLessEqual(len(emitted), 256 * 1024)
                self.assertTrue(emitted.endswith(b"\n"))
                self.assertEqual(envelope["schema_version"], "hda-output-limit-envelope/v1")
                for key in ("catalog", "search", "counts", "bounded"):
                    self.assertEqual(envelope[key], result[key])
                self.assertEqual(envelope["stdout_bytes_limit"], limit)
                self.assertEqual(envelope["normal_serialized_bytes"], len(self.normal_bytes(result)))
                self.assertTrue(envelope["output_limit_exceeded"])
                self.assertTrue(envelope["data_omitted"])
                self.assertIn("omitted", envelope["reason"])
                self.assertNotIn("candidates", envelope)
                for marker in (b"candidate-secret-marker", b"resource-secret-marker", "界".encode()):
                    self.assertNotIn(marker, emitted)

    def test_larger_cli_override_emits_data_default_omits(self):
        result = self.result(title="x" * hdx_discover.DEFAULT_STDOUT_BYTES_LIMIT)
        normal = self.normal_bytes(result)
        self.assertTrue(json.loads(self.run_main(result)[1])["data_omitted"])
        status, emitted, error = self.run_main(result, "--stdout-bytes-limit", str(len(normal)))
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(emitted, normal)
        # The trailing newline counts: one byte less must omit the payload.
        self.assertTrue(json.loads(self.run_main(result, "--stdout-bytes-limit", str(len(normal) - 1))[1])["data_omitted"])

    def test_invalid_cli_limits_are_rejected_before_discovery(self):
        for value in ("262143", "1", "0", "-1", "1.5", "not-an-integer"):
            with self.subTest(value=value), \
                 patch.object(hdx_discover, "discover") as discover, \
                 patch.object(hdx_discover.sys, "stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    hdx_discover.main(["water", "--stdout-bytes-limit", value])
                self.assertEqual(exc.exception.code, 2)
                discover.assert_not_called()

    def test_oversized_envelope_fails_cleanly_without_stdout(self):
        result = self.result()
        result["search"]["terms"] = "x" * hdx_discover.DEFAULT_STDOUT_BYTES_LIMIT
        status, emitted, error = self.run_main(result)
        self.assertEqual(status, 1)
        self.assertEqual(emitted, b"")
        self.assertIn("output-limit envelope exceeds stdout byte limit", error)

    def test_existing_cli_count_boundaries_unchanged(self):
        result = self.result()
        status, _, error = self.run_main(result, "--limit", "100", "--resource-limit", "100")
        self.assertEqual((status, error), (0, ""))
        for option in ("--limit", "--resource-limit"):
            for value in ("0", "101"):
                with self.subTest(option=option, value=value), \
                     patch.object(hdx_discover, "discover") as discover, \
                     patch.object(hdx_discover.sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit) as exc:
                        hdx_discover.main(["water", option, value])
                    self.assertEqual(exc.exception.code, 2)
                    discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
