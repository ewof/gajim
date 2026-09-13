# This file is part of Gajim.
# SPDX-License-Identifier: GPL-3.0-only

"""GStreamer audio transport. Microphone acquisition follows DTLS verification."""

import secrets
from collections.abc import Callable
from functools import lru_cache

import gi
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from gi.repository import GLib

from gajim.common.calls.protocol import CallError
from gajim.common.calls.protocol import fingerprint


@lru_cache(maxsize=1)
def availability() -> str | None:
    try:
        gi.require_version("Gst", "1.0")
        gi.require_version("GstWebRTC", "1.0")
        gi.require_version("GstSdp", "1.0")
        from gi.repository import Gst
        from gi.repository import GstSdp  # noqa: F401
        from gi.repository import GstWebRTC  # noqa: F401

        Gst.init(None)
        if Gst.version()[:3] < (1, 28, 7):
            return "Secure calls require GStreamer 1.28.7 or newer"
        required = (
            "webrtcbin",
            "nicesrc",
            "dtlssrtpenc",
            "dtlssrtpdec",
            "opusenc",
            "opusdec",
            "rtpopuspay",
            "rtpopusdepay",
            "appsrc",
            "appsink",
            "pulsesrc",
            "pulsesink",
            "webrtcdsp",
            "webrtcechoprobe",
        )
        missing = [name for name in required if Gst.ElementFactory.find(name) is None]
        if missing:
            return "Missing audio components: " + ", ".join(missing)
    except (ValueError, ImportError):
        return "GStreamer WebRTC components are not installed"
    return None


