"""Container-local HTTP relay: unchanged model requests, recoverable waits only.

Copied into the trial container; deliberately uses only Python's standard library.
No prompts, keys, response bodies or endpoints are written to its timing journal.
"""

from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
import socket
import tempfile
import threading
import time
import uuid
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def recoverable_status(status):
    # Same status policy as llm.client._recoverable_transport.
    return status in {401, 402, 403, 408, 409, 425, 429} or 500 <= status <= 599


_MAX_TOKENS_LIMIT = re.compile(
    r"max_tokens.*?(?:valid|maximum|limit).*?(?:<=|less than or equal to)\s*(\d+)"
    r".*?(?:got|received)\s*(\d+)",
    re.IGNORECASE | re.DOTALL,
)


def max_tokens_limit(body):
    """Return an explicit provider output-token limit, if the error states one.

    This intentionally recognizes only the unambiguous "max_tokens ... <= N ...
    got M" form.  A generic 400 response must remain fatal rather than becoming
    an unbounded or speculative request rewrite.
    """
    if not body:
        return None
    if isinstance(body, bytes):
        body = body[:131072].decode("utf-8", "replace")
    match = _MAX_TOKENS_LIMIT.search(str(body))
    if not match:
        return None
    limit, requested = map(int, match.groups())
    return limit if requested > limit > 0 else None


def _request_with_max_tokens(request, limit):
    """Clone an OpenAI request with one provider-confirmed smaller cap."""
    if not limit or limit <= 0:
        return None
    try:
        payload = json.loads(request.data or b"{}")
        if not isinstance(payload, dict):
            return None
        requested = int(payload.get("max_tokens"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if requested <= limit:
        return None
    payload["max_tokens"] = limit
    headers = dict(request.headers)
    headers.pop("Content-Length", None)
    headers.pop("content-length", None)
    return Request(
        request.full_url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        origin_req_host=request.origin_req_host,
        unverifiable=request.unverifiable,
        method=request.get_method(),
    )


def response_error(data, content_type):
    """Recognize provider failures carried inside an HTTP 200 response."""
    try:
        if "event-stream" in content_type:
            records = []
            completed = False
            for line in data:
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    completed = True
                    continue
                try:
                    value = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(value, dict):
                    continue
                if value.get("error"):
                    records.append(value)
                if (
                    value.get("type") == "message_stop"
                    or value.get("type") == "response.completed"
                    or any(
                        c.get("finish_reason") is not None
                        for c in (value["choices"] if isinstance(value.get("choices"), list) else [])
                        if isinstance(c, dict)
                    )
                ):
                    completed = True
            if not records:
                return None if completed else 503
        elif "json" in content_type:
            try:
                records = [json.load(data)]
            except ValueError:
                return 503
        else:
            return None
        for value in records:
            if not isinstance(value, dict):
                return 503
            error = value.get("error")
            if error:
                try:
                    code = int(error.get("code")) if isinstance(error, dict) else 503
                except (TypeError, ValueError):
                    code = 503
                return code if 400 <= code <= 599 else 503
            if "choices" in value and (not isinstance(value["choices"], list)
                    or not value["choices"] or any(not isinstance(c, dict) for c in value["choices"])):
                return 503
        return None
    finally:
        data.seek(0)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        # Preserve method/body and do not forward model credentials to a new host.
        return None


class ModelRelay(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, routes, events, *, retry_seconds=30, opener=None):
        self.routes = routes
        self.events = Path(events)
        self.retry_seconds = retry_seconds
        self.retry_backoffs = (2, 4, 6)
        # urllib honors Pier's authenticated HTTP(S)_PROXY and domain ACLs.
        self.opener = opener or build_opener(NoRedirect())
        self.journal_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), RelayHandler)

    def record(self, request_id, event, **fields):
        record = dict(request_id=request_id, event=event, at=time.monotonic(), **fields)
        with self.journal_lock, self.events.open("a") as stream:
            stream.write(json.dumps(record) + "\n")

    def fetch(self, request_id, request, cancelled):
        batch_started = time.monotonic()
        attempt = 0
        max_tokens_adapted = False
        while not cancelled.is_set():
            data = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
            try:
                # Match the reference OpenAI SDK's ordinary 600-second network
                # timeout; max-reasoning TTFT must not become a short retry loop.
                response = self.opener.open(request, timeout=600)
                try:
                    content_type = response.headers.get(
                        "Content-Type", "application/json"
                    )
                    streaming = "event-stream" in content_type
                    terminal = False
                    read = (
                        response.readline if streaming else lambda: response.read(65536)
                    )
                    while chunk := read():
                        if cancelled.is_set():
                            data.close()
                            return None
                        data.write(chunk)
                        if streaming:
                            # SSE's terminal event can precede HTTP EOF on a
                            # keep-alive connection. Preserve its blank separator,
                            # then close upstream instead of waiting for EOF.
                            if terminal and not chunk.strip():
                                break
                            if chunk.startswith(b"data:"):
                                payload = chunk[5:].strip()
                                if payload == b"[DONE]":
                                    terminal = True
                                else:
                                    try:
                                        value = json.loads(payload)
                                    except ValueError:
                                        continue
                                    terminal = isinstance(value, dict) and value.get(
                                        "type"
                                    ) in {"message_stop", "response.completed"}
                finally:
                    response.close()
                data.seek(0)
                failure = response_error(data, content_type)
                if failure == 400 and not max_tokens_adapted:
                    body = data.read()
                    replacement = _request_with_max_tokens(
                        request, max_tokens_limit(body)
                    )
                    data.seek(0)
                    if replacement is not None:
                        data.close()
                        request = replacement
                        max_tokens_adapted = True
                        self.record(request_id, "max_tokens_adapted", limit=max_tokens_limit(body))
                        continue
                if failure is None or not recoverable_status(failure):
                    return failure or 200, content_type, data
            except HTTPError as error:
                if not recoverable_status(error.code):
                    try:
                        body = error.read()
                        replacement = None
                        if error.code == 400 and not max_tokens_adapted:
                            replacement = _request_with_max_tokens(
                                request, max_tokens_limit(body)
                            )
                        if replacement is not None:
                            error.close()
                            request = replacement
                            max_tokens_adapted = True
                            self.record(request_id, "max_tokens_adapted", limit=max_tokens_limit(body))
                            continue
                        data.write(body)
                        data.seek(0)
                        return (
                            error.code,
                            error.headers.get("Content-Type", "application/json"),
                            data,
                        )
                    finally:
                        error.close()
                error.close()
            except (URLError, TimeoutError, ConnectionError, http.client.HTTPException):
                pass
            except BaseException:
                data.close()
                raise
            data.close()
            # llm.client first tries four calls with 2/4/6-second backoffs.
            # A batch that recovers within those calls remains charged work.
            # Only an exhausted recoverable batch reaches core.loop's pause.
            if attempt < len(self.retry_backoffs):
                cancelled.wait(self.retry_backoffs[attempt])
                attempt += 1
                continue
            self.record(request_id, "pause_start", started=batch_started)
            try:
                cancelled.wait(self.retry_seconds)
            finally:
                self.record(request_id, "pause_end")
            batch_started = time.monotonic()
            attempt = 0
        return None


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_POST(self):
        self._forward()

    def do_GET(self):
        self._forward()

    def _forward(self):
        parts = self.path.split("/", 2)
        origin = self.server.routes.get(parts[1]) if len(parts) == 3 else None
        if origin is None:
            self.send_error(404)
            return
        payload = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            streaming = json.loads(payload).get("stream") is True
        except (ValueError, AttributeError):
            streaming = False
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower()
            not in {"host", "content-length", "connection", "accept-encoding"}
        }
        request = Request(
            origin + "/" + parts[2],
            data=payload if self.command == "POST" else None,
            headers=headers,
            method=self.command,
        )
        request_id = uuid.uuid4().hex
        cancelled = threading.Event()
        self.server.record(request_id, "request_start")
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(self.server.fetch, request_id, request, cancelled)
        began = False
        try:
            while True:
                if future.done():
                    result = future.result()
                    break
                try:
                    result = future.result(timeout=5)
                    break
                except concurrent.futures.TimeoutError:
                    if streaming:
                        if not began:
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream")
                            self.send_header("Connection", "close")
                            self.end_headers()
                            began = True
                        # SSE comments keep the native transport alive, without
                        # producing assistant messages, tokens or model rounds.
                        self.wfile.write(b": transport pending\n\n")
                        self.wfile.flush()
            if result is None:
                return
            status, content_type, data = result
            with data:
                if began and status != 200:
                    # An SSE connection already has HTTP 200 headers. Preserve
                    # the fatal status in a provider-error envelope instead.
                    error = {
                        "error": {"code": status, "message": "Model request rejected"}
                    }
                    self.wfile.write(("data: " + json.dumps(error) + "\n\n").encode())
                else:
                    if not began:
                        self.send_response(status)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Connection", "close")
                        self.end_headers()
                    while chunk := data.read(65536):
                        self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            cancelled.set()
            pool.shutdown(wait=True)
            if future.done() and not future.cancelled() and future.exception() is None:
                result = future.result()
                if result is not None:
                    result[2].close()
            self.server.record(request_id, "request_end")
            self.close_connection = True


