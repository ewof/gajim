# This file is part of Gajim.
# SPDX-License-Identifier: GPL-3.0-only

"""The deliberately small, audio-only Jingle/WebRTC interoperability profile.

All remote values are validated before constructing SDP. The fingerprint passed
to ``description_to_sdp`` must have been authenticated by OMEMO, never taken from
a cleartext DTLS element. Only one bidirectional Opus stream is supported.
"""

import ipaddress
import re
import secrets
from dataclasses import dataclass

from nbxmpp.simplexml import Node

JINGLE = "urn:xmpp:jingle:1"
JMI = "urn:xmpp:jingle-message:0"
RTP = "urn:xmpp:jingle:apps:rtp:1"
ICE = "urn:xmpp:jingle:transports:ice-udp:1"
DTLS = "urn:xmpp:jingle:apps:dtls:0"
VERIFY = "http://gultsch.de/xmpp/drafts/omemo/dlts-srtp-verification"
OMEMO = "eu.siacs.conversations.axolotl"
GROUP = "urn:xmpp:jingle:apps:grouping:0"
EXTDISCO = "urn:xmpp:extdisco:2"
FEATURES = [JINGLE, JMI, RTP, RTP + ":audio", ICE, DTLS]


class CallError(ValueError):
    """An unsupported or unsafe call negotiation."""


def token(value: str | None, limit: int = 256) -> str:
    if (
        not value
        or len(value) > limit
        or not re.fullmatch(r"[a-zA-Z0-9_+./=-]+", value)
    ):
        raise CallError("Invalid call negotiation value")
    return value


def number(value: str | None, low: int, high: int) -> int:
    if value is None or not value.isascii() or not value.isdecimal():
        raise CallError("Invalid numeric call parameter")
    result = int(value)
    if not low <= result <= high:
        raise CallError("Call parameter outside supported range")
    return result


def fingerprint(value: str) -> str:
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){31}[0-9a-fA-F]{2}", value):
        raise CallError("Invalid SHA-256 certificate fingerprint")
    return value.upper()


def one(node: Node, name: str, namespace: str) -> Node:
    nodes = [child for child in node.getChildren() if child.getName() == name]
    if len(nodes) != 1 or nodes[0].getNamespace() != namespace:
        raise CallError(f"Expected exactly one {name}")
    return nodes[0]


def audio_content(jingle: Node) -> tuple[Node, Node, Node]:
    content = one(jingle, "content", JINGLE)
    if content.getAttr("creator") != "initiator":
        raise CallError("Unexpected content creator")
    if content.getAttr("senders") not in (None, "both"):
        raise CallError("Only bidirectional audio is supported")
    token(content.getAttr("name"))
    description = one(content, "description", RTP)
    if description.getAttr("media") != "audio":
        raise CallError("Only audio calls are supported")
    if description.getTag("encryption") is not None:
        raise CallError("Legacy media encryption is not supported")
    transport = one(content, "transport", ICE)
    return content, description, transport


def verified_fingerprint_node(transport: Node) -> Node:
    # Reject cleartext and ambiguous fingerprints, including mixed namespaces.
    nodes = [n for n in transport.getChildren() if n.getName() == "fingerprint"]
    if len(nodes) != 1 or nodes[0].getNamespace() != VERIFY:
        raise CallError("This call requires OMEMO certificate verification")
    node = nodes[0]
    if node.getAttr("hash") != "sha-256":
        raise CallError("This call requires SHA-256 certificate fingerprints")
    if node.getAttr("setup") not in ("actpass", "active", "passive"):
        raise CallError("Invalid DTLS role")
    if node.getData().strip():
        raise CallError("Unexpected cleartext fingerprint")
    one(node, "encrypted", OMEMO)
    return node


def candidate_to_sdp(node: Node) -> str:
    foundation = token(node.getAttr("foundation"), 32)
    component = number(node.getAttr("component"), 1, 2)
    protocol = node.getAttr("protocol")
    if protocol != "udp":
        raise CallError("Only UDP ICE candidates are supported")
    priority = number(node.getAttr("priority"), 0, 2**32 - 1)
    try:
        address = str(ipaddress.ip_address(node.getAttr("ip")))
    except ValueError:
        raise CallError("Invalid ICE address") from None
    port = number(node.getAttr("port"), 1, 65535)
    kind = node.getAttr("type")
    if kind not in ("host", "srflx", "prflx", "relay"):
        raise CallError("Invalid ICE candidate type")
    return (
        f"candidate:{foundation} {component} UDP {priority} {address} {port} typ {kind}"
    )


def candidate_to_xml(value: str) -> Node | None:
    fields = value.removeprefix("a=").removeprefix("candidate:").split()
    if len(fields) < 8 or fields[2].lower() != "udp":
        return None
    node = Node(
        "candidate",
        attrs={
            "xmlns": ICE,
            "foundation": fields[0],
            "component": fields[1],
            "protocol": "udp",
            "priority": fields[3],
            "ip": fields[4],
            "port": fields[5],
            "type": fields[7],
            "generation": "0",
            "id": secrets.token_hex(8),
            "network": "0",
        },
    )
    candidate_to_sdp(node)
    return node


