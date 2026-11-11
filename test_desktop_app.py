"""Desktop behavior tests; no audio stream, registry mutation or receiver needed."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication
import desktop_app as gui


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.object(gui, 'startup_enabled', return_value=False):
            self.window = gui.Window(Path(self.temp.name) / 'settings.json', enumerate_devices=False)
        self.window.tray.hide()

    def tearDown(self):
        if self.window.process.state() != QProcess.ProcessState.NotRunning:
            self.window.process.kill()
            self.window.process.waitForFinished(2000)
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def wait_for(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(predicate())

    def test_configuration_roundtrip_preserves_device_and_destinations(self):
        self.window.device.clear()
        self.window.device.addItem('Speakers (USB Audio) [Loopback]')
        self.window.endpoints['source'].setText('rtp+rs8m://example.local:12001')
        self.window.chunk.setValue(441)
        saved = self.window.save()
        self.assertEqual(saved, json.loads(self.window.settings_path.read_text(encoding='utf-8')))
        with patch.object(gui, 'startup_enabled', return_value=False):
            other = gui.Window(self.window.settings_path, enumerate_devices=False)
        self.assertEqual(other.values(), saved)
        other.tray.hide()
        other.deleteLater()

    def test_invalid_destination_does_not_replace_saved_settings(self):
        self.window.save()
        original = self.window.settings_path.read_bytes()
        self.window.endpoints['source'].setText('rtp+rs8m://host:99999')
        with self.assertRaises(ValueError):
            self.window.save()
        self.assertEqual(original, self.window.settings_path.read_bytes())

    def test_normal_stop_uses_stdin_and_waits_for_child_cleanup(self):
        child = "import sys,time; print('ready',flush=True); sys.stdin.readline(); time.sleep(.1); print('cleaned',flush=True)"
        with patch.object(gui, 'worker_command', return_value=[sys.executable, '-u', '-c', child]):
            self.window.start()
        self.wait_for(lambda: 'ready' in self.window.log.toPlainText())
        self.assertFalse(self.window.config_group.isEnabled())
        self.window.stop()
        self.wait_for(lambda: self.window.process.state() == QProcess.ProcessState.NotRunning)
        self.assertEqual(self.window.process.exitCode(), 0)
        self.assertIn('cleaned', self.window.log.toPlainText())
        self.assertTrue(self.window.config_group.isEnabled())

    def test_child_failure_reenables_start_and_surfaces_diagnostics(self):
        child = "import sys; print('audio failure',flush=True); sys.exit(2)"
        with patch.object(gui, 'worker_command', return_value=[sys.executable, '-u', '-c', child]):
            self.window.start()
        self.wait_for(lambda: self.window.start_button.isEnabled())
        self.assertEqual(self.window.process.exitCode(), 2)
        self.assertIn('audio failure', self.window.log.toPlainText())

    def test_worker_arguments_use_existing_sender_parser(self):
        values = self.window.values()
        command = gui.worker_command(values)
        args = gui.sender.parser().parse_args(command[2:])
        settings = gui.sender.settings(args)
        self.assertEqual(settings['device'], values['device'])
        self.assertEqual(settings['source'], values['source'])
        self.assertTrue(args.stop_stdin)

    def test_constructing_window_does_not_change_windows_startup(self):
        with patch.object(gui, 'set_startup') as change, patch.object(gui, 'startup_enabled', return_value=True):
            other = gui.Window(Path(self.temp.name) / 'another.json', enumerate_devices=False)
        change.assert_not_called()
        other.tray.hide()
        other.deleteLater()

    def monitor_sample(self, remote_state='feedback'):
        return {
            'local': dict(state='receiving', captured_frames=44100, sent_frames=44100,
                          elapsed_s=1.0, peak=0.2, queue_blocks=0, callback_age_s=0.01),
            'remote': dict(state=remote_state, receiver_count=1 if remote_state == 'feedback' else 0,
                           latencies_ms=[120.0] if remote_state == 'feedback' else []),
            'playback': 'unknown',
        }

    def test_feedback_loss_notifies_once_and_can_recover(self):
        with patch.object(self.window.tray, 'showMessage') as notify:
            self.window.update_monitor(self.monitor_sample())
            notify.assert_not_called()
            self.window.update_monitor(self.monitor_sample('lost'))
            self.window.update_monitor(self.monitor_sample('lost'))
            self.assertEqual(notify.call_count, 1)
            self.window.update_monitor(self.monitor_sample())
            self.assertEqual(self.window.monitor_data['remote']['receiver_count'], 1)
            self.window.update_monitor(self.monitor_sample('lost'))
            self.assertEqual(notify.call_count, 2)

    def test_monitor_stops_presenting_stale_feedback(self):
        self.window.update_monitor(self.monitor_sample())
        self.window.monitor_received_at -= 5
        with patch.object(self.window.process, 'state', return_value=QProcess.ProcessState.Running):
            self.window.check_monitor_freshness()
        self.assertIsNone(self.window.monitor_data)
        self.assertIsNone(self.window.monitor_received_at)

    def test_process_exit_clears_previous_feedback(self):
        self.window.update_monitor(self.monitor_sample())
        self.window.finished(0, QProcess.ExitStatus.NormalExit)
        self.assertIsNone(self.window.monitor_data)


if __name__ == '__main__':
    unittest.main()