def main():
    import sys

    config_path, events_path, ready_path = map(Path, sys.argv[1:])
    config = json.loads(config_path.read_text())
    models = [
        m for m in config.get("ai", {}).get("models", []) if m.get("enabled", True)
    ]
    routes = {}
    for index, model in enumerate(models):
        url = urlsplit(model["base_url"])
        routes[f"model-{index}"] = f"{url.scheme}://{url.netloc}"
    relay = ModelRelay(routes, events_path)
    for index, model in enumerate(models):
        url = urlsplit(model["base_url"])
        model["base_url"] = (
            f"http://127.0.0.1:{relay.server_port}/model-{index}{url.path}"
        )
    # Only the relay uses Pier's egress proxy; native requests stay on loopback.
    config["ai"]["proxy"] = {"enabled": False}
    # The relay owns transport timeouts/retries. Prevent BitFun from abandoning
    # the same operation during a long outage (or while a response is buffered).
    # These transport-only values are never inserted into role prompts.
    config["ai"]["stream_idle_timeout_secs"] = 2**32 - 1
    config["ai"]["stream_ttft_timeout_secs"] = 2**32 - 1
    with config_path.open("w") as stream:
        json.dump(config, stream)
    os.chmod(config_path, 0o600)
    ready_path.write_text(str(os.getpid()))
    relay.serve_forever()


if __name__ == "__main__":
    main()