@dataclass(frozen=True)
class AudioDescription:
    name: str
    payload: int
    ufrag: str
    password: str
    fingerprint: str
    setup: str


def description_to_sdp(
    jingle: Node, authenticated: str, *, offer: bool
) -> tuple[str, AudioDescription]:
    content, description, transport = audio_content(jingle)
    fp = verified_fingerprint_node(transport)
    setup = fp.getAttr("setup")
    if setup not in (("actpass",) if offer else ("active", "passive")):
        raise CallError("Unexpected DTLS offer/answer role")
    name = token(content.getAttr("name"))
    ufrag = token(transport.getAttr("ufrag"))
    password = token(transport.getAttr("pwd"))
    if len(ufrag) < 4 or len(password) < 22:
        raise CallError("Invalid ICE credentials")
    if description.getTag("rtcp-mux", namespace=RTP) is None:
        raise CallError("RTCP multiplexing is required")
    payloads = description.getTags("payload-type", namespace=RTP)
    if len(payloads) > 32:
        raise CallError("Too many codecs")
    opus = [
        p
        for p in payloads
        if (p.getAttr("name") or "").lower() == "opus"
        and p.getAttr("clockrate") == "48000"
        and p.getAttr("channels") == "2"
    ]
    if len(opus) != 1:
        raise CallError("The other client must support Opus audio")
    payload = number(opus[0].getAttr("id"), 96, 127)
    params = []
    for param in opus[0].getTags("parameter", namespace=RTP):
        key = param.getAttr("name")
        # Only the parameters whose grammar and semantics we support.
        if key in (
            "minptime",
            "maxplaybackrate",
            "sprop-maxcapturerate",
            "maxaveragebitrate",
        ):
            params.append(f"{key}={number(param.getAttr('value'), 1, 510000)}")
        elif key in ("stereo", "sprop-stereo", "useinbandfec", "usedtx", "cbr"):
            params.append(f"{key}={number(param.getAttr('value'), 0, 1)}")
    authenticated = fingerprint(authenticated)
    lines = [
        "v=0",
        "o=- 1 1 IN IP4 0.0.0.0",
        "s=-",
        "t=0 0",
        f"m=audio 9 UDP/TLS/RTP/SAVPF {payload}",
        "c=IN IP4 0.0.0.0",
        f"a=mid:{name}",
        "a=sendrecv",
        "a=rtcp-mux",
        f"a=ice-ufrag:{ufrag}",
        f"a=ice-pwd:{password}",
        "a=ice-options:trickle",
        f"a=setup:{setup}",
        f"a=fingerprint:sha-256 {authenticated}",
        f"a=rtpmap:{payload} opus/48000/2",
    ]
    if params:
        lines.append(f"a=fmtp:{payload} " + ";".join(params))
    candidates = transport.getTags("candidate", namespace=ICE)
    if len(candidates) > 64:
        raise CallError("Too many ICE candidates")
    for candidate in candidates:
        if candidate.getAttr("protocol") == "udp":
            lines.append("a=" + candidate_to_sdp(candidate))
    return "\r\n".join(lines) + "\r\n", AudioDescription(
        name, payload, ufrag, password, authenticated, setup
    )


def sdp_to_description(
    sdp: str, *, name: str | None = None, relay_only: bool = False
) -> tuple[Node, AudioDescription]:
    lines = sdp.replace("\r", "").splitlines()

    def attr(key: str) -> str:
        values = [line[len(key) :] for line in lines if line.startswith(key)]
        if len(values) != 1:
            raise CallError(f"Invalid local SDP: {key}")
        return values[0]

    media = attr("m=audio ").split()
    if media[1] != "UDP/TLS/RTP/SAVPF":
        raise CallError("Unencrypted media is prohibited")
    pt = number(media[2], 96, 127)
    mid = token(name or attr("a=mid:"))
    ufrag, password = token(attr("a=ice-ufrag:")), token(attr("a=ice-pwd:"))
    fp = fingerprint(attr("a=fingerprint:sha-256 "))
    setup = attr("a=setup:")
    content = Node(
        "content",
        attrs={"xmlns": JINGLE, "creator": "initiator", "name": mid, "senders": "both"},
    )
    description = content.addChild(
        "description", namespace=RTP, attrs={"media": "audio"}
    )
    payload = description.addChild(
        "payload-type",
        attrs={"id": str(pt), "name": "opus", "clockrate": "48000", "channels": "2"},
    )
    for line in lines:
        if line.startswith(f"a=fmtp:{pt} "):
            for param in line.split(" ", 1)[1].split(";"):
                key, _, value = param.strip().partition("=")
                payload.addChild("parameter", attrs={"name": key, "value": value})
    description.addChild("rtcp-mux")
    transport = content.addChild(
        "transport", namespace=ICE, attrs={"ufrag": ufrag, "pwd": password}
    )
    # Caller supplies an encrypted fingerprint element before sending.
    for line in lines:
        if line.startswith("a=candidate:"):
            node = candidate_to_xml(line)
            if node is not None and (not relay_only or node.getAttr("type") == "relay"):
                transport.addChild(node=node)
    return content, AudioDescription(mid, pt, ufrag, password, fp, setup)
