"""Native Windows WASAPI input/loopback -> libroc RS8M/RTCP sender."""
from __future__ import annotations

import argparse
import ctypes as C
import json
from contextlib import ExitStack
from pathlib import Path
import os
from queue import Queue, Empty, Full
import signal
import sys
import threading
import time
import tomllib
from urllib.parse import urlsplit


class CaptureQueue:
    """Keep the audio callback independent of encoding/network work."""

    def __init__(self, channels, capacity, continue_code, abort_code):
        self.channels = channels
        self.blocks = Queue(maxsize=capacity)
        self.continue_code, self.abort_code = continue_code, abort_code
        self.error = None
        self.frames = 0
        self.high_water = 0
        self.last_callback = None
        self.last_status = 0

    def callback(self, data, frame_count, time_info, status):
        # PortAudio runs this on a native thread. Preserve the actual exception
        # rather than allowing the consumer to see only an inactive stream.
        self.last_callback = time.monotonic()
        self.last_status = status
        try:
            return self._accept(data, frame_count, status)
        except Exception as error:
            self.error = error
            return None, self.abort_code

    def _accept(self, data, frame_count, status):
        # Errors cross the callback boundary explicitly; never silently drop PCM.
        if self.error is not None:
            return None, self.abort_code
        if status:
            self.error = RuntimeError(f"WASAPI capture status {status}: audio continuity lost")
        elif data is None or len(data) != frame_count * self.channels * 4:
            self.error = RuntimeError("WASAPI returned an incomplete audio block")
        else:
            try:
                self.blocks.put_nowait((data, frame_count))
            except Full:
                self.error = RuntimeError("Capture queue full: sender cannot keep up; audio continuity lost")
            else:
                self.frames += frame_count
                self.high_water = max(self.high_water, self.blocks.qsize())
        return None, self.abort_code if self.error else self.continue_code

    def check(self):
        if self.error is not None:
            raise self.error

    def check_active(self, stream):
        self.check()
        active = stream.is_active()
        # A callback can abort while is_active() releases the GIL.
        self.check()
        if not active:
            age = ("never" if self.last_callback is None else
                   f"{time.monotonic() - self.last_callback:.3f}s ago")
            raise RuntimeError("WASAPI capture stopped unexpectedly; "
                               f"last callback {age}, last status {self.last_status}, "
                               f"captured {self.frames} frames, "
                               f"queue {self.blocks.qsize()}/{self.blocks.maxsize}. "
                               "The installed PyAudioWPatch binding does not expose the native "
                               "WASAPI stop HRESULT; the cause is not yet known.")

    def read(self, timeout=0.1):
        self.check()
        try:
            block = self.blocks.get(timeout=timeout)
        except Empty:
            self.check()
            return None
        self.check()
        return block


class CaptureHealth:
    """Windowed observations distinguish silent PCM from missing callbacks."""

    def __init__(self, now, captured=0, sent=0):
        self.started = now
        self.captured = captured
        self.sent = sent
        self.peak = 0.0

    def observe(self, data):
        peak = max((abs(value) for value in memoryview(data).cast("f")), default=0.0)
        self.peak = max(self.peak, peak)
        return peak

    def snapshot(self, capture, sent, now):
        captured = capture.frames
        result = {
            "elapsed": now - self.started,
            "captured": captured - self.captured,
            "sent": sent - self.sent,
            "peak": self.peak,
            "queue": capture.blocks.qsize(),
            "callback_age": None if capture.last_callback is None else now - capture.last_callback,
        }
        self.started, self.captured, self.sent = now, captured, sent
        self.peak = 0.0
        return result


