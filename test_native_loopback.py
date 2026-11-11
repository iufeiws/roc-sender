"""End-to-end smoke test for the native Roc DLL, including RS8M repair traffic."""

import ctypes as C
import math
import os
import struct
import time
from pathlib import Path

from sender import ContextConfig, Frame, MediaEncoding, Roc, SenderConfig


class ReceiverConfig(C.Structure):
    _fields_ = [
        ("frame_encoding", MediaEncoding),
        ("clock_source", C.c_int),
        ("latency_tuner_backend", C.c_int),
        ("latency_tuner_profile", C.c_int),
        ("resampler_backend", C.c_int),
        ("resampler_profile", C.c_int),
        ("target_latency", C.c_ulonglong),
        ("latency_tolerance", C.c_ulonglong),
        ("no_playback_timeout", C.c_longlong),
        ("choppy_playback_timeout", C.c_longlong),
    ]


def check(result, operation):
    if result != 0:
        raise RuntimeError(f"{operation} failed with Roc status {result}")


def main():
    dll = Path("build/Release/roc.dll").resolve()
    os.add_dll_directory(str(dll.parent))
    roc = Roc(dll)
    roc.dll.roc_receiver_open.argtypes = [C.c_void_p, C.POINTER(ReceiverConfig), C.POINTER(C.c_void_p)]
    roc.dll.roc_receiver_bind.argtypes = [C.c_void_p, C.c_ulonglong, C.c_int, C.c_void_p]
    roc.dll.roc_receiver_read.argtypes = [C.c_void_p, C.POINTER(Frame)]
    roc.dll.roc_receiver_close.argtypes = [C.c_void_p]
    for name in ("roc_receiver_open", "roc_receiver_bind", "roc_receiver_read",
                 "roc_receiver_close"):
        getattr(roc.dll, name).restype = C.c_int

    context = C.c_void_p()
    roc.call("context_open", C.byref(ContextConfig()), C.byref(context))
    receiver = C.c_void_p()
    sender = C.c_void_p()
    endpoints = []
    try:
        receiver_config = ReceiverConfig()
        receiver_config.frame_encoding = MediaEncoding(48000, 1, 3, 0)
        receiver_config.clock_source = 1
        check(roc.dll.roc_receiver_open(context, C.byref(receiver_config), C.byref(receiver)),
              "roc_receiver_open")

        uris = ((11, "rtp+rs8m://127.0.0.1:21001"),
                (12, "rs8m://127.0.0.1:21002"),
                (13, "rtcp://127.0.0.1:21003"))
        for interface, uri in uris:
            value = C.c_void_p()
            roc.call("endpoint_allocate", C.byref(value))
            endpoints.append(value)
            roc.call("endpoint_set_uri", value, uri.encode())
            check(roc.dll.roc_receiver_bind(receiver, 0, interface, value), "roc_receiver_bind")

        sender_config = SenderConfig()
        sender_config.frame_encoding = MediaEncoding(48000, 1, 3, 0)
        sender_config.packet_encoding = 10
        sender_config.fec_encoding = 1
        sender_config.clock_source = 1
        roc.call("sender_open", context, C.byref(sender_config), C.byref(sender))
        for interface, uri in uris:
            roc.connect(sender, interface, uri)

        frames_per_chunk = 480
        received_peak = 0.0
        for chunk in range(160):
            values = []
            for index in range(frames_per_chunk):
                sample = 0.25 * math.sin(2 * math.pi * 440 * (chunk * frames_per_chunk + index) / 48000)
                values.extend((sample, sample))
            roc.write(sender, struct.pack(f"<{len(values)}f", *values))
            time.sleep(0.01)

            output = (C.c_float * (frames_per_chunk * 2))()
            frame = Frame(C.cast(output, C.c_void_p), C.sizeof(output))
            check(roc.dll.roc_receiver_read(receiver, C.byref(frame)), "roc_receiver_read")
            received_peak = max(received_peak, max(abs(value) for value in output))

        if received_peak < 0.05:
            raise RuntimeError(f"decoded output stayed silent (peak={received_peak:.6f})")
        print(f"Native sender/receiver RS8M loopback decoded audio, peak={received_peak:.6f}")
    finally:
        if sender:
            roc.call("sender_close", sender)
        if receiver:
            check(roc.dll.roc_receiver_close(receiver), "roc_receiver_close")
        for value in endpoints:
            roc.call("endpoint_deallocate", value)
        if context:
            roc.call("context_close", context)


if __name__ == "__main__":
    main()
