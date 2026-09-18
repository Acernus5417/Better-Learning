"""The transcription watchdog: metadata-only nudges, never a scheduler."""
import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'better-learning/scripts'))

from _common import read_json, write_json  # noqa: E402
import watchdog  # noqa: E402
import convert_materials as cm  # noqa: E402


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.course = Path(self.tmp.name) / 'course'
        (self.course / '_工作区').mkdir(parents=True)
        write_json(self.course / cm.LEDGER, {
            'schema_version': 3, 'engine': 'host-subagent', 'max_concurrent': 4,
            'max_attempts': 3, 'current_batch_no': 2,
            'sources': [{'id': 'SRC-001', 'sha256': 'x', 'path': 'x', 'units': [
                {'id': 'U00001', 'processing_route': 'vision', 'status': 'running',
                 'attempt_count': 1, 'blocked': False}],
                'batches': [{'id': 'B01', 'units': ['U00001'], 'active_member': 'vis_1'}]}],
            'attempts': [{'id': 'A-1', 'state': 'running', 'staging_dir': '_工作区/转写尝试/A-1',
                          'started_at': '2026-01-01T00:00:00+00:00', 'host_agent_id': 'member-1',
                          'dispatch_batch_no': 2}]})

    def tearDown(self):
        self.tmp.cleanup()

    def test_inspect_reports_only_metadata(self):
        report = watchdog.inspect(self.course)
        self.assertEqual(report['active'], 1)
        self.assertEqual(report['batch_no'], 2)
        self.assertTrue(report['members'][0]['attempt'] == 'A-1')
        payload = json.dumps(report, ensure_ascii=False)
        self.assertNotIn('transcript', payload)

    def test_reminder_text_and_persistence(self):
        report = watchdog.inspect(self.course)
        message = watchdog.render(report)
        self.assertIn(watchdog.NUDGE_TEXT, message)
        watchdog._append_reminder(self.course, message, report)
        rows = [json.loads(line) for line in
                (self.course / watchdog.REMINDER_FILE).read_text('utf-8').splitlines() if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertIn(watchdog.NUDGE_TEXT, rows[0]['message'])

    def test_status_and_stop_are_safe_without_process(self):
        self.assertFalse(watchdog.is_running(self.course))
        self.assertFalse(watchdog.status(self.course)['running'])
        outcome = watchdog.stop(self.course)
        self.assertTrue(outcome['stopped'])
        self.assertEqual(outcome['reason'], 'no_pid')
        self.assertTrue(watchdog.stop(self.course)['stopped'])

    def test_inspect_never_touches_the_ledger(self):
        before = (self.course / cm.LEDGER).read_text('utf-8')
        watchdog.inspect(self.course)
        watchdog.inspect(self.course)
        self.assertEqual(before, (self.course / cm.LEDGER).read_text('utf-8'))


if __name__ == '__main__':
    unittest.main()
