"""Authenticated HTTP coordinator; workers need no shared filesystem."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .scheduler import JobStore, SchedulerError, canonical

# An 8 MiB patch may expand sixfold in escaped JSON. Complete traces stay local.
MAX_REQUEST_BYTES = 64 * 1024 * 1024


class Coordinator(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, store, token, *, request_timeout=15, max_connections=16):
        if not isinstance(token, str) or not 32 <= len(token) <= 4096 or "\n" in token or "\r" in token:
            raise ValueError("Use a bearer token of at least 32 characters from the external environment")
        if not 1 <= request_timeout <= 120 or not 1 <= max_connections <= 64:
            raise ValueError("Invalid bounded server configuration")
        self.store, self._token, self.request_timeout = store, token, request_timeout
        self.tls_context = None
        self._slots = threading.BoundedSemaphore(max_connections)
        self._active = 0
        self._drain = threading.Condition()
        super().__init__(address, CoordinatorHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.request_timeout)
        if self.tls_context is not None:
            try:
                connection = self.tls_context.wrap_socket(connection, server_side=True)
            except BaseException:
                connection.close()
                raise
        return connection, address

    def process_request(self, request, client_address):
        self._slots.acquire()
        with self._drain:
            self._active += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            with self._drain:
                self._active -= 1
                self._drain.notify_all()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
            with self._drain:
                self._active -= 1
                self._drain.notify_all()

    def server_close(self):
        super().server_close()
        # Let bounded request handlers close their SQLite connections before a
        # clean coordinator stop; Windows otherwise retains open database files.
        deadline = time.monotonic() + self.request_timeout + 12
        with self._drain:
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._drain.wait(remaining)

    def handle_error(self, request, client_address):
        # Disconnected clients and TLS handshakes are normal bounded failures;
        # unexpected errors already produce a sanitized JSON reply in the handler.
        pass


class CoordinatorHandler(BaseHTTPRequestHandler):
    server_version = "DeliveryCoordinator/1"

    def log_message(self, format, *args):
        # Request URLs and bodies may contain task data; do not dump them.
        pass

    def _reply(self, status, value):
        raw = canonical(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _authenticate(self):
        value = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server._token
        if not hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8")):
            raise SchedulerError("Authentication required", 401)

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise SchedulerError("Chunked requests are unsupported", 400)
        if self.headers.get_content_type() != "application/json":
            raise SchedulerError("application/json required", 415)
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            raise SchedulerError("One Content-Length required", 411)
        try:
            size = int(lengths[0])
        except ValueError as exc:
            raise SchedulerError("Invalid Content-Length") from exc
        if not 0 < size <= MAX_REQUEST_BYTES:
            raise SchedulerError("Request exceeds limit", 413)
        deadline = time.monotonic() + self.server.request_timeout
        remaining, chunks = size, []
        while remaining:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise SchedulerError("Request body timeout", 408)
            self.connection.settimeout(timeout)
            chunk = self.rfile.read1(min(remaining, 64 * 1024))
            if not chunk:
                raise SchedulerError("Incomplete request")
            chunks.append(chunk)
            remaining -= len(chunk)
        try:
            value = json.loads(b"".join(chunks).decode("utf-8"),
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise SchedulerError("Malformed finite JSON") from exc
        if not isinstance(value, dict):
            raise SchedulerError("Request body must be an object")
        return value

    @staticmethod
    def _fields(value, required, optional=()):
        if set(value) - (set(required) | set(optional)) or set(required) - set(value):
            raise SchedulerError("Unexpected or missing request fields")

    def _dispatch(self, method):
        self._authenticate()
        parsed = urlsplit(self.path)
        path = parsed.path.split("/")
        store = self.server.store
        if method == "GET":
            if parsed.path == "/health":
                return {"status": "ok", "protocol": 1}
            if parsed.path == "/v1/jobs":
                query = parse_qs(parsed.query)
                if set(query) - {"limit"}:
                    raise SchedulerError("Unknown query field")
                try:
                    limit = int(query.get("limit", ["100"])[0])
                except ValueError as exc:
                    raise SchedulerError("Invalid limit") from exc
                return {"jobs": store.list_jobs(limit=limit)}
            if len(path) == 4 and path[:3] == ["", "v1", "jobs"] and not parsed.query:
                return store.get(path[3])
            raise SchedulerError("Endpoint not found", 404)
        if parsed.query:
            raise SchedulerError("POST query fields are unsupported")
        value = self._body()
        if parsed.path == "/v1/workers/register":
            self._fields(value, ["worker_id"])
            return store.register(value["worker_id"])
        if parsed.path == "/v1/jobs":
            self._fields(value, ["job_id", "payload"], ["max_attempts"])
            return store.submit(value["job_id"], value["payload"], max_attempts=value.get("max_attempts", 3))
        if parsed.path == "/v1/claim":
            self._fields(value, ["worker_id"])
            return {"job": store.claim(value["worker_id"])}
        if len(path) == 5 and path[:3] == ["", "v1", "jobs"]:
            job_id, action = path[3:]
            if action == "cancel":
                self._fields(value, [])
                return store.cancel(job_id)
            self._fields(value, ["worker_id", "lease_token", "generation"],
                         ["completion"] if action == "complete" else [])
            args = (job_id, value["worker_id"], value["lease_token"], value["generation"])
            if action == "heartbeat":
                return store.heartbeat(*args)
            if action == "complete":
                if "completion" not in value:
                    raise SchedulerError("Completion required")
                return store.complete(*args, value["completion"])
        raise SchedulerError("Endpoint not found", 404)

    def _handle(self, method):
        try:
            self._reply(200, self._dispatch(method))
        except SchedulerError as exc:
            try:
                self._reply(exc.status, {"error": str(exc)})
            except OSError:
                pass
        except (TimeoutError, socket.timeout):
            try:
                self._reply(408, {"error": "Request timeout"})
            except OSError:
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            # Never echo SQLite paths, credentials, or unexpected exception text.
            try:
                self._reply(500, {"error": "Coordinator internal error"})
            except OSError:
                pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--lease-seconds", type=float, default=30)
    parser.add_argument("--token-env", default="DELIVERY_SCHEDULER_TOKEN")
    parser.add_argument("--tls-cert")
    parser.add_argument("--tls-key")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535 or bool(args.tls_cert) != bool(args.tls_key):
        parser.error("Invalid port or incomplete TLS configuration")
    token = os.environ.get(args.token_env, "")
    if len(token) < 32:
        parser.error("External bearer-token environment variable must contain at least 32 characters")
    store = JobStore(args.db, lease_seconds=args.lease_seconds)
    server = Coordinator((args.host, args.port), store, token)
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
        # Bound each handshake before TLS wrapping; an idle TLS client must not
        # hold the accept loop indefinitely before get_request sets its timeout.
        server.tls_context = context
    print(canonical({"listening": args.host, "port": server.server_port,
                     "tls": bool(args.tls_cert), "protocol": 1}), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
