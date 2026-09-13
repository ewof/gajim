# This file is part of Gajim.
# SPDX-License-Identifier: GPL-3.0-only

"""Strict OMEMO-authenticated, one-to-one audio calls (JMI + Jingle)."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from dataclasses import field
from urllib.parse import quote

from gi.repository import GLib
from nbxmpp.protocol import Error
from nbxmpp.protocol import Iq
from nbxmpp.protocol import JID
from nbxmpp.protocol import Message
from nbxmpp.protocol import NodeProcessed
from nbxmpp.simplexml import Node
from nbxmpp.structs import StanzaHandler

from gajim.common import app
from gajim.common.calls import protocol as p
from gajim.common.calls.media import AudioTransport
from gajim.common.calls.media import availability
from gajim.common.calls.security import CallSecurity
from gajim.common.calls.security import TRUST_REQUIRED
from gajim.common.const import SimpleClientState
from gajim.common.modules.base import BaseModule
from gajim.common.modules.contacts import BareContact


@dataclass
class AudioCall:
    sid: str
    peer: JID
    outgoing: bool
    security: CallSecurity
    relay_only: bool = True
    state: str = "ringing"
    media: AudioTransport | None = None
    local: p.AudioDescription | None = None
    remote: p.AudioDescription | None = None
    timeout: int = 0
    trust_timer: int = 0
    offered: bool = False
    candidates: list[str] | None = None
    remote_transports: list[Node] = field(default_factory=list)
    candidate_count: int = 0


class SecureCalls(BaseModule):
    def __init__(self, con) -> None:
        super().__init__(con)
        self.call: AudioCall | None = None
        self.window = None
        self._recent: dict[tuple[str, str], float] = {}
        self._iq_ids: set[str] = set()
        self.handlers = [
            StanzaHandler(
                name="message", ns=p.JMI, callback=self._message, priority=49
            ),
            # Consume all Jingle requests before the disabled legacy RTP path.
            StanzaHandler(
                name="iq", typ="set", ns=p.JINGLE, callback=self._iq, priority=49
            ),
        ]
        con.connect_signal("state-changed", self._state_changed)

    @property
    def available(self) -> bool:
        return availability() is None

    def _state_changed(self, _client, _signal: str, state: SimpleClientState) -> None:
        if self.call is not None and not state.is_connected:
            self.end("Account disconnected", send=False)

    def _contact(self, jid: JID) -> BareContact:
        contact = self._client.get_module("Contacts").get_contact(jid.new_as_bare())
        if not isinstance(contact, BareContact) or not contact.is_in_roster:
            raise p.CallError("Add this person to your contacts before calling")
        if contact.is_blocked:
            raise p.CallError("This contact is blocked")
        if contact.settings.get("encryption") != "OMEMO":
            raise p.CallError(
                "Enable OMEMO in this chat and verify the other device before calling"
            )
        if jid.bare == self._client.get_own_jid().bare:
            raise p.CallError("Calling your own account is not supported")
        return contact

    def _show(self, peer: JID, text: str, *, incoming: bool = False) -> None:
        from gajim.gtk.secure_call import SecureCallWindow

        if self.window is not None and self.window.peer != peer.new_as_bare():
            self.window.close()
        if self.window is None:
            self.window = SecureCallWindow(self, peer)
        self.window.update(text, incoming=incoming, active=self.call is not None)
        self.window.present()

    def _status(self, text: str) -> None:
        if self.window is not None:
            self.window.update(text, active=self.call is not None)

    def _ring(self, direction: str | None) -> None:
        from gajim.gtk import sound

        if direction is None:
            sound.stop()
        else:
            sound.play(f"{direction}-call-sound", self._account, loop=True)

    def _new_call(self, peer: JID, sid: str, outgoing: bool) -> AudioCall:
        if self.window is not None:
            self.window.close()
        backend = self._client.get_module("OMEMO").backend
        call = AudioCall(
            sid,
            peer,
            outgoing,
            CallSecurity(backend, peer.bare),
            relay_only=not app.settings.get("calls_do_not_relay"),
            candidates=[],
        )
        self.call = call
        self._deadline(call, 60)
        return call

    def start(self, jid: JID) -> None:
        if self.call is not None:
            self._show(self.call.peer, "A call is already in progress")
            return
        try:
            self._contact(jid)
            error = availability()
            if error:
                raise p.CallError(error)
            backend = self._client.get_module("OMEMO").backend
            from omemo_dr.const import OMEMOTrust

            if not backend.get_identity_infos(jid.bare, trust=OMEMOTrust.VERIFIED):
                raise p.CallError(TRUST_REQUIRED)
            call = self._new_call(jid.new_as_bare(), str(uuid.uuid4()), True)
            self._show(jid, "Calling… Waiting for the other person to answer")
            node = self._jmi(call.peer, "propose", call.sid)
            node.getTag("propose", namespace=p.JMI).addChild(
                "description", namespace=p.RTP, attrs={"media": "audio"}
            )
            self._client.send_stanza(node)
            self._ring("outgoing")
        except p.CallError as error:
            if self.call is not None:
                self.end(str(error))
            else:
                self._show(jid, str(error))

    def _jmi(self, peer: JID, action: str, sid: str) -> Message:
        message = Message(to=peer, typ="chat")
        message.addChild(action, namespace=p.JMI, attrs={"id": sid})
        # Store hints are needed by mobile push implementations. There is no
        # message body and no media or credentials in these notifications.
        message.addChild("store", namespace="urn:xmpp:hints")
        return message

    def _send_jmi(self, peer: JID, action: str, sid: str) -> None:
        self._client.send_stanza(self._jmi(peer, action, sid))

    def _message(self, _client, stanza: Message, properties) -> None:
        nodes = [n for n in stanza.getChildren() if n.getNamespace() == p.JMI]
        if len(nodes) != 1 or properties.is_mam_message:
            raise NodeProcessed
        node = nodes[0]
        action, sid, sender = node.getName(), node.getAttr("id"), stanza.getFrom()
        if sender is None or not sid or len(sid) > 128:
            raise NodeProcessed
        timestamp = getattr(properties, "user_timestamp", None)
        if timestamp is not None and time.time() - timestamp > 60:
            raise NodeProcessed
        if getattr(properties, "has_server_delay", False):
            if time.time() - properties.timestamp > 60:
                raise NodeProcessed
        call = self.call
        # Carbon copies of an accept/proceed on another device dismiss ringing
        # here, but never initiate a second media session or open the microphone.
        if sender.bare == self._client.get_own_jid().bare:
            if (
                call is not None
                and not call.outgoing
                and call.sid == sid
                and call.state == "ringing"
                and action in ("accept", "proceed", "reject")
            ):
                self.end("Call answered or declined on another device", send=False)
            raise NodeProcessed
        if properties.is_sent_carbon:
            raise NodeProcessed
        try:
            if action == "propose":
                self._propose(sender, sid, node)
            elif call is not None and sid == call.sid and sender.bare == call.peer.bare:
                if action == "proceed" and call.outgoing and call.state == "ringing":
                    if not sender.is_full:
                        raise p.CallError(
                            "The answering client did not identify its resource"
                        )
                    device = p.one(node, "device", p.VERIFY)
                    call.security.pin(p.number(device.getAttr("id"), 1, 2**31 - 1))
                    call.peer = sender
                    call.state = "connecting"
                    self._ring(None)
                    self._status("Verifying and connecting…")
                    self._deadline(call, 45)
                    self._discover(
                        call, lambda servers: self._outgoing_media(call, servers)
                    )
                elif action == "ringing" and call.outgoing and call.state == "ringing":
                    self._status("Ringing…")
                elif action in ("reject", "retract", "finish"):
                    if call.peer.is_full and sender != call.peer:
                        raise NodeProcessed
                    self.end("Call ended by the other person", send=False)
        except p.CallError as error:
            if action != "propose" and self.call is call and call is not None:
                self.end(str(error), reason="security-error")
        raise NodeProcessed

    def _propose(self, sender: JID, sid: str, node: Node) -> None:
        self._contact(sender)
        now = time.monotonic()
        self._recent = {key: at for key, at in self._recent.items() if now - at < 120}
        key = (sender.bare, sid)
        if key in self._recent:
            return
        # Bound memory and repeated ringing from even a roster contact.
        if len(self._recent) >= 64 or any(
            jid == sender.bare and now - at < 5
            for (jid, _sid), at in self._recent.items()
        ):
            return
        self._recent[key] = now
        descriptions = node.getTags("description", namespace=p.RTP)
        if (
            not self.available
            or self.call is not None
            or len(descriptions) != 1
            or descriptions[0].getAttr("media") != "audio"
        ):
            self._send_jmi(sender, "reject", sid)
            return
        self._new_call(sender, sid, False)
        self._show(sender, "Incoming secure audio call", incoming=True)
        self._ring("incoming")
        self._send_jmi(sender, "ringing", sid)

    def accept(self) -> None:
        call = self.call
        if call is None or call.outgoing or call.state != "ringing":
            return
        call.state = "connecting"
        self._ring(None)
        self._deadline(call, 45)
        self._status("Verifying and connecting…")
        self._send_jmi(self._client.get_own_jid().new_as_bare(), "accept", call.sid)
        message = self._jmi(call.peer, "proceed", call.sid)
        message.getTag("proceed", namespace=p.JMI).addChild(
            "device",
            namespace=p.VERIFY,
            attrs={"id": str(call.security.backend.get_our_device())},
        )
        self._client.send_stanza(message)

    def _deadline(self, call: AudioCall, seconds: int) -> None:
        if call.timeout:
            GLib.source_remove(call.timeout)

        def expired():
            call.timeout = 0
            if self.call is call:
                if call.state == "ringing":
                    text = "Call was not answered"
                elif call.relay_only:
                    text = (
                        "The relayed call timed out. Check TURN connectivity and "
                        "firewall ports. No direct connection was attempted."
                    )
                else:
                    text = (
                        "Call timed out. A TURN relay may be needed on these networks."
                    )
                self.end(text, reason="connectivity-error")
            return GLib.SOURCE_REMOVE

        call.timeout = GLib.timeout_add_seconds(seconds, expired)

    def _request(self, stanza: Iq, callback) -> None:
        def response(_client, result):
            self._iq_ids.discard(stanza.getID())
            if result is not None and result.getFrom() not in (None, stanza.getTo()):
                result = None
            callback(result)

        id_ = self._client.connection.send_stanza(stanza, callback=response, timeout=10)
        self._iq_ids.add(id_)

    def _discover(self, call: AudioCall, callback) -> None:
        domain = self._client.get_own_jid().domain
        stanza = Iq(typ="get", to=domain)
        stanza.addChild("services", namespace=p.EXTDISCO)

        def discovered(result):
            if self.call is not call:
                return
            servers = []
            services = (
                result.getTag("services", namespace=p.EXTDISCO)
                if result is not None and result.getType() == "result"
                else None
            )
            pending = []
            if services is not None:
                for service in services.getTags("service", namespace=p.EXTDISCO)[:16]:
                    if service.getAttr("type") in ("turn", "turns") and (
                        not service.getAttr("username")
                        or not service.getAttr("password")
                    ):
                        pending.append(service)
                        continue
                    try:
                        uri = self._service_uri(service)
                        if uri is not None:
                            servers.append(uri)
                    except p.CallError:
                        continue

            def ready():
                if self.call is not call:
                    return
                try:
                    callback(servers)
                except p.CallError as error:
                    self.end(str(error))
                except Exception:
                    self.end("Could not initialize the audio transport")

            if not pending:
                ready()
                return
            remaining = len(pending)

            def credentials_received(service, response):
                nonlocal remaining
                if self.call is not call:
                    return
                credentials = (
                    response.getTag("credentials", namespace=p.EXTDISCO)
                    if response is not None and response.getType() == "result"
                    else None
                )
                if credentials is not None:
                    for entry in credentials.getTags("service", namespace=p.EXTDISCO):
                        if all(
                            entry.getAttr(key) == service.getAttr(key)
                            for key in ("host", "type", "port")
                        ):
                            merged = Node(node=str(service))
                            for key in ("username", "password"):
                                if entry.getAttr(key):
                                    merged.setAttr(key, entry.getAttr(key))
                            try:
                                uri = self._service_uri(merged)
                                if uri is not None:
                                    servers.append(uri)
                            except p.CallError:
                                pass
                remaining -= 1
                if remaining == 0:
                    ready()

            for service in pending:
                request = Iq(typ="get", to=domain)
                credentials = request.addChild("credentials", namespace=p.EXTDISCO)
                credentials.addChild(
                    "service",
                    attrs={
                        key: service.getAttr(key)
                        for key in ("host", "type", "port")
                        if service.getAttr(key) is not None
                    },
                )
                self._request(
                    request,
                    lambda result, service=service: credentials_received(
                        service, result
                    ),
                )

        self._request(stanza, discovered)

    @staticmethod
    def _service_uri(service: Node) -> str | None:
        kind = service.getAttr("type")
        if kind not in ("stun", "turn", "turns"):
            return None
        host = service.getAttr("host") or ""
        import ipaddress
        import re

        try:
            address = ipaddress.ip_address(host)
            host = f"[{address}]" if address.version == 6 else str(address)
        except ValueError:
            if not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", host):
                raise p.CallError("Invalid STUN/TURN host")
        port = p.number(service.getAttr("port"), 1, 65535)
        transport = service.getAttr("transport") or "udp"
        if transport not in ("udp", "tcp"):
            return None
        if kind == "stun":
            return f"stun://{host}:{port}" if transport == "udp" else None
        user, password = service.getAttr("username"), service.getAttr("password")
        if not user or not password:
            return None
        credentials = f"{quote(user, safe='')}:{quote(password, safe='')}"
        return f"{kind}://{credentials}@{host}:{port}?transport={transport}"

    def _make_media(
        self, call: AudioCall, servers: list[str], payload: int = 111
    ) -> AudioTransport:
        return AudioTransport(
            payload=payload,
            ice_servers=servers,
            relay_only=call.relay_only,
            on_candidate=lambda candidate: self._candidate(call, candidate),
            on_connected=lambda: self._connected(call),
            on_error=lambda text: self.end(text) if self.call is call else None,
        )

    def _outgoing_media(self, call: AudioCall, servers: list[str]) -> None:
        call.media = self._make_media(call, servers)
        call.media.create_description(
            True, lambda sdp: self._local_description(call, sdp, True)
        )

    def _local_description(self, call: AudioCall, sdp: str, offer: bool) -> None:
        if self.call is not call:
            return
        try:
            content, local = p.sdp_to_description(
                sdp,
                name=call.remote.name if call.remote else None,
                relay_only=call.relay_only,
            )
            call.local = local
            transport = p.one(content, "transport", p.ICE)
            transport.addChild(
                node=call.security.encrypt(local.fingerprint, local.setup)
            )
            node = self._jingle(call, "session-initiate" if offer else "session-accept")
            node.addChild(node=content)
            call.offered = True
            self._send_jingle(call, node)
            for candidate in call.candidates or []:
                self._candidate(call, candidate)
            call.candidates = []
        except Exception as error:
            self.end(
                str(error)
                if isinstance(error, p.CallError)
                else "Could not authenticate the call",
                reason="security-error",
            )

    def _candidate(self, call: AudioCall, candidate: str) -> None:
        if self.call is not call:
            return
        if call.local is None or not call.offered:
            if call.candidates is not None and len(call.candidates) < 64:
                call.candidates.append(candidate)
            return
        try:
            child = p.candidate_to_xml(candidate)
            if child is None or (call.relay_only and child.getAttr("type") != "relay"):
                return
            node = self._jingle(call, "transport-info")
            content = node.addChild(
                "content", attrs={"creator": "initiator", "name": call.local.name}
            )
            transport = content.addChild(
                "transport",
                namespace=p.ICE,
                attrs={"ufrag": call.local.ufrag, "pwd": call.local.password},
            )
            transport.addChild(node=child)
            self._send_jingle(call, node, critical=False)
        except p.CallError:
            self.end("Invalid local network candidate")

    def _jingle(self, call: AudioCall, action: str) -> Node:
        attrs = {"xmlns": p.JINGLE, "sid": call.sid, "action": action}
        if action == "session-initiate":
            attrs["initiator"] = str(self._client.get_own_jid())
        elif action == "session-accept":
            attrs["responder"] = str(self._client.get_own_jid())
        return Node("jingle", attrs=attrs)

    def _send_jingle(
        self, call: AudioCall, node: Node, *, critical: bool = True
    ) -> None:
        stanza = Iq(typ="set", to=call.peer)
        stanza.addChild(node=node)

        def replied(result):
            if (
                self.call is call
                and critical
                and (result is None or result.getType() != "result")
            ):
                self.end("The other client did not accept the secure call")

        self._request(stanza, replied)

    def _iq(self, _client, stanza: Iq, _properties) -> None:
        call = self.call
        node = stanza.getTag("jingle", namespace=p.JINGLE)
        sender = stanza.getFrom()
        if (
            call is None
            or node is None
            or node.getAttr("sid") != call.sid
            or sender is None
            or sender.bare != call.peer.bare
            or not sender.is_full
            or (call.peer.is_full and call.peer != sender)
        ):
            self._client.send_stanza(Error(stanza, "item-not-found"))
            raise NodeProcessed
        try:
            action = node.getAttr("action")
            if action == "session-terminate":
                self._client.send_stanza(stanza.buildReply("result"))
                self.end("Call ended by the other person", send=False)
                raise NodeProcessed
            if action in ("session-initiate", "session-accept"):
                offer = action == "session-initiate"
                if (
                    offer == call.outgoing
                    or call.state != "connecting"
                    or call.remote is not None
                ):
                    raise p.CallError("Unexpected call negotiation")
                _content, _description, transport = p.audio_content(node)
                fp = p.verified_fingerprint_node(transport)
                authenticated = call.security.decrypt(fp)
                sdp, remote = p.description_to_sdp(node, authenticated, offer=offer)
                if call.local is not None and (
                    call.local.name != remote.name
                    or call.local.payload != remote.payload
                ):
                    raise p.CallError("The answer changed the offered audio stream")
                call.remote = remote
                call.peer = sender
                if offer:
                    # Keep the original Jingle id pinned to the accepted JMI.
                    self._discover(
                        call, lambda servers: self._incoming_media(call, servers, sdp)
                    )
                else:
                    if call.media is None:
                        raise p.CallError("No pending audio offer")
                    call.media.set_remote(
                        sdp, authenticated, offer=False, callback=lambda: None
                    )
                    self._flush_remote_candidates(call)
            elif action == "transport-info":
                self._remote_candidates(call, node)
            elif action != "session-info":
                self._client.send_stanza(Error(stanza, "feature-not-implemented"))
                raise NodeProcessed
            self._client.send_stanza(stanza.buildReply("result"))
        except p.CallError as error:
            self._client.send_stanza(Error(stanza, "not-acceptable"))
            self.end(str(error), reason="security-error")
        except NodeProcessed:
            raise
        except Exception:
            self._client.send_stanza(Error(stanza, "bad-request"))
            self.end("Invalid call negotiation", reason="security-error")
        raise NodeProcessed

    def _incoming_media(self, call: AudioCall, servers: list[str], sdp: str) -> None:
        if call.remote is None:
            return
        call.media = self._make_media(call, servers, call.remote.payload)
        call.media.set_remote(
            sdp,
            call.remote.fingerprint,
            offer=True,
            callback=lambda: call.media.create_description(
                False, lambda value: self._local_description(call, value, False)
            ),
        )
        self._flush_remote_candidates(call)

    def _flush_remote_candidates(self, call: AudioCall) -> None:
        queued, call.remote_transports = call.remote_transports, []
        for node in queued:
            self._remote_candidates(call, node)

    def _remote_candidates(self, call: AudioCall, node: Node) -> None:
        if call.state not in ("connecting", "connected"):
            raise p.CallError("ICE candidates arrived before accepting the call")
        if call.remote is None or call.media is None:
            if len(call.remote_transports) >= 16 or len(str(node)) > 32768:
                raise p.CallError("Too many pending ICE candidates")
            call.remote_transports.append(Node(node=str(node)))
            return
        content = p.one(node, "content", p.JINGLE)
        if call.remote is None or content.getAttr("name") != call.remote.name:
            raise p.CallError("ICE candidate for an unknown stream")
        transport = p.one(content, "transport", p.ICE)
        if transport.getAttr("ufrag") not in (
            None,
            call.remote.ufrag,
        ) or transport.getAttr("pwd") not in (None, call.remote.password):
            raise p.CallError("ICE restarts are not supported")
        candidates = transport.getTags("candidate", namespace=p.ICE)
        call.candidate_count += len(candidates)
        if len(candidates) > 64 or call.candidate_count > 256:
            raise p.CallError("Too many ICE candidates")
        for candidate in candidates:
            if candidate.getAttr("protocol") != "udp":
                continue
            value = p.candidate_to_sdp(candidate)
            if call.media is not None:
                call.media.add_candidate(value)

    def _connected(self, call: AudioCall) -> None:
        if self.call is not call:
            return
        call.security.check_trust()
        call.state = "connected"
        if call.timeout:
            GLib.source_remove(call.timeout)
            call.timeout = 0
        self._status("Connected — encrypted and verified with OMEMO")

        def check():
            if self.call is not call:
                return GLib.SOURCE_REMOVE
            try:
                self._contact(call.peer)
                call.security.check_trust()
            except p.CallError:
                call.trust_timer = 0
                self.end(
                    "Call stopped because device trust changed", reason="security-error"
                )
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        call.trust_timer = GLib.timeout_add_seconds(1, check)

    def mute(self, muted: bool) -> None:
        if self.call is not None and self.call.media is not None:
            self.call.media.set_muted(muted)

    def end(
        self, text: str = "Call ended", *, send: bool = True, reason: str = "success"
    ) -> None:
        call, self.call = self.call, None
        if call is None:
            return
        self._ring(None)
        if call.media is not None:
            call.media.close()
        for timer in (call.timeout, call.trust_timer):
            if timer:
                GLib.source_remove(timer)
        if send and app.account_is_connected(self._account):
            if call.state == "ringing":
                self._send_jmi(
                    call.peer, "retract" if call.outgoing else "reject", call.sid
                )
            else:
                node = self._jingle(call, "session-terminate")
                node.addChild("reason").addChild(reason)
                self._send_jingle(call, node, critical=False)
                self._send_jmi(call.peer, "finish", call.sid)
        self._status(text)

    def cleanup(self) -> None:
        self.end("Account disconnected", send=False)
        if self.window is not None:
            self.window.close()
        super().cleanup()
