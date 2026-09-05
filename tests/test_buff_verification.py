import pytest


def test_buff_page_expired_is_verification_required():
    from buff.buyer import _is_verification_required

    assert _is_verification_required({"code": "FAIL", "msg": "页面已过期，请刷新当前页面"})


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_verification_response_opens_circuit_without_retry(monkeypatch, method):
    from buff.buyer import (
        BuffBuyer,
        BuffRequestPolicy,
        BuffVerificationRequired,
        BuffWriteResultUnknown,
    )

    calls = []

    class FakeResponse:
        status_code = 200
        text = '{"code":"FAIL","msg":"页面已过期，请刷新当前页面"}'

        def json(self):
            return {"code": "FAIL", "msg": "页面已过期，请刷新当前页面"}

    def request(self, *args, **kwargs):
        calls.append(args)
        return FakeResponse()

    monkeypatch.setattr("requests.Session.request", request)
    buyer = BuffBuyer(
        "csrf_token=abc",
        request_policy=BuffRequestPolicy(min_interval=0, state_path=None, persist=False),
    )
    expected_error = BuffWriteResultUnknown if method == "POST" else BuffVerificationRequired
    try:
        with pytest.raises(expected_error):
            buyer._make_request(method, "https://buff.163.com/api/fake")
        with pytest.raises(BuffVerificationRequired):
            buyer._make_request(method, "https://buff.163.com/api/fake")
        assert len(calls) == 1
    finally:
        buyer.close()
