import base64
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.licensing import features as F
from app.licensing import gates as G
from app.licensing.fingerprint import machine_id
from app.licensing.manager import LicenseManager
from app.licensing.verify import canonical, encode_activation


def _keypair(monkeypatch) -> Ed25519PrivateKey:
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pub_b64 = base64.urlsafe_b64encode(pub).decode().rstrip("=")
    monkeypatch.setattr("app.licensing.verify._public_key_b64", lambda: pub_b64)
    return priv


def _sign(priv, **payload) -> str:
    base = {"key": "MA-TEST", "edition": "pro",
            "features": list(F.PRO_FEATURES), "machine_id": None,
            "issued_at": "2026-01-01T00:00:00Z", "expires_at": None}
    base.update(payload)
    sig = priv.sign(canonical(base))
    return encode_activation(base, base64.b64encode(sig).decode())


def test_activate_unlocks_features(tmp_path, monkeypatch):
    priv = _keypair(monkeypatch)
    mgr = LicenseManager(tmp_path)
    assert not mgr.active and not mgr.has_feature(F.VIDEO)

    ok, _ = mgr.activate(_sign(priv))
    assert ok and mgr.active
    assert mgr.has_feature(F.VIDEO) and mgr.has_feature(F.PLATFORM_SYNC)


def test_bad_signature_rejected(tmp_path, monkeypatch):
    _keypair(monkeypatch)                       # app trusts key A
    other = Ed25519PrivateKey.generate()        # attacker signs with key B
    mgr = LicenseManager(tmp_path)
    ok, msg = mgr.activate(_sign(other))
    assert not ok and "签名" in msg
    assert not mgr.active


def test_machine_binding(tmp_path, monkeypatch):
    priv = _keypair(monkeypatch)
    mgr = LicenseManager(tmp_path)
    # bound to a different machine -> rejected
    ok, msg = mgr.activate(_sign(priv, machine_id="deadbeef" * 4))
    assert not ok and "其他机器" in msg
    # bound to THIS machine -> ok
    ok2, _ = mgr.activate(_sign(priv, machine_id=machine_id()))
    assert ok2 and mgr.active


def test_expired_rejected(tmp_path, monkeypatch):
    priv = _keypair(monkeypatch)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    mgr = LicenseManager(tmp_path)
    ok, msg = mgr.activate(_sign(priv, expires_at=past))
    assert not ok and "过期" in msg


def test_persists_across_reload_same_machine(tmp_path, monkeypatch):
    priv = _keypair(monkeypatch)
    LicenseManager(tmp_path).activate(_sign(priv))
    # fresh manager reads the encrypted license.bin back
    mgr2 = LicenseManager(tmp_path)
    assert mgr2.active and mgr2.has_feature(F.SCHEDULE)


def test_dev_bypass(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_AGENT_LICENSE_DEV", "1")
    mgr = LicenseManager(tmp_path)
    assert mgr.active and mgr.has_feature(F.VIDEO)
    assert mgr.status_dict()["dev"] is True


class _FakeStore:
    def __init__(self):
        self.d = {}

    def get_setting(self, k, default=None):
        return self.d.get(k, default)

    def set_setting(self, k, v):
        self.d[k] = v


def test_free_rewrite_daily_quota(tmp_path, monkeypatch):
    mgr = LicenseManager(tmp_path)               # free tier
    store = _FakeStore()
    assert G.rewrite_remaining(mgr, store) == F.FREE_REWRITE_PER_DAY
    assert G.rewrite_allowed(mgr, store)
    G.consume_rewrite(mgr, store)
    assert G.rewrite_remaining(mgr, store) == 0
    assert not G.rewrite_allowed(mgr, store)     # quota exhausted


def test_integrity_rejects_swapped_public_key(tmp_path, monkeypatch):
    priv = _keypair(monkeypatch)
    # pin integrity to a hash that won't match the (test) public key
    monkeypatch.setattr("app.licensing.integrity.EXPECTED_PUBKEY_SHA256",
                        "deadbeef")
    mgr = LicenseManager(tmp_path)
    ok, msg = mgr.activate(_sign(priv))
    assert not ok and "完整性" in msg
    assert not mgr.active


def test_pro_rewrite_unlimited(tmp_path, monkeypatch):
    priv = _keypair(monkeypatch)
    mgr = LicenseManager(tmp_path)
    mgr.activate(_sign(priv))
    store = _FakeStore()
    assert G.rewrite_remaining(mgr, store) is None   # unlimited
    G.consume_rewrite(mgr, store)                    # no-op for Pro
    assert G.rewrite_allowed(mgr, store)
