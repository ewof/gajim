# This file is part of Gajim.
# SPDX-License-Identifier: GPL-3.0-only

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from nbxmpp.simplexml import Node
from omemo_dr.const import OMEMOTrust
from omemo_dr.session_manager import OMEMOSessionManager
from omemo_dr.structs import OMEMOConfig

from gajim.common import app
from gajim.common import modules  # noqa: F401 - initialize module registry first
from gajim.common.calls import protocol as p
from gajim.common.calls.security import CallSecurity
from gajim.common.calls.security import trust_recent_keys
from gajim.common.storage.omemo import OMEMOStorage

FP = ":".join(["AB"] * 32)
SDP = (
    "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\nc=IN IP4 0.0.0.0\r\n"
    "a=mid:audio0\r\na=rtcp-mux\r\na=sendrecv\r\n"
    "a=rtpmap:111 OPUS/48000/2\r\na=setup:actpass\r\n"
    "a=ice-ufrag:abcd\r\na=ice-pwd:abcdefghijklmnopqrstuvwx\r\n"
    f"a=fingerprint:sha-256 {FP}\r\n"
)


def jingle(sdp=SDP):
    content, _local = p.sdp_to_description(sdp)
    fp = content.getTag("transport").addChild(
        "fingerprint", namespace=p.VERIFY, attrs={"hash": "sha-256", "setup": "actpass"}
    )
    fp.addChild("encrypted", namespace=p.OMEMO)
    node = Node("jingle", attrs={"xmlns": p.JINGLE})
    node.addChild(node=content)
    return Node(node=str(node))


class ProtocolTests(unittest.TestCase):
    def test_relay_description_does_not_disclose_related_addresses(self):
        candidates = (
            "a=candidate:1 1 UDP 100 192.168.1.2 1234 typ host\r\n"
            "a=candidate:2 1 UDP 90 198.51.100.2 1235 typ srflx\r\n"
            "a=candidate:3 1 UDP 80 203.0.113.2 1236 typ relay "
            "raddr 198.51.100.2 rport 1235\r\n"
        )
        content, _ = p.sdp_to_description(SDP + candidates, relay_only=True)
        wire = str(content)
        self.assertNotIn("192.168.1.2", wire)
        self.assertNotIn("198.51.100.2", wire)
        self.assertIn("203.0.113.2", wire)
        self.assertNotIn("raddr", wire)
        self.assertEqual(len(content.getTag("transport").getTags("candidate")), 1)
        direct, _ = p.sdp_to_description(SDP + candidates, relay_only=False)
        self.assertEqual(len(direct.getTag("transport").getTags("candidate")), 3)

    def test_missing_turn_fails_before_pipeline_creation(self):
        from gajim.common.calls.media import AudioTransport

        for servers in ([], ["stun://stun.example.org:3478"]):
            with (
                self.subTest(servers=servers),
                self.assertRaisesRegex(
                    p.CallError, "did not provide a usable TURN relay"
                ),
            ):
                AudioTransport(
                    ice_servers=servers,
                    on_candidate=Mock(),
                    on_connected=Mock(),
                    on_error=Mock(),
                )

    def test_roundtrip(self):
        sdp, remote = p.description_to_sdp(jingle(), FP, offer=True)
        self.assertEqual(remote.payload, 111)
        self.assertIn(f"a=fingerprint:sha-256 {FP}", sdp)

    def test_reject_unverified_fingerprint(self):
        node = jingle()
        node.getTag("content").getTag("transport").getTag("fingerprint").setNamespace(
            p.DTLS
        )
        with self.assertRaises(p.CallError):
            p.description_to_sdp(node, FP, offer=True)

    def test_reject_ambiguous_fingerprints(self):
        node = jingle()
        node.getTag("content").getTag("transport").addChild(
            "fingerprint", namespace=p.DTLS
        )
        with self.assertRaises(p.CallError):
            p.description_to_sdp(node, FP, offer=True)

    def test_reject_video_and_extra_content(self):
        for video in (False, True):
            node = jingle()
            if video:
                node.getTag("content").getTag("description").setAttr("media", "video")
            else:
                node.addChild("content")
            with self.assertRaises(p.CallError):
                p.description_to_sdp(node, FP, offer=True)

    def test_reject_sdp_injection(self):
        for attr in ("ufrag", "pwd"):
            node = jingle()
            node.getTag("content").getTag("transport").setAttr(attr, "abcd\r\na=evil")
            with self.assertRaises(p.CallError):
                p.description_to_sdp(node, FP, offer=True)

    def test_candidate_roundtrip_and_injection(self):
        candidate = "candidate:1 1 UDP 2122260223 192.0.2.1 34567 typ host"
        node = p.candidate_to_xml(candidate)
        self.assertEqual(p.candidate_to_sdp(node), candidate)
        node.setAttr("ip", "192.0.2.1\r\na=evil")
        with self.assertRaises(p.CallError):
            p.candidate_to_sdp(node)


