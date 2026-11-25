import pytest

from backend.cluster.messages import Codec, CodecError, derive_key


def test_hmac_reorder_and_replay():
    key = derive_key("k")
    a, b = Codec(key, "pi-01"), Codec(key, "pi-02")
    m = [a.encode({"t": "x", "i": i}) for i in range(5)]
    assert b.decode(m[3])["i"] == 3
    assert b.decode(m[1])["i"] == 1          # reordered: accepted
    with pytest.raises(CodecError):
        b.decode(m[1])                       # duplicate: rejected
    with pytest.raises(CodecError):
        b.decode(m[4][:-1] + bytes([m[4][-1] ^ 1]))   # tampered
    assert b.decode(m[4])["i"] == 4
    old = m[0]
    for i in range(300):
        b.decode(a.encode({"t": "x"}))
    with pytest.raises(CodecError):
        b.decode(old)                        # older than the window
    with pytest.raises(CodecError):
        Codec(derive_key("other"), "pi-03").decode(a.encode({"t": "x"}))
