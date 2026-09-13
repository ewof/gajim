# This file is part of Gajim.
# SPDX-License-Identifier: GPL-3.0-only

"""OMEMO 0.3 call verification compatible with Monal and Conversations.

Never encrypt a fingerprint to every device as for a chat message: exactly one
verified peer device is pinned for the entire call. Use omemo-dr's established
AES/Signal implementation; don't change device lists to select a recipient.
"""

import time
from base64 import b64decode
from base64 import b64encode

from nbxmpp.simplexml import Node
from omemo_dr.aes import aes_encrypt
from omemo_dr.const import OMEMOTrust
from omemo_dr.session_manager import OMEMOSessionManager
from omemo_dr.structs import OMEMOMessage

from gajim.common.calls.protocol import CallError
from gajim.common.calls.protocol import fingerprint
from gajim.common.calls.protocol import number
from gajim.common.calls.protocol import OMEMO
from gajim.common.calls.protocol import one
from gajim.common.calls.protocol import VERIFY

TRUST_REQUIRED = "Verify the other device in the chat's OMEMO settings before calling"


def trust_recent_keys(backend: OMEMOSessionManager, address: str) -> int:
    """Explicitly approve this contact's keys seen within the last 24 hours."""
    now = time.time()
    keys = [
        info
        for info in backend.get_identity_infos(address)
        if info.last_seen is not None
        and now - 86400 <= info.last_seen <= now
        and info.trust != OMEMOTrust.VERIFIED
    ]
    for info in keys:
        backend.set_trust(address, info.public_key, OMEMOTrust.VERIFIED)
    return len(keys)


class CallSecurity:
    def __init__(self, backend: OMEMOSessionManager, address: str) -> None:
        self.backend = backend
        self.address = address
        self.device: int | None = None
        self.identity: str | None = None

    def pin(self, device: int) -> None:
        if self.device is not None and self.device != device:
            raise CallError("The other device changed during call setup")
        infos = [
            i
            for i in self.backend.get_identity_infos(self.address)
            if i.device_id == device and i.trust == OMEMOTrust.VERIFIED
        ]
        if len(infos) != 1:
            raise CallError(TRUST_REQUIRED)
        identity = infos[0].public_key.get_fingerprint()
        if self.identity is not None and identity != self.identity:
            raise CallError("The other device's identity changed during the call")
        self.device, self.identity = device, identity

    def check_trust(self) -> None:
        if self.device is None:
            raise CallError("No authenticated peer device")
        self.pin(self.device)

    def encrypt(self, value: str, setup: str) -> Node:
        self.check_trust()
        result = aes_encrypt(fingerprint(value))
        # omemo-dr 1.x has no public device-targeted payload method. Keep this
        # single adapter covered by a real two-device roundtrip test.
        encrypted_key, prekey = self.backend._get_whisper_message(
            self.address, self.device, result.key
        )
        node = Node(
            "fingerprint", attrs={"xmlns": VERIFY, "hash": "sha-256", "setup": setup}
        )
        encrypted = node.addChild("encrypted", namespace=OMEMO)
        header = encrypted.addChild(
            "header", attrs={"sid": str(self.backend.get_our_device())}
        )
        attrs = {"rid": str(self.device)}
        if prekey:
            attrs["prekey"] = "true"
        header.addChild("key", attrs=attrs, payload=b64encode(encrypted_key).decode())
        header.addChild("iv", payload=b64encode(result.iv).decode())
        encrypted.addChild("payload", payload=b64encode(result.payload).decode())
        return node

    def decrypt(self, node: Node) -> str:
        encrypted = one(node, "encrypted", OMEMO)
        header = one(encrypted, "header", OMEMO)
        device = number(header.getAttr("sid"), 1, 2**31 - 1)
        self.pin(device)
        key = one(header, "key", OMEMO)
        recipient = number(key.getAttr("rid"), 1, 2**31 - 1)
        if recipient != self.backend.get_our_device():
            raise CallError("Call fingerprint was encrypted for another device")
        prekey = key.getAttr("prekey")
        if prekey not in (None, "false", "0", "true", "1"):
            raise CallError("Invalid OMEMO prekey flag")

        def decode(value: str, limit: int) -> bytes:
            if len(value) > limit:
                raise CallError("Oversized OMEMO call envelope")
            try:
                return b64decode(value, validate=True)
            except ValueError:
                raise CallError("Invalid OMEMO call envelope") from None

        iv = decode(one(header, "iv", OMEMO).getData(), 64)
        if len(iv) not in (12, 16):
            raise CallError("Invalid OMEMO IV")
        message = OMEMOMessage(
            sid=device,
            iv=iv,
            keys={recipient: (decode(key.getData(), 8192), prekey in ("true", "1"))},
            payload=decode(one(encrypted, "payload", OMEMO).getData(), 1024),
        )
        try:
            plaintext, identity, trust = self.backend.decrypt_message(
                message, self.address
            )
        except Exception:
            raise CallError("Could not authenticate the call with OMEMO") from None
        if trust != OMEMOTrust.VERIFIED or identity != self.identity:
            raise CallError("Call identity does not match the verified device")
        self.check_trust()
        return fingerprint(plaintext)
