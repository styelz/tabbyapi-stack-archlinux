"""
Internal logging utility.
"""

import logging
import os
import sys
import requests
import json
from datetime import datetime, timezone
import re
from collections.abc import Mapping, Sequence, Set
from typing import Optional

from loguru import logger
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

from common.utils import unwrap


def console_width(
    env_value: Optional[str] = None,
    isatty: Optional[bool] = None,
) -> Optional[int]:
    """Rich wraps at 80 when stdout is not a TTY (systemd journal)."""
    raw = os.getenv("TABBY_LOG_CONSOLE_WIDTH") if env_value is None else env_value
    if raw is not None and str(raw).isnumeric() and int(raw) > 0:
        return int(raw)
    tty = sys.stdout.isatty() if isatty is None else isatty
    return None if tty else 256


RICH_CONSOLE = Console(width=console_width(), soft_wrap=True)
LOG_LEVEL = os.getenv("TABBY_LOG_LEVEL", "INFO")


def get_progress_bar():
    return Progress(console=RICH_CONSOLE)


def get_loading_progress_bar():
    """Gets a pre-made progress bar for loading tasks."""

    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=RICH_CONSOLE,
    )


def _log_formatter(record: dict):
    """Log message formatter."""

    color_map = {
        "TRACE": "dim blue",
        "DEBUG": "cyan",
        "INFO": "green",
        "SUCCESS": "bold green",
        "WARNING": "yellow",
        "ERROR": "red",
        "CRITICAL": "bold white on red",
    }

    time = record.get("time")
    colored_time = f"[grey37]{time:YYYY-MM-DD HH:mm:ss.SSS}[/grey37]"

    level = record.get("level")
    level_color = color_map.get(level.name, "cyan")
    colored_level = f"[{level_color}]{level.name}[/{level_color}]:"

    separator = " " * (9 - len(level.name))

    message = unwrap(record.get("message"), "")

    # Replace once loguru allows for turning off str.format
    message = message.replace("{", "{{").replace("}", "}}").replace("<", r"\<")

    # Escape markup tags from Rich
    message = escape(message)
    lines = message.splitlines()

    fmt = ""
    if len(lines) > 1:
        fmt = "\n".join([f"{colored_time} {colored_level}{separator}{line}" for line in lines])
    else:
        fmt = f"{colored_time} {colored_level}{separator}{message}"

    return fmt


# uvicorn access lines for the management UI itself (status poll, assets, logs stream)
# and the sidecar GET /v1/model card poll. Optional first segment covers reverse-proxy
# prefixes such as /openai/v1/ui. Exact /model only — keep /v1/model/load visible.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_UI_ACCESS_RE = re.compile(r'"[A-Z]+ (?:/\w+)?(?:/v1)?/ui(?:[/?\s]|$)')
_MODEL_POLL_RE = re.compile(r'"GET (?:/\w+)?(?:/v1)?/model(?:[\s?"]|$)')
# sse_starlette logs every sent chunk at DEBUG. Streaming /ui/logs back into
# journalctl turns that into a runaway echo (multi-megabyte lines, /health hangs).
_SSE_ECHO_RE = re.compile(r"event:\s*log|chunk:\s*b['\"]event:", re.I)
_JOURNAL_LINE_MAX = 4000


def is_ui_access_line(line: str) -> bool:
    text = _ANSI_RE.sub("", line or "")
    return bool(_UI_ACCESS_RE.search(text) or _MODEL_POLL_RE.search(text))


def is_hidden_journal_line(line: str) -> bool:
    text = _ANSI_RE.sub("", line or "")
    if not text:
        return True
    if len(text) > _JOURNAL_LINE_MAX:
        return True
    if is_ui_access_line(text):
        return True
    if _SSE_ECHO_RE.search(text):
        return True
    # Expected while Comfy/llama owns the GPU; pollers used to reprint this
    # twice a second and the Logs SSE echoed every line.
    if "No models are currently loaded." in text:
        return True
    if "\\" * 40 in text:
        return True
    return False


# Uvicorn log handler
# Uvicorn log portions inspired from https://github.com/encode/uvicorn/discussions/2027#discussioncomment-6432362
class UvicornLoggingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record).rstrip()
        if is_ui_access_line(message):
            return
        logger.opt(exception=record.exc_info).log(record.levelname, message)


# Uvicorn config for logging. Passed into run when creating all loggers in server
UVICORN_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "uvicorn": {
            "class": f"{UvicornLoggingHandler.__module__}.{UvicornLoggingHandler.__qualname__}",  # noqa
        },
    },
    "root": {"handlers": ["uvicorn"], "propagate": False, "level": LOG_LEVEL},
}


