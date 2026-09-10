"""Models page download banner. Keep in sync with ui/static/models.js."""

from __future__ import annotations

import unittest
from pathlib import Path

MODELS_JS = Path(__file__).resolve().parents[1] / "ui" / "static" / "models.js"


class ModelsJobBannerTests(unittest.TestCase):
    def setUp(self):
        self.src = MODELS_JS.read_text(encoding="utf-8")

    def test_stale_finished_jobs_are_not_painted(self):
        self.assertIn("function shouldPaintJob(job)", self.src)
        self.assertIn("function jobIsFresh(job)", self.src)
        self.assertIn("JOB_DONE_TTL_MS = 90 * 1000", self.src)
        self.assertIn("tabby-models-dismissed-job", self.src)
        self.assertIn("Dismiss", self.src)
        self.assertIn('dataset.action === "dismiss"', self.src)
        self.assertIn("function dismissPaintedJob()", self.src)
        self.assertNotIn('showOk(data.job.message || "Download finished")', self.src)
        self.assertNotIn('showOk(job.message || "Download finished")', self.src)

    def test_busy_jobs_still_show_byte_progress(self):
        paint = self.src.split("function paintJob(job)")[1].split("function libraryRows")[0]
        self.assertIn("if (jobBusy(job))", paint)
        self.assertIn("job.bytes_total", paint)
        self.assertIn("formatBytes", paint)
        self.assertIn('jobCancel.textContent = busy ? "Cancel" : "Dismiss"', paint)
