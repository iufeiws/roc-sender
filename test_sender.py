import unittest
import ctypes as C
from unittest.mock import Mock
from array import array
from sender import (CaptureHealth, CaptureQueue, FeedbackHealth, Roc, SenderMetrics,
                    capture_devices, select_device, validate_endpoint, parser, settings)


class CaptureTests(unittest.TestCase):
    def test_callback_preserves_pcm_and_variable_frame_counts_in_order(self):
        capture = CaptureQueue(2, 3, 0, 2)
        blocks = [(bytes(range(24)), 3), (bytes(range(40)), 5)]
        for data, count in blocks:
            capture.callback(data, count, {}, 0)
        self.assertEqual([capture.read(0), capture.read(0)], blocks)
        self.assertEqual(capture.frames, 8)
        self.assertIsNone(capture.read(0))

    def test_slow_consumer_fails_instead_of_discarding_or_overwriting(self):
        capture = CaptureQueue(1, 1, 0, 2)
        capture.callback(b'1234', 1, {}, 0)
        self.assertEqual(capture.callback(b'5678', 1, {}, 0)[1], 2)
        self.assertEqual(capture.frames, 1)
        with self.assertRaises(RuntimeError):
            capture.read(0)

    def test_driver_error_reaches_consumer_even_without_audio(self):
        capture = CaptureQueue(2, 2, 0, 2)
        self.assertEqual(capture.callback(None, 0, {}, 2)[1], 2)
        with self.assertRaises(RuntimeError):
            capture.read(0)

    def test_incomplete_block_fails_before_sending(self):
        capture = CaptureQueue(2, 2, 0, 2)
        capture.callback(b'1234', 2, {}, 0)
        with self.assertRaises(RuntimeError):
            capture.read(0)

    def test_callback_exception_is_reraised_by_consumer(self):
        capture = CaptureQueue(2, 2, 0, 2)
        original = MemoryError()
        capture.blocks.put_nowait = Mock(side_effect=original)
        result = capture.callback(bytes(8), 1, {}, 0)
        self.assertEqual(result[1], 2)
        with self.assertRaises(MemoryError) as caught:
            capture.read(0)
        self.assertIs(caught.exception, original)

    def test_callback_abort_during_activity_check_preserves_cause(self):
        capture = CaptureQueue(2, 2, 0, 2)
        original = ValueError()

        def become_inactive():
            capture.error = original
            return False

        with self.assertRaises(ValueError) as caught:
            capture.check_active(Mock(is_active=become_inactive))
        self.assertIs(caught.exception, original)

    def test_native_stop_fails_without_restart(self):
        capture = CaptureQueue(2, 2, 0, 2)
        stream = Mock()
        stream.is_active.return_value = False
        with self.assertRaises(RuntimeError):
            capture.check_active(stream)
        stream.start_stream.assert_not_called()

    def test_health_windows_separate_silent_pcm_and_missing_callbacks(self):
        capture = CaptureQueue(1, 2, 0, 2)
        health = CaptureHealth(10)
        audible = array('f', [0.5, -0.25]).tobytes()
        capture.callback(audible, 2, {}, 0)
        data, count = capture.read(0)
        health.observe(data)
        first = health.snapshot(capture, count, 11)
        self.assertEqual((first['captured'], first['sent'], first['peak']), (2, 2, 0.5))
        capture.callback(bytes(8), 2, {}, 0)
        data, count = capture.read(0)
        health.observe(data)
        silent = health.snapshot(capture, 4, 12)
        self.assertEqual((silent['captured'], silent['sent'], silent['peak']), (2, 2, 0.0))
        absent = health.snapshot(capture, 4, 13)
        self.assertEqual((absent['captured'], absent['sent']), (0, 0))
        self.assertEqual(absent['callback_age'] - silent['callback_age'], 1)


