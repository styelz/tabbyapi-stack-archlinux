import logging
import unittest
from unittest import mock

from common.logger import (
    UvicornLoggingHandler,
    console_width,
    is_hidden_journal_line,
    is_ui_access_line,
)


class LoggerWidthTests(unittest.TestCase):
    def test_env_overrides_tty(self):
        self.assertEqual(console_width("200", isatty=True), 200)
        self.assertEqual(console_width("200", isatty=False), 200)

    def test_non_tty_defaults_wide(self):
        self.assertEqual(console_width("", isatty=False), 256)
        self.assertEqual(console_width(None, isatty=False), 256)

    def test_tty_auto_width(self):
        self.assertIsNone(console_width(None, isatty=True))
        self.assertIsNone(console_width("0", isatty=True))
        self.assertEqual(console_width("0", isatty=False), 256)


class UiAccessLogTests(unittest.TestCase):
    def test_status_poll_is_ui_access(self):
        line = (
            "Aug 24 06:08:12 archy.local python[122943]: "
            "2026-08-24 06:08:12.392 INFO:     36.255.114.172:0 - "
            '"GET /v1/ui/status HTTP/1.1" 200'
        )
        self.assertTrue(is_ui_access_line(line))
        self.assertTrue(is_ui_access_line(
            '127.0.0.1:56946 - "GET /v1/model HTTP/1.1" 200'
        ))
        self.assertTrue(is_ui_access_line('"GET /openai/v1/model HTTP/1.1" 200'))
        self.assertFalse(is_ui_access_line('"GET /v1/models HTTP/1.1" 200'))
        self.assertFalse(is_ui_access_line('"POST /v1/model/load HTTP/1.1" 200'))
        self.assertFalse(is_ui_access_line('"GET /v1/chat/completions HTTP/1.1" 200'))

    def test_sse_chunk_echo_is_hidden(self):
        self.assertTrue(
            is_hidden_journal_line("2026-08-30 07:38:13.030 DEBUG:    chunk: b'event: log\\r\\ndata: {\"line\": \"hi\"}'")
        )
        self.assertTrue(is_hidden_journal_line("x" * 4001))
        self.assertTrue(is_hidden_journal_line("keep " + ("\\" * 40)))
        self.assertFalse(is_hidden_journal_line("Model loaded: qwen"))
        self.assertTrue(
            is_hidden_journal_line("ERROR:    Sent to request: No models are currently loaded.")
        )

    def test_handler_drops_ui_status_access(self):
        handler = UvicornLoggingHandler()
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='36.255.114.172:0 - "GET /v1/ui/status HTTP/1.1" 200',
            args=(),
            exc_info=None,
        )
        with mock.patch("common.logger.logger") as log:
            handler.emit(record)
        log.opt.assert_not_called()

    def test_handler_drops_model_poll_access(self):
        handler = UvicornLoggingHandler()
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='127.0.0.1:56946 - "GET /v1/model HTTP/1.1" 200',
            args=(),
            exc_info=None,
        )
        with mock.patch("common.logger.logger") as log:
            handler.emit(record)
        log.opt.assert_not_called()

    def test_handler_keeps_other_access(self):
        handler = UvicornLoggingHandler()
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='36.255.114.172:0 - "POST /v1/chat/completions HTTP/1.1" 200',
            args=(),
            exc_info=None,
        )
        with mock.patch("common.logger.logger") as log:
            handler.emit(record)
        log.opt.assert_called_once()