class FeedbackHealth:
    """Report Roc's feedback observation, without claiming remote playback."""

    def __init__(self, now):
        self.started = now
        self.seen = False
        self.wait_warned = False
        self.last_verified = "waiting"

    def snapshot(self, receiver_count, latencies_ms, local, now):
        warnings = []
        # Roc expires feedback during sender_write. Without current PCM, a query
        # can retain old feedback indefinitely and is not a liveness check.
        progressing = (local["sent"] > 0 and local["callback_age"] is not None
                       and local["callback_age"] <= 1.0)
        if not progressing:
            state = "unverified"
            latencies_ms = [None for _ in latencies_ms]
        elif receiver_count:
            state = "feedback"
            if self.last_verified == "lost" or self.wait_warned and not self.seen:
                warnings.append("RTCP receiver feedback is available again; playback remains unknown.")
            self.seen = True
            self.last_verified = state
        else:
            state = "lost" if self.seen else "waiting"
            if state == "lost" and self.last_verified != "lost":
                warnings.append("RTCP receiver feedback was lost; remote playback is unknown.")
            elif state == "waiting" and now - self.started >= 10 and not self.wait_warned:
                warnings.append("No RTCP receiver feedback observed after 10 seconds; remote playback is unknown.")
                self.wait_warned = True
            self.last_verified = state
        return {"state": state, "receiver_count": receiver_count,
                "latencies_ms": latencies_ms}, warnings


def monitor_snapshot(local, remote):
    state = ("no_pcm" if local["captured"] == 0 else
             "silent" if local["sent"] > 0 and local["peak"] == 0 else "receiving")
    return {"local": {"state": state, "captured_frames": local["captured"],
                      "sent_frames": local["sent"], "elapsed_s": local["elapsed"],
                      "peak": local["peak"], "queue_blocks": local["queue"],
                      "callback_age_s": local["callback_age"]},
            "remote": remote, "playback": "unknown"}


class MediaEncoding(C.Structure):
    _fields_ = [("rate", C.c_uint), ("format", C.c_int),
                ("channels", C.c_int), ("tracks", C.c_uint)]


class ContextConfig(C.Structure):
    _fields_ = [("max_packet_size", C.c_uint), ("max_frame_size", C.c_uint)]


class SenderConfig(C.Structure):
    # Keep in sync with vendor/roc-toolkit/src/public_api/include/roc/config.h.
    _fields_ = [("frame_encoding", MediaEncoding), ("packet_encoding", C.c_int),
                ("packet_length", C.c_ulonglong), ("packet_interleaving", C.c_uint),
                ("fec_encoding", C.c_int), ("fec_block_source_packets", C.c_uint),
                ("fec_block_repair_packets", C.c_uint), ("clock_source", C.c_int),
                ("latency_tuner_backend", C.c_int), ("latency_tuner_profile", C.c_int),
                ("resampler_backend", C.c_int), ("resampler_profile", C.c_int),
                ("target_latency", C.c_ulonglong), ("latency_tolerance", C.c_ulonglong)]


class Frame(C.Structure):
    _fields_ = [("samples", C.c_void_p), ("samples_size", C.c_size_t)]


class SenderMetrics(C.Structure):
    _fields_ = [("connection_count", C.c_uint)]


class ConnectionMetrics(C.Structure):
    _fields_ = [("e2e_latency", C.c_ulonglong)]


def check(result, operation):
    if result != 0:
        raise RuntimeError(f"libroc {operation} failed ({result}); see native log above")