class SecurityTests(unittest.TestCase):
    def test_recent_trust_only_approves_keys_in_the_time_window(self):
        from types import SimpleNamespace

        now = 200000
        backend = Mock()
        infos = [
            SimpleNamespace(public_key=object(), last_seen=seen, trust=trust)
            for seen, trust in (
                (now, OMEMOTrust.UNDECIDED),
                (now - 86400, OMEMOTrust.BLIND),
                (now - 86401, OMEMOTrust.UNDECIDED),
                (None, OMEMOTrust.UNDECIDED),
                (now + 1, OMEMOTrust.UNDECIDED),
                (now, OMEMOTrust.VERIFIED),
            )
        ]
        backend.get_identity_infos.return_value = infos
        with patch("gajim.common.calls.security.time.time", return_value=now):
            self.assertEqual(trust_recent_keys(backend, "bob@example.org"), 2)
        backend.get_identity_infos.assert_called_once_with("bob@example.org")
        self.assertEqual(backend.set_trust.call_count, 2)
        for index, call in enumerate(backend.set_trust.call_args_list):
            self.assertEqual(
                call.args,
                ("bob@example.org", infos[index].public_key, OMEMOTrust.VERIFIED),
            )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = patch.object(
            app.settings, "get_account_setting", return_value=False
        )
        self.settings.start()
        self.addCleanup(self.settings.stop)
        config = OMEMOConfig(10, 5, 86400, 86400, 2000)
        self.backends = []
        for name in ("alice", "bob"):
            store = OMEMOStorage(
                name, Path(self.temp.name) / name, logging.getLogger(name)
            )
            self.addCleanup(store._con.close)
            self.backends.append(
                OMEMOSessionManager(name + "@example.org", store, config)
            )
        alice, bob = self.backends
        for local, remote, address in (
            (alice, bob, "bob@example.org"),
            (bob, alice, "alice@example.org"),
        ):
            local.build_session(address, remote.get_bundle(p.OMEMO))
            local.add_device(address, remote.get_our_device())
            local.set_trust(address, remote.get_our_identity()[1], OMEMOTrust.VERIFIED)
        self.alice = CallSecurity(alice, "bob@example.org")
        self.bob = CallSecurity(bob, "alice@example.org")
        self.alice.pin(bob.get_our_device())

    def test_real_signal_roundtrip_and_replay_rejection(self):
        node = self.alice.encrypt(FP, "actpass")
        # Serialize and parse as on the wire, exercising namespace inheritance.
        node = Node(node=str(node))
        self.assertEqual(self.bob.decrypt(node), FP)
        self.assertEqual(
            self.alice.decrypt(Node(node=str(self.bob.encrypt(FP, "active")))), FP
        )
        with self.assertRaises(p.CallError):
            self.bob.decrypt(node)

    def test_tampered_envelope(self):
        node = self.alice.encrypt(FP, "actpass")
        node.getTag("encrypted").getTag("payload").setData("QUFBQQ==")
        with self.assertRaises(p.CallError):
            self.bob.decrypt(Node(node=str(node)))

    def test_trust_revocation(self):
        alice, bob = self.backends
        alice.set_trust(
            "bob@example.org", bob.get_our_identity()[1], OMEMOTrust.UNTRUSTED
        )
        with self.assertRaises(p.CallError):
            self.alice.encrypt(FP, "actpass")

    def test_no_multi_recipient_or_device_switch(self):
        node = self.alice.encrypt(FP, "actpass")
        node.getTag("encrypted").getTag("header").addChild("key", attrs={"rid": "123"})
        with self.assertRaises(p.CallError):
            self.bob.decrypt(Node(node=str(node)))
        with self.assertRaises(p.CallError):
            self.alice.pin(123)


