import importlib
import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch


ifq_stub = types.ModuleType("ifq")
ifq_stub.DownloadError = type("DownloadError", (Exception,), {})
ifq_stub.IssueNotAvailableError = type("IssueNotAvailableError", (Exception,), {})
ifq_stub.LoginError = type("LoginError", (Exception,), {})
ifq_stub.Scraper = object
sys.modules.setdefault("ifq", ifq_stub)

ifq_job = importlib.import_module("ifq_job")


@contextmanager
def fake_span():
    class _Span:
        def set_attribute(self, *_args, **_kwargs):
            pass

    yield _Span()


class _FakeTracer:
    def start_as_current_span(self, _name):
        return fake_span()


class IfqJobTests(unittest.TestCase):
    def test_archive_moves_only_files_older_than_keep_window(self):
        entries = [
            {".tag": "file", "path_display": "/inbox/ilfatto-20260603.pdf"},
            {".tag": "file", "path_display": "/inbox/ilfatto-20260604.pdf"},
            {".tag": "file", "path_display": "/inbox/notes.txt"},
        ]
        with (
            patch.object(ifq_job, "list_dropbox_files", return_value=entries),
            patch.object(ifq_job, "move_dropbox_file") as move_mock,
        ):
            moved = ifq_job.maybe_archive_old_dropbox_issues(
                token="token",
                source_root="/inbox",
                archive_root="/archive",
                keep_days=3,
                reference_day=ifq_job.date(2026, 6, 6),
            )

        self.assertEqual(moved, 1)
        move_mock.assert_called_once_with(
            "token", "/inbox/ilfatto-20260603.pdf", "/archive/ilfatto-20260603.pdf"
        )

    def test_cleanup_runs_even_when_daily_issue_already_exists(self):
        tracer = _FakeTracer()
        with (
            patch.object(ifq_job, "dropbox_issue_exists", return_value=True),
            patch.object(ifq_job, "run_ifq_download") as download_mock,
            patch.object(ifq_job, "upload_to_dropbox") as upload_mock,
            patch.object(
                ifq_job,
                "maybe_archive_old_dropbox_issues",
                return_value=2,
            ) as cleanup_mock,
        ):
            ifq_job.run_once(
                tracer=tracer,
                run_id="run-id",
                issue_date="2026-06-06",
                ifq_username="user",
                ifq_password="pass",
                dropbox_token="token",
                dropbox_root="/inbox",
                archive_root="/archive",
                keep_days=7,
                requested_day=ifq_job.date(2026, 6, 6),
            )

        download_mock.assert_not_called()
        upload_mock.assert_not_called()
        cleanup_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
