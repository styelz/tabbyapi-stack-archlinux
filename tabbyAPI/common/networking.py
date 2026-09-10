"""Common utility functions"""

import asyncio
import json
import socket
import time
import traceback
from types import SimpleNamespace
from fastapi import Depends, HTTPException, Request
from starlette.requests import HTTPConnection
from loguru import logger
from common.logger import xlogger
from pydantic import BaseModel
from typing import Optional
from uuid import uuid4

from common.errors import context_length_error_content
from common.tabby_config import config


def get_sse_ping_interval() -> int:
    """SSE keep-alive ping interval in seconds, or effectively never if disabled."""

    from sys import maxsize

    interval = config.network.sse_ping_interval
    return interval if interval else maxsize


class TabbyRequestErrorMessage(BaseModel):
    """Common request error type."""

    message: str
    trace: Optional[str] = None


class TabbyRequestError(BaseModel):
    """Common request error type."""

    error: TabbyRequestErrorMessage


def get_generator_error(message: str, exc_info: bool = True):
    """Get a generator error."""

    generator_error = handle_request_error(message, exc_info)

    return generator_error.model_dump_json()


def get_context_length_generator_error(message: str):
    """Get an OpenAI-compatible context overflow error for an active stream."""

    handle_request_error(message, exc_info=False)
    return json.dumps(context_length_error_content(message))


def handle_request_error(message: str, exc_info: bool = True):
    """Log a request error to the console."""

    trace = traceback.format_exc()
    send_trace = config.network.send_tracebacks

    error_message = TabbyRequestErrorMessage(message=message, trace=trace if send_trace else None)

    request_error = TabbyRequestError(error=error_message)

    # Log the error and provided message to the console
    if trace and exc_info:
        xlogger.error("Error", {"trace": trace, "message": message}, details=trace)

    logger.error(f"Sent to request: {message}")

    return request_error


def handle_request_disconnect(message: str):
    """Wrapper for handling for request disconnection."""

    xlogger.error(message)


def generation_request(request=None):
    """Object with ``state.id`` for ``generate_chat_completion``.

    Background console flights keep the original HTTP request off
    ``DisconnectHandler`` so poll() does not cancel after the SSE client
    reconnects. Nested generate still needs a request id. A stand-in is
    never disconnected; a real Starlette request is returned as-is.
    """
    req_id = getattr(getattr(request, "state", None), "id", None)
    if request is not None and req_id and callable(
        getattr(request, "is_disconnected", None)
    ):
        return request

    async def is_disconnected():
        return False

    return SimpleNamespace(
        state=SimpleNamespace(id=str(req_id or uuid4().hex)),
        is_disconnected=is_disconnected,
    )


