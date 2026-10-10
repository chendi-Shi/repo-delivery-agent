"""Real segmented HTTP/TLS rejection: deliver 401 without dispatching body data."""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from delivery_agent.scheduler import JobStore, canonical
from delivery_agent.scheduler_server import (
    Coordinator, CoordinatorHandler, MAX_REQUEST_BYTES, REJECTED_BODY_DRAIN_BYTES,
)

TOKEN = "rejection-fixture-" + "a" * 40

# Public, disposable self-signed TLS fixture material is appended below. It is
# used only by this loopback test; no installed credentials or external service.


class HTTPRejectionTests(unittest.TestCase):
    @contextmanager
    def coordinator(self, *, tls=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = JobStore(root / "jobs.sqlite")
            closed = threading.Event()

            class ObservedCoordinator(Coordinator):
                def process_request_thread(self, request, address):
                    try:
                        super().process_request_thread(request, address)
                    finally:
                        closed.set()

            context = None
            if tls:
                (root / "fixture-cert.pem").write_text(TLS_CERTIFICATE, encoding="ascii")
                (root / "fixture-key.pem").write_text(TLS_PRIVATE_KEY, encoding="ascii")
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(root / "fixture-cert.pem", root / "fixture-key.pem")
            server = ObservedCoordinator(("127.0.0.1", 0), store, TOKEN, request_timeout=2)
            server.tls_context = context
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            try:
                with socket.create_connection(server.server_address, timeout=3) as transport:
                    transport.settimeout(3)
                    if tls:
                        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                        # This public self-signed fixture tests encrypted transport,
                        # not production certificate trust configuration.
                        context.check_hostname = False
                        context.verify_mode = ssl.CERT_NONE
                        with context.wrap_socket(transport, server_hostname="localhost") as client:
                            yield client, closed, store
                    else:
                        yield transport, closed, store
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    @staticmethod
    def headers(framing):
        return ("POST /v1/jobs HTTP/1.1\r\nHost: localhost\r\n"
            "Authorization: Bearer wrong-fixture-token\r\nContent-Type: application/json\r\n"
            + framing + "Connection: close\r\n\r\n").encode("ascii")

    @staticmethod
    def body():
        body = canonical({"job_id": "forbidden", "max_attempts": 1,
            "payload": {"task": {"instance_id": "fixture", "repo": "example/project",
                "base_commit": "a" * 40, "problem_statement": "Trusted transport fixture",
                "image": "python:3.12-slim", "public_checks": []},
                "config": {"model": "fixture", "profile": "baseline"}}}).encode()
        return body + b" " * (32768 - len(body))

    def assert_unauthorized(self, client):
        response = http.client.HTTPResponse(client)
        response.begin()
        self.assertEqual(response.status, 401)
        self.assertEqual(json.loads(response.read()), {"error": "Authentication required"})

    def segmented_rejection(self, *, tls):
        rejected = threading.Event()
        first_byte_sent = threading.Event()
        response_sent = threading.Event()
        original_reply = CoordinatorHandler._reply

        def gate_response(handler, status, value):
            if status == 401:
                rejected.set()
                if not first_byte_sent.wait(2):
                    raise AssertionError("Client did not send the first body segment")
            original_reply(handler, status, value)
            if status == 401:
                response_sent.set()

        with patch.object(CoordinatorHandler, "_reply", gate_response), self.coordinator(tls=tls) as (client, closed, store):
            body = self.body()
            client.sendall(self.headers(f"Content-Length: {len(body)}\r\n"))
            self.assertTrue(rejected.wait(2), "Authentication must reject before waiting for body")
            client.sendall(body[:1])
            first_byte_sent.set()
            self.assertTrue(response_sent.wait(2))
            # Client sends the remainder after the server has produced 401.
            # Old Windows behavior closes here and reproducibly loses 401 to 10053.
            self.assertFalse(closed.wait(.03), "Rejection closed while the bounded body was still arriving")
            client.sendall(body[1:])
            self.assert_unauthorized(client)
            self.assertTrue(closed.wait(1), "Body cleanup did not finish")
            self.assertEqual(store.list_jobs(), [])

    def test_segmented_plain_http_receives_401_without_creating_job(self):
        self.segmented_rejection(tls=False)

    def test_segmented_tls_receives_401_without_creating_job(self):
        self.segmented_rejection(tls=True)

    def test_slow_body_has_absolute_deadline_and_immediate_401(self):
        with self.coordinator() as (client, closed, store):
            client.sendall(self.headers("Content-Length: 1000\r\n"))
            self.assert_unauthorized(client)
            stop = threading.Event()

            def drip():
                while not stop.wait(.025):
                    try:
                        client.sendall(b" ")
                    except OSError:
                        return

            producer = threading.Thread(target=drip, daemon=True)
            producer.start()
            try:
                # Repeated partial reads must not restart the 250ms budget.
                self.assertTrue(closed.wait(1), "A trickling body extended cleanup indefinitely")
            finally:
                stop.set()
                producer.join(timeout=1)
            self.assertEqual(store.list_jobs(), [])

    def test_ambiguous_or_oversize_framing_is_never_drained(self):
        original = CoordinatorHandler._drain_rejected_body
        framing_cases = [
            "Content-Length: 10\r\nContent-Length: 10\r\n",
            "Transfer-Encoding: chunked\r\nContent-Length: 10\r\n",
            "Content-Length: not-a-number\r\n",
            f"Content-Length: {REJECTED_BODY_DRAIN_BYTES + 1}\r\n",
            f"Content-Length: {MAX_REQUEST_BYTES + 1}\r\n",
        ]
        for framing in framing_cases:
            with self.subTest(framing=framing):
                reads = []

                class ReadAudit:
                    def __init__(self, stream):
                        self.stream = stream

                    def read1(self, size):
                        reads.append(size)
                        raise AssertionError("Ambiguous/oversized body must not be consumed")

                    def __getattr__(self, name):
                        return getattr(self.stream, name)

                def audit(handler):
                    handler.rfile = ReadAudit(handler.rfile)
                    return original(handler)

                with patch.object(CoordinatorHandler, "_drain_rejected_body", audit), self.coordinator() as (client, closed, store):
                    client.sendall(self.headers(framing))
                    self.assert_unauthorized(client)
                    self.assertTrue(closed.wait(1))
                    self.assertEqual(reads, [])
                    self.assertEqual(store.list_jobs(), [])


TLS_CERTIFICATE = """-----BEGIN CERTIFICATE-----
MIIBSjCB8aADAgECAhQauVtbTIITxNhNwacRqkUCoxJr9jAKBggqhkjOPQQDAjAU
MRIwEAYDVQQDDAlsb2NhbGhvc3QwHhcNMjYxMDEwMDY1MTMwWhcNMzYxMDA3MDY1
MTMwWjAUMRIwEAYDVQQDDAlsb2NhbGhvc3QwWTATBgcqhkjOPQIBBggqhkjOPQMB
BwNCAAQHyhIj4S2fkxEIwp2AmHLiY9vHm//jh0HvdcIekFbL1MSTE/DSCT+mBfsm
dR/BNE5iXrmnjyUCnBIfywui51+EoyEwHzAdBgNVHQ4EFgQUh43Vjcyt9R4RN4rb
BX1Z5znpQQowCgYIKoZIzj0EAwIDSAAwRQIhALcGkpq6x81gwLjNvr7KpvT2Bd76
rBipuqDVnqqT5UldAiBYzr9cNjRIDk/jFTMH2hb/M8y2Ov1iRAwumSSxkgZlRQ==
-----END CERTIFICATE-----
"""
TLS_PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgeYA5l9wDNlFL5ALO
D0NOgRk6IxKyfEB7dChFE9c+VnWhRANCAAQHyhIj4S2fkxEIwp2AmHLiY9vHm//j
h0HvdcIekFbL1MSTE/DSCT+mBfsmdR/BNE5iXrmnjyUCnBIfywui51+E
-----END PRIVATE KEY-----
"""


if __name__ == "__main__":
    unittest.main()
