import contextlib
import http.server
import json
import os
import threading
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import hda_gdacs
import hdx_acquire_inspect
from hda_http import IncompleteResponse, open_http, read_limited, remote_json_loads


class _Handler(http.server.BaseHTTPRequestHandler):
    other_origin = ""
    cross_origin_requests = 0

    def do_GET(self):
        if self.path == "/cross":
            self.send_response(302)
            self.send_header("Location", self.other_origin + "/complete")
            self.end_headers()
        elif self.path == "/same":
            self.send_response(302)
            self.send_header("Location", "/complete")
            self.end_headers()
        elif self.path == "/complete":
            if self.server.server_address[1] == int(self.other_origin.rsplit(":", 1)[-1]):
                type(self).cross_origin_requests += 1
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/short":
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "10")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
        elif self.path == "/chunked":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"5\r\n{}\r\n")
            self.close_connection = True

    def log_message(self, format, *args):
        pass


@contextlib.contextmanager
def _servers():
    first = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    second = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    first_url = f"http://127.0.0.1:{first.server_port}"
    second_url = f"http://127.0.0.1:{second.server_port}"
    _Handler.other_origin = second_url
    _Handler.cross_origin_requests = 0
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (first, second)]
    for thread in threads:
        thread.start()
    try:
        yield first_url, second_url
    finally:
        for server in (first, second):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


class HTTPTrustAndCompletenessTests(unittest.TestCase):
    def setUp(self):
        proxy_bypass = patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        proxy_bypass.start()
        self.addCleanup(proxy_bypass.stop)

    def test_cross_origin_redirect_is_rejected_before_followup(self):
        with _servers() as (first, _):
            with self.assertRaisesRegex(ValueError, "unapproved origin"):
                open_http(first + "/cross", timeout=2)
            self.assertEqual(_Handler.cross_origin_requests, 0)

    def test_same_origin_redirect_succeeds_and_exposes_exact_final_url(self):
        with _servers() as (first, _):
            with open_http(first + "/same", timeout=2) as response:
                self.assertEqual(json.loads(read_limited(response, 100, "complete", response.headers)), {"ok": True})
                self.assertEqual(response.geturl(), first + "/complete")

    def test_reviewed_extra_origin_can_be_allowed(self):
        with _servers() as (first, second):
            with open_http(first + "/cross", timeout=2, allowed_origins=[second]) as response:
                self.assertEqual(response.geturl(), second + "/complete")

    def test_client_network_lineage_exposes_final_url(self):
        with _servers() as (first, _):
            payload, network = hda_gdacs._request_json(first + "/same", timeout=2)
            self.assertEqual(payload, {"ok": True})
            self.assertEqual(network["final_response_url"], first + "/complete")

    def test_short_content_length_rejects_valid_json(self):
        with _servers() as (first, _):
            with open_http(first + "/short", timeout=2) as response:
                with self.assertRaisesRegex(IncompleteResponse, "Content-Length: 10, received: 2"):
                    read_limited(response, 100, "short JSON", response.headers)

    def test_malformed_chunked_transfer_is_deterministic(self):
        with _servers() as (first, _):
            with open_http(first + "/chunked", timeout=2) as response:
                with self.assertRaisesRegex(IncompleteResponse, "incomplete or malformed"):
                    read_limited(response, 100, "chunked JSON", response.headers)

    def test_custom_streaming_paths_reject_incomplete_responses(self):
        with _servers() as (first, _):
            with self.assertRaises(IncompleteResponse):
                hdx_acquire_inspect.disposable_inspect(first + "/short", "CSV", 100, 1, 2)
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(IncompleteResponse):
                hdx_acquire_inspect.acquire(
                    first + "/chunked", "dataset", "resource", Path(directory) / "raw.bin", 2
                )

    def test_complete_bounded_response_succeeds(self):
        with _servers() as (first, _):
            with open_http(first + "/complete", timeout=2) as response:
                self.assertEqual(read_limited(response, 100, "complete", response.headers), b'{"ok": true}')


class RemoteJSONBoundaryTests(unittest.TestCase):
    def test_deep_json_becomes_controlled_value_error(self):
        document = b"[" * 1200 + b"0" + b"]" * 1200
        with unittest.mock.patch("hda_http.json.loads", side_effect=RecursionError("nested JSON")):
            with self.assertRaisesRegex(ValueError, "nesting capacity"):
                remote_json_loads(document)

    def test_integer_token_bound_and_large_normal_integer(self):
        with self.assertRaisesRegex(ValueError, "4300 decimal digits"):
            remote_json_loads(b"1" * 4301)
        token = "9" * 500
        self.assertEqual(remote_json_loads(token), int(token))

    def test_float_and_constant_strictness(self):
        for document in ("NaN", "Infinity", "-Infinity", "1e309"):
            with self.subTest(document=document), self.assertRaises(ValueError):
                remote_json_loads(document)
        self.assertEqual(remote_json_loads("-123.5e2"), -12350.0)


if __name__ == "__main__":
    unittest.main()