@unittest.skipUnless(
    os.environ.get("GAJIM_TEST_CALL_MEDIA") == "1", "Opt-in local WebRTC test"
)
class MediaTests(unittest.TestCase):
    def _pair(self, tamper=False):
        from gi.repository import GLib

        from gajim.common.calls.media import AudioTransport

        loop = GLib.MainLoop()
        errors, connected = [], []
        peers = []

        def fail(message):
            errors.append(message)
            loop.quit()

        for i in range(2):
            peers.append(
                AudioTransport(
                    ice_servers=[],
                    relay_only=False,
                    test_audio=True,
                    on_candidate=lambda c, i=i: peers[1 - i].add_candidate(c),
                    on_connected=lambda i=i: connected.append(i),
                    on_error=fail,
                )
            )
        alice, bob = peers
        self.assertIsNone(alice.capture)
        self.assertIsNone(bob.capture)

        def answered(sdp):
            _content, local = p.sdp_to_description(sdp)
            alice.set_remote(
                sdp,
                FP if tamper else local.fingerprint,
                offer=False,
                callback=lambda: None,
            )

        def offered(sdp):
            _content, local = p.sdp_to_description(sdp)
            bob.set_remote(
                sdp,
                local.fingerprint,
                offer=True,
                callback=lambda: bob.create_description(False, answered),
            )

        alice.create_description(True, offered)

        def ready():
            if all(peer.received_buffers > 10 for peer in peers):
                loop.quit()
                return False
            return True

        poll = GLib.timeout_add(100, ready)
        timeout = GLib.timeout_add_seconds(15, loop.quit)
        try:
            loop.run()
            if tamper:
                self.assertTrue(errors)
                self.assertIsNone(alice.capture)
                self.assertNotIn(0, connected)
            else:
                self.assertEqual(errors, [])
                self.assertCountEqual(connected, [0, 1])
                self.assertTrue(all(peer.received_buffers > 10 for peer in peers))
                for peer in peers:
                    self.assertEqual(peer.capture.get_parent(), peer.pipeline)
                    self.assertEqual(
                        peer.capture.get_clock(), peer.pipeline.get_clock()
                    )
        finally:
            for peer in peers:
                peer.close()
            for source in (poll, timeout):
                if GLib.MainContext.default().find_source_by_id(source):
                    GLib.source_remove(source)

    def test_encrypted_audio_roundtrip(self):
        self._pair()

    def test_certificate_substitution_never_opens_capture(self):
        self._pair(tamper=True)


