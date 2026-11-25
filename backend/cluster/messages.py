"""Authenticated inter-node datagrams.

Wire format: ``b"PHOS" | version(1) | hmac_sha256(16 bytes truncated) | zlib(json)``.
The HMAC key is the cluster key from secrets.yaml (distinct from the Wi-Fi
PSK). Replay protection: each sender includes a random ``boot`` id and a
strictly increasing ``seq``; receivers drop non-increasing sequences per boot
and anything older than ``max_age_s`` of *receiver-local* arrival bookkeeping.
Timestamps inside messages are always in the sender's own clock domain and are
never interpreted on the receiver except via the explicit clock-sync protocol.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import zlib
from dataclasses import dataclass, field

MAGIC = b"PHOS"
VERSION = 1
TAG_LEN = 16
MAX_DATAGRAM = 60000


class CodecError(Exception):
    pass


@dataclass
class Codec:
    key: bytes
    node_id: str
    boot: str = field(default_factory=lambda: os.urandom(6).hex())
    _seq: int = 0
    _last_seen: dict[tuple[str, str], int] = field(default_factory=dict)
    _window: dict[tuple[str, str], set] = field(default_factory=dict)
    WINDOW = 256   # sliding replay window: reordered datagrams are fine, duplicates/old ones are not

    def encode(self, msg: dict) -> bytes:
        self._seq += 1
        body = dict(msg)
        body["_from"] = self.node_id
        body["_boot"] = self.boot
        body["_seq"] = self._seq
        payload = zlib.compress(json.dumps(body, separators=(",", ":")).encode(), 6)
        tag = hmac.new(self.key, payload, hashlib.sha256).digest()[:TAG_LEN]
        data = MAGIC + bytes([VERSION]) + tag + payload
        if len(data) > MAX_DATAGRAM:
            raise CodecError(f"datagram too large ({len(data)} bytes)")
        return data

    def decode(self, data: bytes) -> dict:
        if len(data) < len(MAGIC) + 1 + TAG_LEN or not data.startswith(MAGIC):
            raise CodecError("bad magic")
        if data[4] != VERSION:
            raise CodecError("bad version")
        tag, payload = data[5:5 + TAG_LEN], data[5 + TAG_LEN:]
        expect = hmac.new(self.key, payload, hashlib.sha256).digest()[:TAG_LEN]
        if not hmac.compare_digest(tag, expect):
            raise CodecError("bad hmac")
        try:
            body = json.loads(zlib.decompress(payload, bufsize=1 << 16))
        except (zlib.error, ValueError) as exc:
            raise CodecError("bad payload") from exc
        if not isinstance(body, dict) or "t" not in body or "_from" not in body:
            raise CodecError("bad message")
        sender, boot, seq = body["_from"], str(body.get("_boot")), int(body.get("_seq", 0))
        if sender == self.node_id and boot == self.boot:
            raise CodecError("own message")
        key = (sender, boot)
        top = self._last_seen.get(key, 0)
        seen = self._window.setdefault(key, set())
        if seq <= top - self.WINDOW or seq in seen:
            raise CodecError("replay")
        seen.add(seq)
        if seq > top:
            self._last_seen[key] = seq
            if len(seen) > 2 * self.WINDOW:
                floor = seq - self.WINDOW
                self._window[key] = {s for s in seen if s > floor}
        if len(self._last_seen) > 256:  # forget stale boots
            for k in list(self._last_seen)[:128]:
                del self._last_seen[k]
                self._window.pop(k, None)
        return body


def derive_key(cluster_key: str) -> bytes:
    if not cluster_key:
        # Development default only; installer always generates a random key.
        cluster_key = "phosphor2000-insecure-dev-key"
    return hashlib.sha256(b"phos-udp|" + cluster_key.encode()).digest()


# ---------------------------------------------------------------------------
# Small secrets (e.g. the venue Wi-Fi password) travelling inside cluster state:
# encrypt-then-MAC with keys derived from the cluster key. Keystream = SHA-256 in counter
# mode over (key, nonce, counter) — a PRF stream cipher; integrity by HMAC-SHA256.
# ---------------------------------------------------------------------------
import base64 as _b64


def _keys(cluster_key: str) -> tuple[bytes, bytes]:
    base = hashlib.sha256(b"phos-seal|" + cluster_key.encode()).digest()
    return hashlib.sha256(base + b"|enc").digest(), hashlib.sha256(base + b"|mac").digest()


def _stream(k: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    ctr = 0
    while len(out) < n:
        out += hashlib.sha256(k + nonce + ctr.to_bytes(8, "big")).digest()
        ctr += 1
    return bytes(out[:n])


def seal(cluster_key: str, plaintext: str) -> str:
    ek, mk = _keys(cluster_key)
    nonce = os.urandom(16)
    data = plaintext.encode()
    ct = bytes(a ^ b for a, b in zip(data, _stream(ek, nonce, len(data))))
    tag = hmac.new(mk, nonce + ct, hashlib.sha256).digest()
    return _b64.urlsafe_b64encode(nonce + tag + ct).decode()


def unseal(cluster_key: str, token: str) -> str:
    ek, mk = _keys(cluster_key)
    raw = _b64.urlsafe_b64decode(token.encode())
    nonce, tag, ct = raw[:16], raw[16:48], raw[48:]
    if not hmac.compare_digest(tag, hmac.new(mk, nonce + ct, hashlib.sha256).digest()):
        raise CodecError("sealed value: bad MAC")
    return bytes(a ^ b for a, b in zip(ct, _stream(ek, nonce, len(ct)))).decode()
