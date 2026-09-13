# Secure audio calls: initial implementation

This branch adds one-to-one audio calls using Jingle Message Initiation, Jingle
RTP/ICE, GStreamer WebRTC, Opus, and OMEMO-authenticated DTLS fingerprints.
It targets Linux and Monal first. Video, group calls, screen sharing, ICE restarts,
and seamless network changes are not implemented. The old Farstream calling
implementation remains disabled.

## Trying it

1. Restart Gajim from this checkout (`.venv/bin/python launch.py`). Exit the
   existing process first so launching does not just activate the old instance.
2. Add the other person to your contacts and enable OMEMO in the chat on both
   clients. Exchange an encrypted message in each direction to establish sessions.
3. Verify the other person's device fingerprint in Gajim's OMEMO settings,
   comparing it with their device through a trusted channel. This implementation
   deliberately requires **verified**, not merely automatically trusted, keys.
   Verify the Gajim device in Monal too. In Monal, turn off **Allow unverified
   calls in encrypted chats**.
4. Click the phone icon in the chat header, or right-click the chat in the chat
   list and choose **Start Secure Audio Call…**. Alternatively,
   place an audio call from Monal and answer the incoming Gajim window.
5. Confirm the window says **Connected — encrypted and verified with OMEMO**,
   and test audio in both directions, mute, hangup, decline, and cancellation.

The verification prompt also offers **Trust keys used in the last 24 hours**.
This explicitly approves this contact's keys with an OMEMO last-seen timestamp
within that window and retries the call. It does not compare fingerprints;
manual comparison remains the stronger way to establish the contact's identity.
Older keys and keys that have never been seen are not approved by this shortcut.

Capture now runs inside the media pipeline with a shared clock, preserving source
timestamps rather than retimestamping audio as Python callbacks arrive. Voice
processing uses gentle noise suppression without automatic digital gain control;
Opus uses 20 ms frames, 48 kbit/s, and in-band forward error correction. Real
speech quality still needs listening tests on both endpoints.

The microphone is acquired only after the actual remote DTLS certificate matches
the fingerprint authenticated through the verified OMEMO device. A missing,
plaintext, undecryptable, or mismatched fingerprint aborts the call. Revoking
device trust ends an active call (checked every second). Calls from non-contacts
are not accepted. Accepting an incoming call authorizes audio only.

Calls use TURN relays by default, hiding local network addresses from the other
caller. If the account server does not provide a usable TURN service, the call
fails without falling back to a direct connection. The call window offers
**Do not relay calls**, off by default, to allow direct connections for future
calls. Direct connections expose your IP address to the other person.

OMEMO verification remains required in either mode. A relay forwards encrypted
media; it does not authenticate the caller or hide signaling metadata from XMPP
servers. The TURN operator can see connection addresses and traffic timing.

## Dependencies and server

GStreamer 1.28.7 or newer, the GstWebRTC/GstSdp introspection bindings, libnice,
DTLS/SRTP, Opus, WebRTC DSP/echo probe, and PulseAudio source/sink plugins are
required. PipeWire's PulseAudio compatibility service works with these elements.
Availability checks hide advertised calling capabilities when dependencies are
missing. No additional Python dependency is required by the current checkout.

STUN/TURN services and temporary TURN credentials are discovered through
XEP-0215. No public relay or fallback credentials are hardcoded into Gajim.

## Verification

Run protocol, real OMEMO/Signal, and signaling safety tests:

```sh
.venv/bin/python -m unittest test.common.test_secure_calls -v
```

Include local WebRTC audio and complete simulated XMPP call tests:

```sh
GAJIM_TEST_CALL_MEDIA=1 .venv/bin/python -m unittest test.common.test_secure_calls -v
```

The latter needs local UDP sockets, so a network sandbox may block it. These
tests use generated audio, fake playback sinks, temporary OMEMO databases, and
simulated XMPP accounts. They do not access the microphone or contact anyone.
They check media certificate substitution before capture, actual encrypted audio
exchange, complete call setup/hangup, plaintext-fingerprint rejection, replay,
tampered envelopes, device pinning, trust revocation, SDP injection, unsolicited
Jingle, TURN credential discovery, missing-relay failure, and filtering of local
and related addresses from relayed call signaling.

Real Monal interoperability and calls across the user's networks still require
manual testing; passing loopback tests is not a security audit. Distribution and
Flatpak/Windows packaging have not been updated.

## Implementation references

- [Monal-compatible OMEMO call verification protocol](https://gist.github.com/iNPUTmice/aa4fc0aeea6ce5fb0e0fe04baca842cd)
- [GStreamer WebRTC](https://gstreamer.freedesktop.org/documentation/webrtc/index.html)
- [XEP-0353: Jingle Message Initiation](https://xmpp.org/extensions/xep-0353.html)
- [XEP-0320: DTLS-SRTP in Jingle](https://xmpp.org/extensions/xep-0320.html)
- [XEP-0215: External Service Discovery](https://xmpp.org/extensions/xep-0215.html)

The media backend is new; old unmerged Gajim calling patches were not applied.
The small `CallSecurity` adapter uses omemo-dr 1.x's private device-targeted
Signal method because its public payload API encrypts to multiple devices.
Real roundtrip tests cover that dependency boundary. Keep that test when changing
omemo-dr versions.
