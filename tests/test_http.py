def test_retry_after_parsing():
    from jobservice.http import retry_after

    assert retry_after("12", 5) == 12
    assert retry_after("100000", 5) == 300
    assert retry_after("Wed, 21 Oct 2026 07:28:00 GMT", 5) == 5
    assert retry_after(None, 1000) == 300


def test_429_waits_for_retry_after(monkeypatch):
    import httpx

    from jobservice import http as http_mod

    slept = []
    monkeypatch.setattr(http_mod.time, "sleep", slept.append)
    calls = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)])
    client = http_mod.HttpClient(delay=0, retries=3,
                                 transport=httpx.MockTransport(lambda r: next(calls)))  # fmt: skip
    assert client.get("https://example.com/").status_code == 200
    assert 7 in slept
