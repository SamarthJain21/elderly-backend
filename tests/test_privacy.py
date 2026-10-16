import logging
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import ratelimit
from app.main import app
from app.redact import mask_phone
from app.sms import send_sms
from tests.test_chat import FakeModel, _connect, model, store  # noqa: F401  (fixtures)


def test_mask_phone_keeps_country_code_and_last_three_digits() -> None:
    assert mask_phone("+91 98765 43210") == "+91•••••210"
    assert mask_phone("0812345678") == "•••••678"
    assert mask_phone("123") == "•••"
    assert mask_phone(None) == "(none)"


def test_unsent_sms_log_has_no_body_and_no_full_number(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.sms"):
        assert send_sms("+66812345678", "Rose fell at 12 Sukhumvit Rd") == "log"
    text = caplog.text
    assert "Sukhumvit" not in text and "Rose" not in text and "812345678" not in text
    assert "+66•••••678" in text


def test_limiter_allows_up_to_limit_then_blocks_until_window_passes() -> None:
    now = [0.0]
    lim = ratelimit.RateLimiter(2, 60, clock=lambda: now[0])
    assert lim.allow("u") and lim.allow("u") and not lim.allow("u")
    assert lim.allow("other")            # per key
    now[0] = 60.0
    assert lim.allow("u")                # window slid


def test_limiter_check_raises_429() -> None:
    lim = ratelimit.RateLimiter(1, 60)
    lim.check("u")
    with pytest.raises(HTTPException) as e:
        lim.check("u")
    assert e.value.status_code == 429


def test_chat_socket_tells_the_user_to_slow_down_but_stays_open(
    monkeypatch: pytest.MonkeyPatch, store: Any, model: FakeModel,  # noqa: F811
) -> None:
    monkeypatch.setattr(ratelimit, "chat_frames", ratelimit.RateLimiter(1, 60))
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        _connect(ws)
        ws.send_json({"type": "noop"})           # uses the one allowed frame, no reply
        ws.send_json({"type": "noop"})
        assert ws.receive_json() == {"type": "error", "message": "You're sending messages very quickly — please wait a moment."}
