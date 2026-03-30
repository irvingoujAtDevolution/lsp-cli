from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from solidlsp.ls_config import Language

from lsp_cli.daemon import _extract_location_path, _filter_symbol_tree
from lsp_cli.daemon_state import release_startup_lock, try_acquire_startup_lock
from lsp_cli.observability import read_events
from lsp_cli.session import (
    Session,
    SessionManager,
    SessionStatus,
    _normalize_progress_notification,
    _soften_startup_barriers,
)


class DaemonHelpersTest(unittest.TestCase):
    def test_extract_location_path_uses_uri_when_relative_path_missing(self) -> None:
        loc = {
            "relativePath": None,
            "uri": "file:///D:/lsp-cli/lsp_cli/daemon.py",
        }

        self.assertEqual(
            _extract_location_path(loc, r"D:\lsp-cli"),
            "lsp_cli/daemon.py",
        )

    def test_filter_symbol_tree_keeps_matching_descendants(self) -> None:
        symbols = [
            {
                "name": "pkg",
                "children": [
                    {
                        "name": "module",
                        "children": [
                            {"name": "target_symbol"},
                            {"name": "other_symbol"},
                        ],
                    }
                ],
            }
        ]

        filtered = _filter_symbol_tree(symbols, "target")

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["name"], "pkg")
        self.assertEqual(filtered[0]["children"][0]["name"], "module")
        self.assertEqual(filtered[0]["children"][0]["children"], [{"name": "target_symbol"}])

    def test_filter_symbol_tree_drops_non_matching_branches(self) -> None:
        symbols = [{"name": "pkg", "children": [{"name": "module"}]}]

        self.assertEqual(_filter_symbol_tree(symbols, "missing"), [])

    def test_startup_lock_is_exclusive_until_released(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon_dir = Path(tmp)
            lock_file = daemon_dir / "daemon.start.lock"
            with (
                patch("lsp_cli.daemon_state.DAEMON_DIR", daemon_dir),
                patch("lsp_cli.daemon_state.STARTUP_LOCK_FILE", lock_file),
            ):
                first_fd = try_acquire_startup_lock()
                self.assertIsNotNone(first_fd)
                self.assertIsNone(try_acquire_startup_lock())
                release_startup_lock(first_fd)
                second_fd = try_acquire_startup_lock()
                self.assertIsNotNone(second_fd)
                release_startup_lock(second_fd)

    def test_read_events_filters_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_file = Path(tmp) / "events.jsonl"
            events_file.write_text(
                "\n".join(
                    [
                        '{"event":"a","value":1}',
                        '{"event":"b","value":2}',
                        '{"event":"a","value":3}',
                    ]
                ),
                encoding="utf-8",
            )
            with patch("lsp_cli.observability.EVENTS_FILE", events_file):
                self.assertEqual(read_events(limit=2), [{"event": "b", "value": 2}, {"event": "a", "value": 3}])
                self.assertEqual(read_events(limit=5, event="a"), [{"event": "a", "value": 1}, {"event": "a", "value": 3}])

    def test_soften_startup_barriers_times_out_wait(self) -> None:
        class FakeRustServer:
            language = Language.RUST
            repository_root_path = r"D:\repo"

            def __init__(self) -> None:
                self.server_ready = threading.Event()

        server = FakeRustServer()
        original_wait = server.server_ready.wait

        _soften_startup_barriers(server)  # type: ignore[arg-type]

        self.assertIsNot(server.server_ready.wait, original_wait)
        self.assertFalse(server.server_ready.wait())

    def test_soften_startup_barriers_wraps_generic_analysis_event(self) -> None:
        class FakePythonServer:
            language = Language.PYTHON
            repository_root_path = r"D:\repo"

            def __init__(self) -> None:
                self.analysis_complete = threading.Event()

        server = FakePythonServer()
        original_wait = server.analysis_complete.wait

        _soften_startup_barriers(server)  # type: ignore[arg-type]

        self.assertIsNot(server.analysis_complete.wait, original_wait)
        self.assertFalse(server.analysis_complete.wait())

    def test_soften_startup_barriers_skips_non_event_attrs(self) -> None:
        class FakePythonServer:
            language = Language.PYTHON
            repository_root_path = r"D:\repo"

            def __init__(self) -> None:
                self.server_ready = "not-an-event"

        server = FakePythonServer()
        _soften_startup_barriers(server)  # type: ignore[arg-type]

        self.assertEqual(server.server_ready, "not-an-event")


    def test_normalize_progress_notification_handles_progress_report(self) -> None:
        progress = _normalize_progress_notification(
            "$/progress",
            {
                "token": "1",
                "value": {
                    "kind": "report",
                    "title": "Indexing workspace",
                    "message": "Loading crate graph",
                    "percentage": 42,
                },
            },
            r"D:\repo",
        )

        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress["phase"], "loading_workspace")
        self.assertEqual(progress["percentage"], 42)
        self.assertEqual(progress["source"], "lsp-progress")

    def test_normalize_progress_notification_extracts_counts(self) -> None:
        progress = _normalize_progress_notification(
            "$/progress",
            {
                "token": "rustAnalyzer/Roots Scanned",
                "value": {
                    "kind": "report",
                    "message": "223/716",
                    "percentage": 31,
                },
            },
            r"D:\repo",
        )

        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress["phase"], "indexing")
        self.assertEqual(progress["completed"], 223)
        self.assertEqual(progress["total"], 716)

    def test_normalize_progress_notification_extracts_diagnostics_file(self) -> None:
        progress = _normalize_progress_notification(
            "textDocument/publishDiagnostics",
            {"uri": "file:///D:/repo/src/main.rs", "diagnostics": []},
            r"D:\repo",
        )

        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress["current_file"], "src/main.rs")
        self.assertEqual(progress["phase"], "analyzing")

    def test_session_to_dict_includes_progress(self) -> None:
        session = Session(
            name="gateway",
            root_path=r"D:\repo",
            language=Language.RUST,
            status=SessionStatus.WARM,
        )
        session._progress.phase = "indexing"
        session._progress.message = "Loading workspace"
        session._progress.source = "lsp-progress"
        session._progress.last_update_time = 1.0

        data = session.to_dict(include_progress_raw=True)

        self.assertIn("progress", data)
        self.assertEqual(data["progress"]["phase"], "indexing")
        self.assertEqual(data["progress"]["message"], "Loading workspace")

    def test_start_session_rejects_ambiguous_csharp_solutions_before_async_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "A.sln").write_text("", encoding="utf-8")
            Path(tmp, "B.sln").write_text("", encoding="utf-8")
            manager = SessionManager()

            with self.assertRaises(RuntimeError):
                manager.start_session("rdm", tmp, "csharp")

            self.assertEqual(manager.list_sessions(), [])

    def test_list_sessions_tolerates_serialization_failure(self) -> None:
        manager = SessionManager()
        session = Session(
            name="broken",
            root_path=r"D:\repo",
            language=Language.RUST,
            status=SessionStatus.WARM,
        )

        def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("serialize failed")

        session.to_dict = boom  # type: ignore[method-assign]
        manager._sessions[session.name] = session

        result = manager.list_sessions()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "broken")
        self.assertEqual(result[0]["status"], "error")
        self.assertIn("serialize failed", result[0]["error"])

    def test_find_session_for_path_accepts_warm_sessions(self) -> None:
        manager = SessionManager()
        session = Session(
            name="gateway",
            root_path=r"D:\devolutions-gateway\devolutions-gateway",
            language=Language.RUST,
            status=SessionStatus.WARM,
        )
        manager._sessions[session.name] = session

        resolved = manager.find_session_for_path(r"D:\devolutions-gateway\devolutions-gateway\src\config.rs")

        self.assertIs(resolved, session)


if __name__ == "__main__":
    unittest.main()
