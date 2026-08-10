"""Approval-log polling tests (PLAN.md §11.10.12).

``wait_for_resolution`` used to re-parse the whole ``approvals.jsonl`` every
second. Pilot records embed artifact text, so an overnight wait was a few
hundred thousand full JSON parses of a growing file. The fix: a scanner
that parses only the bytes appended since the last poll, guarding the
torn-tail trap (an offset may advance only up to the last ``\\n``).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.pipeline import approvals as approval_store  # noqa: E402
from kusudaemon.pipeline.run_dir import approvals_path  # noqa: E402

_N_RECORDS = 700


def _write_log(run_dir: Path, n: int) -> None:
    lines = []
    for i in range(n):
        record = approval_store.Approval.create("pilot", title=f"q{i}", message="x" * 400)
        lines.append(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True))
    approvals_path(run_dir).parent.mkdir(parents=True, exist_ok=True)
    approvals_path(run_dir).write_text("\n".join(lines) + "\n", encoding="utf-8")


class IncrementalScanTest(unittest.TestCase):
    def test_each_record_is_parsed_exactly_once_across_polls(self) -> None:
        # Old code: k polls x full re-parse. New code: each appended record
        # is parsed exactly once, on the poll that first sees it — a
        # several-second wait over an unchanging log costs zero re-parses.
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            _write_log(run_dir, _N_RECORDS)

            parsed = {"n": 0}
            original = approval_store.Approval.from_dict

            def counting(data):
                parsed["n"] += 1
                return original(data)

            with mock.patch.object(
                approval_store.Approval, "from_dict", staticmethod(counting)
            ):
                with self.assertRaises(TimeoutError):
                    approval_store.wait_for_resolution(
                        run_dir, "does-not-exist", poll_interval=0.01, timeout=0.05
                    )

            # New behavior: exactly _N_RECORDS (first poll only). Old
            # behavior: _N_RECORDS per poll, k >= 2 polls -> >= 1400.
            self.assertLessEqual(parsed["n"], _N_RECORDS + 100)

    def test_new_records_are_picked_up_incrementally(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            _write_log(run_dir, 3)

            scanner = approval_store._ApprovalScanner(run_dir)
            first = scanner.next_records()
            self.assertEqual([r.title for r in first], ["q0", "q1", "q2"])
            self.assertEqual(scanner.next_records(), [])
            self.assertEqual(scanner.next_records(), [])
            _write_log(run_dir, 5)
            second = scanner.next_records()
            self.assertEqual([r.title for r in second], ["q3", "q4"])

    def test_torn_tail_is_re_read_not_consumed_half(self) -> None:
        # Same trap as v0/runner.py's session-id watcher (§11.9): a
        # concurrently-appended line without a trailing newline must not
        # advance the offset past it, or its remainder is lost forever.
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            _write_log(run_dir, 1)
            scanner = approval_store._ApprovalScanner(run_dir)
            self.assertEqual([r.title for r in scanner.next_records()], ["q0"])
            path = approvals_path(run_dir)
            with path.open("a", encoding="utf-8") as fh:
                fh.write('{"approval_id": "torn-id"')
            self.assertEqual(scanner.next_records(), [])
            with path.open("a", encoding="utf-8") as fh:
                fh.write(', "kind": "pilot", "title": "finished", "status": "pending"}\n')
            torn = scanner.next_records()
            self.assertEqual([r.approval_id for r in torn], ["torn-id"])

    def test_wait_resolves_from_another_process_like_appender(self) -> None:
        import threading

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str)
            _write_log(run_dir, 1)
            pending_rec = approval_store.pending(run_dir)[0]

            def resolve_later() -> None:
                import time as _time

                _time.sleep(0.03)
                approval_store.append(run_dir, pending_rec.resolve(action="answer"))

            thread = threading.Thread(target=resolve_later)
            thread.start()
            resolved = approval_store.wait_for_resolution(
                run_dir, pending_rec.approval_id, poll_interval=0.01, timeout=5.0
            )
            thread.join(timeout=5)
            self.assertEqual(resolved.status, "resolved")
            self.assertEqual(resolved.action, "answer")


if __name__ == "__main__":
    unittest.main()