class Roc:
    def __init__(self, path: Path):
        self.dll = C.CDLL(str(path.resolve()))
        signatures = {
            "context_open": [C.POINTER(ContextConfig), C.POINTER(C.c_void_p)],
            "context_close": [C.c_void_p],
            "sender_open": [C.c_void_p, C.POINTER(SenderConfig), C.POINTER(C.c_void_p)],
            "sender_close": [C.c_void_p],
            "sender_connect": [C.c_void_p, C.c_ulonglong, C.c_int, C.c_void_p],
            "sender_write": [C.c_void_p, C.POINTER(Frame)],
            "sender_query": [C.c_void_p, C.c_ulonglong, C.POINTER(SenderMetrics),
                             C.POINTER(ConnectionMetrics), C.POINTER(C.c_size_t)],
            "endpoint_allocate": [C.POINTER(C.c_void_p)],
            "endpoint_set_uri": [C.c_void_p, C.c_char_p],
            "endpoint_deallocate": [C.c_void_p],
        }
        for name, args in signatures.items():
            func = getattr(self.dll, "roc_" + name)
            func.argtypes, func.restype = args, C.c_int

    def call(self, name, *args):
        check(getattr(self.dll, "roc_" + name)(*args), name)

    def connect(self, sender, interface, uri):
        endpoint = C.c_void_p()
        self.call("endpoint_allocate", C.byref(endpoint))
        try:
            self.call("endpoint_set_uri", endpoint, uri.encode("utf-8"))
            self.call("sender_connect", sender, 0, interface, endpoint)
        finally:
            self.call("endpoint_deallocate", endpoint)

    def write(self, sender, data):
        samples = C.create_string_buffer(data)
        frame = Frame(C.cast(samples, C.c_void_p), len(data))
        self.call("sender_write", sender, C.byref(frame))

    def query(self, sender):
        # This sender uses one unicast slot; the vendored feedback monitor
        # exposes at most one receiver. Never silently truncate metrics.
        slot = SenderMetrics()
        connections = (ConnectionMetrics * 1)()
        count = C.c_size_t(len(connections))
        self.call("sender_query", sender, 0, C.byref(slot), connections, C.byref(count))
        if slot.connection_count > len(connections) or count.value != slot.connection_count:
            raise RuntimeError("libroc returned unexpected receiver metrics for the unicast sender")
        return slot.connection_count, [
            connection.e2e_latency / 1_000_000 if connection.e2e_latency else None
            for connection in connections[:count.value]]


def capture_devices(devices, wasapi_index, mode):
    """Restrict selection to the requested WASAPI endpoint type."""
    if mode not in ("input", "loopback"):
        raise ValueError("capture_mode must be input or loopback")
    return [device for device in devices
            if device["hostApi"] == wasapi_index and device["maxInputChannels"] > 0
            and bool(device["isLoopbackDevice"]) == (mode == "loopback")]


def select_device(devices, name=None, index=None):
    """Select one eligible endpoint; ambiguity and absence are errors."""
    if index is not None:
        matches = [d for d in devices if d["index"] == index]
    else:
        matches = [d for d in devices if name.casefold() in d["name"].casefold()]
    if len(matches) != 1:
        raise ValueError(f"Device selection matched {len(matches)} devices in the selected capture mode; "
                         "use --list-devices and set --capture-mode plus a unique --device or --device-index")
    return matches[0]


def validate_endpoint(uri, scheme):
    parsed = urlsplit(uri)
    if (parsed.scheme != scheme or not parsed.hostname or parsed.port is None
            or not 1 <= parsed.port <= 65535 or parsed.path or parsed.query
            or parsed.fragment or parsed.username is not None):
        raise ValueError(f"Expected {scheme}://host:port, got {uri!r}")
    return uri


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, help="TOML settings file")
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--stop-stdin", action="store_true",
                   help="Stop gracefully when parent closes stdin or sends a line")
    p.add_argument("--capture-mode", choices=("input", "loopback"),
                   help="WASAPI recording input or output loopback (default: input)")
    selection = p.add_mutually_exclusive_group()
    selection.add_argument("--device", help="Unique case-insensitive device name substring")
    selection.add_argument("--device-index", type=int, help="Device index from --list-devices")
    p.add_argument("--dll", type=Path, help="Native roc.dll path")
    for name in ("source", "repair", "control"):
        p.add_argument("--" + name, help=f"Receiver {name} URI")
    p.add_argument("--chunk-frames", type=int)
    p.add_argument("--duration", type=float, help="Stop after N seconds (default: until Ctrl+C)")
    p.add_argument("--stats-interval", type=float, default=10,
                   help="Print capture/send health every N seconds (default: 10)")
    return p