class SignalingTests(unittest.TestCase):
    setUp = SecurityTests.setUp

    def test_relay_default_snapshot_and_trickle_privacy(self):
        from nbxmpp.protocol import JID

        from gajim.common.setting_values import APP_SETTINGS

        self.assertFalse(APP_SETTINGS["calls_do_not_relay"])
        alice, _bob, _statuses = self._modules()
        with patch.object(app.settings, "get", return_value=False):
            call = alice._new_call(
                JID.from_string("bob@example.org/phone"), "relay", True
            )
        self.assertTrue(call.relay_only)
        call.local = p.sdp_to_description(SDP)[1]
        call.offered = True
        alice._send_jingle = Mock()
        for kind in ("host", "srflx", "prflx"):
            alice._candidate(
                call, f"candidate:1 1 UDP 100 198.51.100.2 1234 typ {kind}"
            )
        alice._send_jingle.assert_not_called()
        alice._candidate(
            call,
            "candidate:2 1 UDP 80 203.0.113.2 1236 typ relay "
            "raddr 198.51.100.2 rport 1234",
        )
        alice._send_jingle.assert_called_once()
        self.assertNotIn("198.51.100.2", str(alice._send_jingle.call_args.args[1]))
        alice.end(send=False)
        with patch.object(app.settings, "get", return_value=True):
            direct = alice._new_call(
                JID.from_string("bob@example.org/phone"), "direct", True
            )
        self.assertFalse(direct.relay_only)

    def test_discovery_without_turn_preserves_explanation(self):
        from nbxmpp.protocol import Iq
        from nbxmpp.protocol import JID

        alice, _bob, statuses = self._modules()
        with patch.object(app.settings, "get", return_value=False):
            call = alice._new_call(
                JID.from_string("bob@example.org/phone"), "relay", True
            )
        alice._request = lambda stanza, callback: callback(Iq(typ="result"))
        alice._discover(call, lambda servers: alice._outgoing_media(call, servers))
        self.assertIsNone(alice.call)
        self.assertIn("did not provide a usable TURN relay", statuses[-1])

    def test_connection_notifications_without_a_call(self):
        from gajim.common.const import SimpleClientState

        alice, _bob, _statuses = self._modules()
        callback = alice._client.connect_signal.call_args.args[1]
        for state in SimpleClientState:
            with self.subTest(state=state):
                callback(alice._client, "state-changed", state)
                self.assertIsNone(alice.call)

    def test_connection_notifications_close_media_on_connection_loss(self):
        from nbxmpp.protocol import JID

        from gajim.common.const import SimpleClientState

        alice, _bob, _statuses = self._modules()
        callback = alice._client.connect_signal.call_args.args[1]
        for state in SimpleClientState:
            with self.subTest(state=state):
                call = alice._new_call(
                    JID.from_string("bob@example.org/phone"), "test-call", True
                )
                media = call.media = Mock()
                callback(alice._client, "state-changed", state)
                if state.is_connected:
                    self.assertIs(alice.call, call)
                    media.close.assert_not_called()
                    alice.end(send=False)
                else:
                    self.assertIsNone(alice.call)
                    media.close.assert_called_once_with()
                alice._client.send_stanza.assert_not_called()

    def _modules(self):
        from types import SimpleNamespace

        from gi.repository import GLib
        from nbxmpp.protocol import Iq
        from nbxmpp.protocol import JID
        from nbxmpp.protocol import Message
        from nbxmpp.protocol import NodeProcessed

        from gajim.common.modules.contacts import BareContact
        from gajim.common.modules.secure_calls import SecureCalls

        endpoints = {}
        callbacks = {}
        statuses = []

        def client(name, backend):
            own = JID.from_string(f"{name}@example.org/desktop")
            con = Mock(account=name)
            con.get_own_jid.return_value = own
            contact = Mock(spec=BareContact)
            contact.is_in_roster = True
            contact.is_blocked = False
            contact.settings = Mock()
            contact.settings.get.return_value = "OMEMO"
            con.get_module.side_effect = lambda module: (
                SimpleNamespace(backend=backend)
                if module == "OMEMO"
                else SimpleNamespace(get_contact=lambda jid: contact)
            )

            def send(stanza, callback=None, **kwargs):
                import uuid

                if stanza.getID() is None:
                    stanza.setID(str(uuid.uuid4()))
                if callback is not None:
                    callbacks[stanza.getID()] = callback
                if stanza.getFrom() is None:
                    stanza.setFrom(own)
                wire = str(stanza)

                def deliver():
                    cls = Iq if stanza.getName() == "iq" else Message
                    received = cls(node=wire)
                    peer = received.getTo()
                    if received.getName() == "iq" and received.getType() in (
                        "result",
                        "error",
                    ):
                        cb = callbacks.pop(received.getID(), None)
                        if cb:
                            cb(None, received)
                        return False
                    if peer.bare == "example.org":
                        reply = received.buildReply("result")
                        reply.addChild("services", namespace=p.EXTDISCO)
                        callbacks.pop(received.getID())(None, reply)
                        return False
                    if peer.bare == own.bare:
                        return False
                    target = endpoints[peer.bare]
                    try:
                        if isinstance(received, Iq):
                            target._iq(None, received, None)
                        else:
                            props = SimpleNamespace(
                                is_mam_message=False, is_sent_carbon=False
                            )
                            target._message(None, received, props)
                    except NodeProcessed:
                        pass
                    return False

                GLib.idle_add(deliver)
                return stanza.getID()

            con.send_stanza.side_effect = send
            con.connection.send_stanza.side_effect = send
            module = SecureCalls(con)
            module._ring = Mock()
            module._show = lambda peer, text, **kw: statuses.append(text)
            module._status = statuses.append
            endpoints[own.bare] = module
            return module

        alice, bob = (
            client(name, backend)
            for name, backend in zip(("alice", "bob"), self.backends, strict=True)
        )
        self.addCleanup(alice.end, send=False)
        self.addCleanup(bob.end, send=False)
        return alice, bob, statuses

    def test_unsolicited_jingle_never_opens_media(self):
        from nbxmpp.protocol import Iq
        from nbxmpp.protocol import NodeProcessed

        _alice, bob, _statuses = self._modules()
        iq = Iq(
            typ="set", to="bob@example.org/desktop", frm="alice@example.org/desktop"
        )
        node = jingle()
        node.setAttr("sid", "unsolicited")
        node.setAttr("action", "session-initiate")
        iq.addChild(node=node)
        with self.assertRaises(NodeProcessed):
            bob._iq(None, iq, None)
        self.assertIsNone(bob.call)

    def test_proceed_without_verification_fails_closed(self):
        from types import SimpleNamespace

        from nbxmpp.protocol import JID
        from nbxmpp.protocol import Message
        from nbxmpp.protocol import NodeProcessed

        alice, _bob, statuses = self._modules()
        alice._new_call(JID.from_string("bob@example.org"), "test-call", True)
        message = Message(
            node='<message from="bob@example.org/phone" type="chat">'
            '<proceed xmlns="urn:xmpp:jingle-message:0" id="test-call"/></message>'
        )
        with self.assertRaises(NodeProcessed):
            alice._message(
                None,
                message,
                SimpleNamespace(is_mam_message=False, is_sent_carbon=False),
            )
        self.assertIsNone(alice.call)
        self.assertIn("Expected exactly one device", statuses)

    def test_unrelated_bad_proposal_does_not_end_active_call(self):
        from types import SimpleNamespace

        from nbxmpp.protocol import JID
        from nbxmpp.protocol import Message
        from nbxmpp.protocol import NodeProcessed

        alice, _bob, _statuses = self._modules()
        call = alice._new_call(
            JID.from_string("bob@example.org/phone"), "test-call", True
        )
        alice._contact = Mock(side_effect=p.CallError("Not a contact"))
        message = Message(
            node='<message from="stranger@example.org/phone" type="chat">'
            '<propose xmlns="urn:xmpp:jingle-message:0" id="bad-call"/></message>'
        )
        with self.assertRaises(NodeProcessed):
            alice._message(
                None,
                message,
                SimpleNamespace(is_mam_message=False, is_sent_carbon=False),
            )
        self.assertIs(alice.call, call)

    def test_turn_credentials_discovery(self):
        from nbxmpp.protocol import Iq
        from nbxmpp.protocol import JID

        alice, _bob, _statuses = self._modules()
        call = alice._new_call(
            JID.from_string("bob@example.org/phone"), "test-call", True
        )
        requests = []

        def request(stanza, callback):
            requests.append(stanza)
            if stanza.getTag("services") is not None:
                result = (
                    '<services xmlns="urn:xmpp:extdisco:2">'
                    '<service type="stun" host="stun.example.org" port="3478" '
                    'transport="udp"/><service type="turn" host="turn.example.org" '
                    'port="3478" transport="udp" restricted="true"/></services>'
                )
            else:
                result = (
                    '<credentials xmlns="urn:xmpp:extdisco:2">'
                    '<service type="turn" host="turn.example.org" port="3478" '
                    'username="test:user" password="test/pass"/></credentials>'
                )
            callback(Iq(node=f'<iq type="result">{result}</iq>'))

        alice._request = request
        results = []
        alice._discover(call, results.extend)
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            results,
            [
                "stun://stun.example.org:3478",
                "turn://test%3Auser:test%2Fpass@turn.example.org:3478?transport=udp",
            ],
        )

    @unittest.skipUnless(
        os.environ.get("GAJIM_TEST_CALL_MEDIA") == "1", "Opt-in local WebRTC test"
    )
    def test_complete_call_and_hangup(self):
        from gi.repository import GLib
        from nbxmpp.protocol import JID

        from gajim.common.calls.media import AudioTransport

        alice, bob, statuses = self._modules()

        def factory(**kwargs):
            return AudioTransport(test_audio=True, **kwargs)

        with (
            patch(
                "gajim.common.modules.secure_calls.AudioTransport", side_effect=factory
            ),
            patch.object(app, "account_is_connected", return_value=True),
            patch.object(app.settings, "get", return_value=True),
        ):
            loop = GLib.MainLoop()
            alice.start(JID.from_string("bob@example.org"))
            complete = []

            def advance():
                if bob.call is not None and bob.call.state == "ringing":
                    self.assertIsNone(bob.call.media)
                    bob.accept()
                if all(
                    m.call and m.call.media and m.call.media.received_buffers > 10
                    for m in (alice, bob)
                ):
                    complete.append(True)
                    alice.end()
                if complete and bob.call is None:
                    loop.quit()
                    return False
                return True

            poll = GLib.timeout_add(50, advance)
            timeout = GLib.timeout_add_seconds(15, loop.quit)
            try:
                loop.run()
                self.assertTrue(complete, statuses)
                self.assertIsNone(alice.call)
                self.assertIsNone(bob.call)
            finally:
                for source in (poll, timeout):
                    if GLib.MainContext.default().find_source_by_id(source):
                        GLib.source_remove(source)
