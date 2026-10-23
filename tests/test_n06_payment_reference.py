"""N06: payment verify rejects malformed references before any provider call,
URL-encodes valid ones, and never echoes provider fields."""
import pytest

from conftest import register
import app.routers.payments as payments


class _Resp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


@pytest.mark.parametrize("ref", ["../../balance", "SFP_../../balance", "SFP_ab", "abc", "SFP_" + "a" * 40,
                                 "SFP_abc def", "SFP_abcdef/../x", "SFP_abcdef?x=1"])
@pytest.mark.parametrize("provider", ["paystack", "flutterwave"])
def test_bad_reference_400_and_no_outbound_call(monkeypatch, ref, provider):
    calls = []
    monkeypatch.setattr(payments.requests, "get", lambda *a, **k: calls.append(a) or _Resp({}))
    u = register()
    r = u.post("/payments/verify", {"reference": ref, "provider": provider})
    assert r.status_code == 400, r.text
    assert calls == []


def test_valid_reference_is_encoded_and_provider_status_not_echoed(monkeypatch):
    seen = []

    def fake_get(url, **k):
        seen.append(url)
        return _Resp({"data": {"status": "abandoned<script>secret-field"}})
    monkeypatch.setattr(payments.requests, "get", fake_get)
    u = register()
    r = u.post("/payments/verify", {"reference": "SFP_Ab-12_cd", "provider": "paystack"})
    assert r.status_code == 200
    assert seen == ["https://api.paystack.co/transaction/verify/SFP_Ab-12_cd"]
    assert "secret-field" not in r.text and r.json()["detail"] == "Payment not successful"