def setup_logger():
    """Bootstrap the logger."""

    logger.remove()
    # Keep INFO+ from sse_starlette; DEBUG chunk dumps re-enter journalctl.
    logging.getLogger("sse_starlette").setLevel(logging.INFO)
    logging.getLogger("sse_starlette.sse").setLevel(logging.INFO)

    logger.add(
        RICH_CONSOLE.print,
        level=LOG_LEVEL,
        format=_log_formatter,
        colorize=True,
    )
    # Add file logging
    logger.add(
        "logs/{time}.log",
        level=LOG_LEVEL,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {message}",
        rotation="20 MB",  # Rotate file when it reaches 20MB
        retention="1 week",  # Keep logs for 1 week
        compression="zip",  # Compress rotated log
    )


"""
Extended logging via Seq.
"""

_DATA_URL_RE = re.compile(r"^(data:)([^;,]+)?(?:;[^,]*)?(;base64),(.*)$", re.DOTALL)


def _sanitize_for_logging(obj, head=1024, tail=1024):
    def truncate_string(s: str) -> str:
        if head + tail >= len(s):
            return s

        omitted = len(s) - head - tail
        return f"{s[:head]} [<- {omitted:,} chars truncated ->] {s[-tail:]}"

    def sanitize_string(s: str) -> str:
        m = _DATA_URL_RE.match(s)
        if m:
            prefix1, mime_type, prefix3, payload = m.groups()
            mime_type = mime_type or "application/octet-stream"
            prefix = f"{prefix1}{mime_type}{prefix3}"
            return f"{prefix} [<- {len(payload):,} chars truncated ->]"

        return truncate_string(s)

    def walk(value):
        if isinstance(value, str):
            return sanitize_string(value)

        if isinstance(value, Mapping):
            return {k: walk(v) for k, v in value.items()}

        if isinstance(value, tuple):
            return tuple(walk(v) for v in value)

        if isinstance(value, Set) and not isinstance(value, (str, bytes, bytearray)):
            return {walk(v) for v in value}

        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [walk(v) for v in value]

        return value

    return walk(obj)


class XLogger:
    def __init__(self):
        self.seqlog_url = None
        self.headers = {}
        self.enabled = False

    def _get_timestamp_now(self):
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def setup(self, seqlog_url: str = "http://localhost:5341", api_key: str | None = None):
        self.seqlog_url = seqlog_url.rstrip("/")
        self.headers = {"Content-Type": "application/vnd.serilog.clef"}
        if api_key:
            self.headers["X-Seq-ApiKey"] = api_key

        # Check if seqlog is reachable
        try:
            r = requests.post(
                self.seqlog_url + "/ingest/clef",
                data=(f'{{"@t":"{self._get_timestamp_now()}","@m":"TabbyAPI startup probe"}}\n'),
                headers=self.headers,
                timeout=2,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            logger.info(
                f"Failed to initialize seqlog handler for server at "
                f"{self.seqlog_url}: {e}"
                f"seqlog logging is disabled."
            )
            return

        self.enabled = True
        logger.info(f"Enabled logging to seqlog instance at {self.seqlog_url}")

    def _commit(self, log_level: str, log_message: str, log_extra: dict):
        if not self.enabled:
            return

        try:
            if log_extra is None:
                log_extra = {}
            elif not isinstance(log_extra, dict):
                log_extra = {"extra": str(log_extra)}
            log_extra = _sanitize_for_logging(log_extra)
            event = {
                "@t": self._get_timestamp_now(),
                "@m": log_message,
                "@l": log_level,
                **log_extra,
            }
            try:
                data = json.dumps(event, default=str) + "\n"
            except Exception as e:
                data = "## Failed to serialize log data: " + str(e)
            r = requests.post(
                self.seqlog_url + "/ingest/clef",
                data=data,
                headers=self.headers,
                timeout=2,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"Failed to write log event to Seq, logging disabled: {e}")
            self.enabled = False

    def _compose(self, log_message, details):
        return (log_message + " " + details) if details else log_message

    def verbose(
        self,
        log_message: str,
        log_extra: dict | None = None,
        details: str | None = None,
    ):
        self._commit("Verbose", log_message, log_extra)

    def debug(
        self,
        log_message: str,
        log_extra: dict | None = None,
        details: str | None = None,
    ):
        logger.debug(self._compose(log_message, details))
        self._commit("Debug", log_message, log_extra)

    def info(
        self,
        log_message: str,
        log_extra: dict | None = None,
        details: str | None = None,
    ):
        logger.info(self._compose(log_message, details))
        self._commit("Information", log_message, log_extra)

    def warning(
        self,
        log_message: str,
        log_extra: dict | None = None,
        details: str | None = None,
    ):
        logger.warning(self._compose(log_message, details))
        self._commit("Warning", log_message, log_extra)

    def error(
        self,
        log_message: str,
        log_extra: dict | None = None,
        details: str | None = None,
    ):
        logger.error(self._compose(log_message, details))
        self._commit("Error", log_message, log_extra)


xlogger = XLogger()