class DisconnectHandler:
    def __init__(
        self,
        request: Optional[Request] = None,
        description: str = "",
        abort_event: Optional[asyncio.Event] = None,
    ):
        self.request = request
        self.abort_event = abort_event if abort_event is not None else asyncio.Event()
        self.last_poll = time.time() - 10
        self.disconnected = False
        self.cleanup_tasks = {}
        self.description = description

        # A background task owns the request's receive channel and flags a
        # disconnect as soon as it arrives, so poll() becomes a plain flag
        # check that never suspends the caller or yields to the generator.
        #
        # Only a real Starlette request exposes receive(): nested generate()
        # (mixed dest extract) passes no request, and console flights pass the
        # is_disconnected() stand-in from generation_request(). Those keep the
        # abort_event path so occupancy can still cancel them.
        self._watcher = None
        if request is not None and callable(getattr(request, "receive", None)):
            try:
                self._watcher = asyncio.create_task(self._watch())
            except RuntimeError:
                # No running loop (constructed outside async context); fall
                # back to polling is_disconnected() in poll().
                self._watcher = None

    async def _watch(self):
        """Wait for the client to disconnect and flag it."""

        try:
            # The request body is already consumed, so the only messages left
            # are stray http.request events (ignored) and the eventual
            # http.disconnect. Uvicorn also sends http.disconnect once the
            # response completes, so this task always terminates on its own
            # even if cleanup() is never called.
            while True:
                message = await self.request.receive()
                if message["type"] == "http.disconnect":
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"Disconnect watcher for {self.description} stopped: {exc}")
            return

        self.disconnected = True
        self.abort_event.set()

    async def poll(self):
        """
        Check whether the request has disconnected. Once disconnected (or the
        abort_event is set), runs scheduled cleanup tasks and raises
        asyncio.CancelledError. Caller is responsible for forwarding the error
        back to the endpoint function. The endpoint fn should call poll() at
        least once before returning a non-canceled response.

        With a background watcher this does not suspend the caller. Otherwise
        (nested generate() or the console-flight stand-in) abort_event is
        checked on every call and is_disconnected() at most 20 times per second.
        """

        if self._watcher is not None:
            triggered = self.disconnected or self.abort_event.is_set()
        elif self.abort_event.is_set():
            triggered = True
        else:
            now = time.time()
            if now < self.last_poll + 0.05:
                return
            self.last_poll = now

            http_gone = False
            if self.request is not None and callable(
                getattr(self.request, "is_disconnected", None)
            ):
                http_gone = await self.request.is_disconnected()

            triggered = http_gone

        if not triggered:
            return

        self.abort_event.set()

        await self.cleanup()

        if not self.disconnected:
            xlogger.error(f"Request disconnected: {self.description}")
            self.disconnected = True

        raise asyncio.CancelledError(f"Request disconnected: {self.description}")

    async def add_cleanup_task(self, key, func, args):
        # Intentionally strict
        assert key not in self.cleanup_tasks
        self.cleanup_tasks[key] = (func, args)

    async def finish(self, key):
        # Intentionally strict
        del self.cleanup_tasks[key]

    # Safe to call redundantly, each cleanup task must be called exactly once
    async def cleanup(self):
        if self._watcher is not None:
            self._watcher.cancel()
        for func, args in self.cleanup_tasks.values():
            await func(*args)
        self.cleanup_tasks = {}


async def request_disconnect_loop(request: Request):
    """Polls for a starlette request disconnect."""

    while not await request.is_disconnected():
        await asyncio.sleep(0.5)


async def run_with_request_disconnect(
    request: Request, call_task: asyncio.Task, disconnect_message: str
):
    """Utility function to cancel if a request is disconnected."""

    _, unfinished = await asyncio.wait(
        [
            call_task,
            asyncio.create_task(request_disconnect_loop(request)),
        ],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in unfinished:
        task.cancel()

    try:
        return call_task.result()
    except (asyncio.CancelledError, asyncio.InvalidStateError) as ex:
        handle_request_disconnect(disconnect_message)
        raise HTTPException(422, disconnect_message) from ex


def is_port_in_use(port: int) -> bool:
    """
    Checks if a port is in use

    From https://stackoverflow.com/questions/2470971/fast-way-to-test-if-a-port-is-in-use-using-python
    """

    test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    test_socket.settimeout(1)
    with test_socket:
        return test_socket.connect_ex(("localhost", port)) == 0


async def add_request_id(request: HTTPConnection):
    """FastAPI depends to stamp a UUID on HTTP and WebSocket connections."""

    request.state.id = uuid4().hex
    return request


async def log_request(request: HTTPConnection):
    """FastAPI depends to log a request to the user."""

    method = request.scope.get("method") or request.scope.get("type", "request")
    log_message = [f"Information for {method} request {request.state.id}:"]

    log_message.append(f"URL: {request.url}")
    log_message.append(f"Headers: {dict(request.headers)}")

    if request.scope.get("type") == "http" and method != "GET":
        http_request = (
            request if isinstance(request, Request) else Request(request.scope, receive=request.receive)
        )
        body_bytes = await http_request.body()
        if body_bytes:
            body = json.loads(body_bytes.decode("utf-8"))

            log_message.append(f"Body: {dict(body)}")

    xlogger.info("Request", dict(request), details="\n".join(log_message))


def get_global_depends():
    """Returns global dependencies for a FastAPI app."""

    depends = [Depends(add_request_id)]

    if config.logging.log_requests:
        depends.append(Depends(log_request))

    return depends