def settings(args):
    config = {}
    if args.config:
        with args.config.open("rb") as file:
            config = tomllib.load(file)
    allowed = {"capture_mode", "device", "device_index", "dll", "source", "repair", "control", "chunk_frames"}
    unknown = config.keys() - allowed
    if unknown:
        raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
    for key in allowed:
        value = getattr(args, key)
        if value is not None:
            config[key] = value
    if args.device is not None:
        config.pop("device_index", None)
    if args.device_index is not None:
        config.pop("device", None)
    if "device" in config and "device_index" in config:
        raise ValueError("Configure device or device_index, not both")
    config.setdefault("capture_mode", "input")
    if config["capture_mode"] not in ("input", "loopback"):
        raise ValueError("capture_mode must be input or loopback")
    config.setdefault("device", "CABLE Output" if config["capture_mode"] == "input" else "CABLE Input")
    config.setdefault("chunk_frames", 480)
    if type(config["chunk_frames"]) is not int or config["chunk_frames"] < 1:
        raise ValueError("chunk_frames must be a positive integer")
    for key, scheme in (("source", "rtp+rs8m"), ("repair", "rs8m"), ("control", "rtcp")):
        if key not in config:
            raise ValueError(f"Missing {key}: provide --{key} or --config")
        validate_endpoint(config[key], scheme)
    if "dll" in config:
        dll = Path(config["dll"])
        if not dll.is_absolute() and args.config and args.dll is None:
            dll = args.config.resolve().parent / dll
    else:
        dll = Path(__file__).resolve().parent / "build" / "Release" / "roc.dll"
    config["dll"] = dll
    return config