class FeedbackTests(unittest.TestCase):
    active = {"sent": 48000, "callback_age": 0.01}

    def test_wait_loss_and_recovery_warn_only_on_transitions(self):
        health = FeedbackHealth(0)
        self.assertFalse(health.snapshot(0, [], self.active, 1)[1])
        waiting, warnings = health.snapshot(0, [], self.active, 10)
        self.assertEqual(waiting['state'], 'waiting')
        self.assertEqual(len(warnings), 1)
        self.assertFalse(health.snapshot(0, [], self.active, 11)[1])
        available, warnings = health.snapshot(1, [15.0], self.active, 12)
        self.assertEqual(available['state'], 'feedback')
        self.assertEqual(len(warnings), 1)
        self.assertFalse(health.snapshot(1, [15.0], self.active, 13)[1])
        lost, warnings = health.snapshot(0, [], self.active, 14)
        self.assertEqual(lost['state'], 'lost')
        self.assertEqual(len(warnings), 1)
        self.assertFalse(health.snapshot(0, [], self.active, 15)[1])
        restored, warnings = health.snapshot(1, [16.0], self.active, 16)
        self.assertEqual(restored['state'], 'feedback')
        self.assertEqual(len(warnings), 1)

    def test_no_pcm_cannot_refresh_remote_feedback_or_latency(self):
        health = FeedbackHealth(0)
        health.snapshot(1, [12.0], self.active, 1)
        for local in ({'sent': 0, 'callback_age': 0.1},
                      {'sent': 20, 'callback_age': 1.1},
                      {'sent': 0, 'callback_age': None}):
            remote, warnings = health.snapshot(1, [12.0], local, 20)
            self.assertEqual(remote['state'], 'unverified')
            self.assertEqual(remote['latencies_ms'], [None])
            self.assertFalse(warnings)
        remote, warnings = health.snapshot(0, [], self.active, 21)
        self.assertEqual(remote['state'], 'lost')
        self.assertEqual(len(warnings), 1)

    def test_monitor_window_does_not_reset_longer_local_stats(self):
        capture = CaptureQueue(1, 1, 0, 2)
        capture.callback(array('f', [0.5]).tobytes(), 1, {}, 0)
        data, frames = capture.read(0)
        cli, monitor = CaptureHealth(0), CaptureHealth(0)
        block_peak = cli.observe(data)
        monitor.peak = max(monitor.peak, block_peak)
        monitor.snapshot(capture, frames, 1)
        self.assertEqual(cli.snapshot(capture, frames, 10)['peak'], 0.5)

    def test_query_converts_measured_latency_but_zero_remains_unknown(self):
        roc = Roc.__new__(Roc)

        def query(name, sender, slot, metrics, connections, count):
            C.cast(metrics, C.POINTER(SenderMetrics)).contents.connection_count = 1
            C.cast(count, C.POINTER(C.c_size_t)).contents.value = 1
            connections[0].e2e_latency = latency

        roc.call = query
        for latency, expected in ((0, None), (12_500_000, 12.5)):
            self.assertEqual(roc.query(None), (1, [expected]))

    def test_query_failure_is_not_reported_as_waiting(self):
        roc = Roc.__new__(Roc)
        roc.dll = Mock()
        roc.dll.roc_sender_query.return_value = -1
        with self.assertRaises(RuntimeError):
            roc.query(None)

    def test_unexpected_receiver_count_is_not_silently_truncated(self):
        roc = Roc.__new__(Roc)

        def query(name, sender, slot, metrics, connections, count):
            C.cast(metrics, C.POINTER(SenderMetrics)).contents.connection_count = 2

        roc.call = query
        with self.assertRaises(RuntimeError):
            roc.query(None)


class SelectionTests(unittest.TestCase):
    devices = [{"name": "CABLE Input [Loopback]", "index": 4},
               {"name": "Speakers [Loopback]", "index": 5}]

    def test_select_different_output(self):
        self.assertEqual(select_device(self.devices, "speakers")["index"], 5)

    def test_missing_output_does_not_capture_another(self):
        with self.assertRaises(ValueError):
            select_device(self.devices, "Missing")

    def test_ambiguous_output_requires_selection(self):
        with self.assertRaises(ValueError):
            select_device(self.devices, "Loopback")
        self.assertEqual(select_device(self.devices, index=4)["index"], 4)

    def test_receiver_endpoints_support_ipv6(self):
        validate_endpoint("rtp+rs8m://[::1]:10001", "rtp+rs8m")

    def test_incompatible_protocol_rejected_before_capture(self):
        with self.assertRaises(ValueError):
            validate_endpoint("rtp://localhost:10001", "rtp+rs8m")

    def test_receiver_is_required_not_implicitly_chosen(self):
        with self.assertRaises(ValueError):
            settings(parser().parse_args([]))

    def test_input_selection_excludes_mme_and_loopback(self):
        devices = [
            {"name": "CABLE Output", "index": 1, "hostApi": 0,
             "maxInputChannels": 2, "isLoopbackDevice": False},
            {"name": "CABLE Output", "index": 2, "hostApi": 3,
             "maxInputChannels": 2, "isLoopbackDevice": False},
            {"name": "CABLE Input", "index": 3, "hostApi": 3,
             "maxInputChannels": 2, "isLoopbackDevice": True},
        ]
        inputs = capture_devices(devices, 3, "input")
        self.assertEqual(select_device(inputs, "CABLE Output")["index"], 2)
        with self.assertRaises(ValueError):
            select_device(inputs, index=1)
        with self.assertRaises(ValueError):
            select_device(inputs, index=3)
        loops = capture_devices(devices, 3, "loopback")
        self.assertEqual(select_device(loops, "CABLE Input")["index"], 3)
        with self.assertRaises(ValueError):
            select_device(loops, index=2)


if __name__ == "__main__":
    unittest.main()