class AudioTransport:
    def __init__(
        self,
        *,
        payload: int = 111,
        ice_servers: list[str],
        on_candidate: Callable[[str], None],
        on_connected: Callable[[], None],
        on_error: Callable[[str], None],
        test_audio: bool = False,
        relay_only: bool = True,
    ) -> None:
        if relay_only and not any(
            uri.startswith(("turn://", "turns://")) for uri in ice_servers
        ):
            raise CallError(
                "Your XMPP server did not provide a usable TURN relay. "
                "Ask its administrator to configure TURN, or enable "
                "Do not relay calls to allow sharing your IP address."
            )
        error = availability()
        if error:
            raise CallError(error)
        from gi.repository import Gst

        self.Gst = Gst
        self.closed = False
        self.authenticated = False
        self.capture = None
        self.expected_fingerprint: str | None = None
        self.on_connected = on_connected
        self.on_error = on_error
        self.test_audio = test_audio
        self.received_buffers = 0
        self.muted = False
        self._remote_set = False
        self._candidates: list[str] = []
        self._signals = []
        self._poll = 0
        probe_name = "call-echo-" + secrets.token_hex(8)
        self.pipeline = Gst.parse_launch(
            "appsrc name=input is-live=true format=time do-timestamp=true "
            "caps=audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved "
            "! queue name=capture_queue max-size-time=100000000 "
            "max-size-buffers=0 max-size-bytes=0 "
            "! audioconvert ! audioresample "
            f"! webrtcdsp name=dsp probe={probe_name} echo-cancel=true "
            "noise-suppression-level=low gain-control=false "
            "! volume name=microphone ! opusenc audio-type=voice bitrate=48000 "
            "frame-size=20 inband-fec=true packet-loss-percentage=10 "
            f"! rtpopuspay pt={payload} "
            "! application/x-rtp,media=audio,encoding-name=OPUS,clock-rate=48000,"
            f"payload={payload},encoding-params=(string)2 "
            "! webrtcbin name=rtc bundle-policy=none"
        )
        self.rtc = self.pipeline.get_by_name("rtc")
        from gi.repository import GstWebRTC

        self.rtc.set_property(
            "ice-transport-policy",
            GstWebRTC.WebRTCICETransportPolicy.RELAY
            if relay_only
            else GstWebRTC.WebRTCICETransportPolicy.ALL,
        )
        self.input = self.pipeline.get_by_name("input")
        sink = (
            "fakesink sync=false async=false"
            if self.test_audio
            else "pulsesink sync=false async=false"
        )
        self.output = Gst.parse_bin_from_description(
            "queue ! rtpopusdepay ! opusdec ! audioconvert ! audioresample "
            f"! audio/x-raw,rate=48000 ! webrtcechoprobe name={probe_name} "
            "! " + sink,
            True,
        )
        self.pipeline.add(self.output)
        self.rtc.set_property("latency", 100)
        for uri in ice_servers:
            if uri.startswith("stun://"):
                self.rtc.set_property("stun-server", uri)
            elif not self.rtc.emit("add-turn-server", uri):
                self.close()
                raise CallError("The TURN service configuration could not be used")
        self._signals.append(
            (
                self.rtc,
                self.rtc.connect(
                    "on-ice-candidate",
                    lambda _rtc, _index, value: GLib.idle_add(
                        self._dispatch, on_candidate, value
                    ),
                ),
            )
        )
        self._signals.append(
            (self.rtc, self.rtc.connect("pad-added", self._incoming_pad))
        )
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self._signals.append(
            (self.bus, self.bus.connect("message::error", self._bus_error))
        )
        self._poll = GLib.timeout_add(100, self._check_connection)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _dispatch(self, callback, *args) -> bool:
        if not self.closed:
            callback(*args)
        return GLib.SOURCE_REMOVE

    def _bus_error(self, _bus, _message) -> None:
        # Gst errors/debug strings may include TURN credentials or network SDP.
        self.on_error(
            "Audio transport failed. Check that your audio devices are available."
        )

    def _promise(self, callback):
        def replied(promise, *_args):
            try:
                reply = promise.get_reply()
                if reply is not None and reply.has_field("error"):
                    GLib.idle_add(
                        self._dispatch, self.on_error, "WebRTC negotiation failed"
                    )
                    return
                # get_reply() is borrowed from the promise. The callback runs
                # later on GTK's thread, after the promise can be finalized.
                GLib.idle_add(self._dispatch, callback, reply.copy() if reply else None)
            except Exception:
                GLib.idle_add(
                    self._dispatch, self.on_error, "WebRTC negotiation failed"
                )

        return self.Gst.Promise.new_with_change_func(replied, None, None)

    def create_description(self, offer: bool, callback: Callable[[str], None]) -> None:
        kind = "offer" if offer else "answer"

        def created(reply):
            if reply is None or not reply.has_field(kind):
                self.on_error("Could not create a secure audio session")
                return
            description = reply.get_value(kind).copy()
            sdp = description.sdp.as_text()
            self.rtc.emit(
                "set-local-description",
                description,
                self._promise(lambda _reply: callback(sdp)),
            )

        self.rtc.emit("create-" + kind, None, self._promise(created))

    def set_remote(
        self, sdp: str, expected: str, *, offer: bool, callback: Callable[[], None]
    ) -> None:
        from gi.repository import GstSdp
        from gi.repository import GstWebRTC

        if self.expected_fingerprint is not None:
            raise CallError("Renegotiation is not supported")
        self.expected_fingerprint = fingerprint(expected)
        result, message = GstSdp.SDPMessage.new_from_text(sdp)
        if result != GstSdp.SDPResult.OK:
            raise CallError("Invalid remote session description")
        kind = (
            GstWebRTC.WebRTCSDPType.OFFER if offer else GstWebRTC.WebRTCSDPType.ANSWER
        )
        description = GstWebRTC.WebRTCSessionDescription.new(kind, message)

        def done(_reply):
            self._remote_set = True
            for candidate in self._candidates:
                self.rtc.emit("add-ice-candidate", 0, candidate)
            self._candidates.clear()
            callback()

        self.rtc.emit("set-remote-description", description, self._promise(done))

    def add_candidate(self, value: str) -> None:
        if self.closed:
            return
        if not self._remote_set:
            if len(self._candidates) >= 128:
                raise CallError("Too many pending ICE candidates")
            self._candidates.append(value)
        else:
            self.rtc.emit("add-ice-candidate", 0, value)

    def _check_connection(self) -> bool:
        if self.closed:
            return GLib.SOURCE_REMOVE
        from gi.repository import GstWebRTC

        state = self.rtc.get_property("connection-state")
        if state in (
            GstWebRTC.WebRTCPeerConnectionState.FAILED,
            GstWebRTC.WebRTCPeerConnectionState.CLOSED,
        ):
            self._poll = 0
            self.on_error("The audio connection failed")
            return GLib.SOURCE_REMOVE
        if self.authenticated or self.expected_fingerprint is None:
            return GLib.SOURCE_CONTINUE
        transceiver = self.rtc.emit("get-transceiver", 0)
        if transceiver is None:
            return GLib.SOURCE_CONTINUE
        transport = transceiver.get_property("sender").get_property("transport")
        if transport is None:
            return GLib.SOURCE_CONTINUE
        if (
            transport.get_property("state")
            != GstWebRTC.WebRTCDTLSTransportState.CONNECTED
        ):
            return GLib.SOURCE_CONTINUE
        pem = transport.get_property("remote-certificate")
        if not pem:
            return GLib.SOURCE_CONTINUE
        try:
            certificate = x509.load_pem_x509_certificate(pem.encode())
            actual = certificate.fingerprint(hashes.SHA256()).hex(":").upper()
            if actual != self.expected_fingerprint:
                raise CallError(
                    "The media certificate does not match the verified device"
                )
            self.authenticated = True
            # This callback rechecks OMEMO trust before opening the microphone.
            self.on_connected()
        except Exception:
            self._poll = 0
            self.on_error("Call stopped: media certificate verification failed")
            return GLib.SOURCE_REMOVE
        if not self.closed:
            try:
                self._start_capture()
            except Exception:
                self._poll = 0
                self.on_error("Could not start audio capture. Check your microphone.")
                return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _start_capture(self) -> None:
        Gst = self.Gst
        source = (
            "audiotestsrc is-live=true wave=sine samplesperbuffer=480"
            if self.test_audio
            else "pulsesrc buffer-time=60000 latency-time=10000"
        )
        self.capture = Gst.parse_bin_from_description(
            source + " ! audioconvert ! audioresample "
            '! capsfilter caps="audio/x-raw,format=S16LE,rate=48000,'
            'channels=1,layout=interleaved"',
            True,
        )
        # The placeholder supplies caps during negotiation without acquiring a
        # microphone. After authentication, capture joins the same clocked
        # pipeline. Bridging two pipelines through Python used callback arrival
        # times as timestamps and dropped audio when that callback fell behind.
        queue = self.pipeline.get_by_name("capture_queue")
        self.input.set_state(Gst.State.NULL)
        self.input.unlink(queue)
        self.pipeline.remove(self.input)
        self.pipeline.add(self.capture)
        if not self.capture.link(queue):
            raise CallError("Could not connect the microphone")
        if not self.capture.sync_state_with_parent():
            raise CallError("Could not start the microphone")

    def _incoming_pad(self, _rtc, pad) -> None:
        Gst = self.Gst
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        # Link immediately on the streaming thread so the first packet cannot
        # cause a not-linked error. A pad probe gates all decoded remote audio.
        ghost = self.output.get_static_pad("sink")

        def gate(_pad, _info):
            if not self.authenticated or self.closed:
                return Gst.PadProbeReturn.DROP
            self.received_buffers += 1
            return Gst.PadProbeReturn.OK

        ghost.add_probe(Gst.PadProbeType.BUFFER, gate)
        pad.link(ghost)

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        self.pipeline.get_by_name("microphone").set_property("mute", muted)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._poll:
            GLib.source_remove(self._poll)
            self._poll = 0
        for obj, signal in self._signals:
            obj.disconnect(signal)
        self._signals.clear()
        if self.capture is not None:
            self.capture.set_state(self.Gst.State.NULL)
        self.pipeline.set_state(self.Gst.State.NULL)
        if hasattr(self, "bus"):
            self.bus.remove_signal_watch()