def main():
    args = parser().parse_args()
    if args.duration is not None and (args.duration <= 0 or not args.duration < float("inf")):
        raise ValueError("duration must be positive and finite")
    if not 0 < args.stats_interval < float("inf"):
        raise ValueError("stats-interval must be positive and finite")
    import pyaudiowpatch as audio
    with ExitStack() as stack:
        pa = audio.PyAudio()
        stack.callback(pa.terminate)
        wasapi_index = pa.get_host_api_info_by_type(audio.paWASAPI)["index"]
        devices = [pa.get_device_info_by_index(index) for index in range(pa.get_device_count())]
        if args.list_devices:
            for mode in ("input", "loopback"):
                for device in capture_devices(devices, wasapi_index, mode):
                    print(f'{device["index"]}: [WASAPI {mode}] {device["name"]} '
                          f'({device["defaultSampleRate"]:g} Hz, {device["maxInputChannels"]} channels)')
            return
        config = settings(args)
        devices = capture_devices(devices, wasapi_index, config["capture_mode"])
        device = select_device(devices, config["device"], config.get("device_index"))
        channels = int(device["maxInputChannels"])
        if channels not in (1, 2):
            raise ValueError(f"Selected device has {channels} channels; only mono/stereo supported. "
                             "Set Windows device channel configuration to stereo explicitly.")
        rate = int(device["defaultSampleRate"])
        stack.enter_context(os.add_dll_directory(str(config["dll"].resolve().parent)))
        roc = Roc(config["dll"])
        context, sender = C.c_void_p(), C.c_void_p()
        roc.call("context_open", C.byref(ContextConfig()), C.byref(context))
        stack.callback(roc.call, "context_close", context)
        sender_config = SenderConfig()
        sender_config.frame_encoding = MediaEncoding(rate, 1, 2 if channels == 1 else 3, 0)
        sender_config.packet_encoding = 10  # RTP AVP L16 stereo, 44100 Hz
        sender_config.fec_encoding = 1  # RS8M
        sender_config.clock_source = 1  # WASAPI supplies the external clock
        roc.call("sender_open", context, C.byref(sender_config), C.byref(sender))
        stack.callback(roc.call, "sender_close", sender)
        for interface, key in ((11, "source"), (12, "repair"), (13, "control")):
            roc.connect(sender, interface, config[key])
        # At most 100 ms of queued PCM, consumed immediately without prebuffering.
        capacity = max(1, (rate + 10 * config["chunk_frames"] - 1) // (10 * config["chunk_frames"]))
        capture = CaptureQueue(channels, capacity, audio.paContinue, audio.paAbort)
        stream = pa.open(format=audio.paFloat32, channels=channels, rate=rate, input=True,
                         input_device_index=device["index"], frames_per_buffer=config["chunk_frames"],
                         stream_callback=capture.callback, start=False)
        stack.callback(stream.close)
        stack.callback(stream.stop_stream)
        stopping = False
        parent_stop = threading.Event()
        if args.stop_stdin:
            def watch_parent():
                sys.stdin.readline()
                parent_stop.set()
            threading.Thread(target=watch_parent, daemon=True).start()

        def stop(signum, frame):
            nonlocal stopping
            stopping = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous = signal.signal(sig, stop)
            stack.callback(signal.signal, sig, previous)
        print(f'Capturing WASAPI {config["capture_mode"]}: {device["name"]}, '
              f'{rate} Hz, {channels} channels', flush=True)
        print(f'Sending {config["source"]}, {config["repair"]}, {config["control"]}', flush=True)
        start = time.monotonic()
        frames = 0
        peak = 0.0
        health = CaptureHealth(start)
        monitor_health = CaptureHealth(start)
        feedback_health = FeedbackHealth(start)
        stream.start_stream()
        while not stopping and not parent_stop.is_set() and (args.duration is None or time.monotonic() - start < args.duration):
            block = capture.read()
            capture.check_active(stream)
            if block is not None:
                data, frame_count = block
                block_peak = health.observe(data)
                monitor_health.peak = max(monitor_health.peak, block_peak)
                peak = max(peak, block_peak)
                roc.write(sender, data)
                frames += frame_count
            now = time.monotonic()
            if now - monitor_health.started >= 1.0:
                local = monitor_health.snapshot(capture, frames, now)
                receiver_count, latencies_ms = roc.query(sender)
                remote, warnings = feedback_health.snapshot(receiver_count, latencies_ms, local, now)
                print("ROC_MONITOR " + json.dumps(monitor_snapshot(local, remote)), flush=True)
                for warning in warnings:
                    print("WARNING: " + warning, flush=True)
            if now - health.started >= args.stats_interval:
                status = health.snapshot(capture, frames, now)
                age = ("never" if status["callback_age"] is None else
                       f'{status["callback_age"]:.3f}s')
                state = ("no captured PCM" if status["captured"] == 0 else
                         "silent PCM" if status["sent"] and status["peak"] == 0 else "PCM")
                print(f'Local: {status["elapsed"]:.2f}s, captured {status["captured"]}, '
                      f'sent {status["sent"]} frames, peak {status["peak"]:.6f}, '
                      f'queue {status["queue"]}/{capacity}, last callback {age}, {state}', flush=True)
        stream.stop_stream()
        capture.check()
        # No producer remains: drain every accepted block before closing Roc.
        while (block := capture.read(timeout=0)) is not None:
            data, frame_count = block
            peak = max(peak, max(abs(value) for value in memoryview(data).cast("f")))
            roc.write(sender, data)
            frames += frame_count
        elapsed = time.monotonic() - start
        print(f'Stopped: {capture.frames} captured frames, {frames} sent frames, '
              f'{frames / rate:.2f}s audio / {elapsed:.3f}s wall, peak {peak:.6f}, '
              f'queue high-water {capture.high_water}/{capacity} blocks')
        if peak == 0:
            print("WARNING: captured audio was entirely silent; verify audio is routed to the selected device.")


if __name__ == "__main__":
    main()
