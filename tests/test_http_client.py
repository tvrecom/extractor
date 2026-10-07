import logging

import pytest
import requests

from tmdb_extractor.errors import TMDBRequestError
from tmdb_extractor.http_client import (
    RateGate, RetryPolicy, TMDBClient, parse_retry_after, redact,
)
from tmdb_extractor.logging_setup import RedactingFilter
from tests.support import API_KEY


class Resp:
    def __init__(self, status, body=None, headers=None, text=""):
        self.status_code, self._body = status, body
        self.headers, self.text = headers or {}, text

    def json(self):
        return self._body


class Session:
    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(script, attempts=4, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    session = Session(script)
    now = [0.0]

    def sleeper(seconds):  # virtual time: sleeping advances the clock
        sleeps.append(seconds)
        now[0] += seconds

    gate = RateGate(clock=lambda: now[0], sleeper=sleeper)
    c = TMDBClient(API_KEY, "https://x/3", 5, RetryPolicy(attempts, 1.0, jitter=0.0),
                   gate=gate, session_factory=lambda: session, sleeper=sleeper)
    return c, session, sleeps


def test_429_then_success_backs_off_and_succeeds():
    c, s, sleeps = client([Resp(429, headers={"Retry-After": "3"}), Resp(200, {"ok": 1})])
    assert c.get_json("movie/1") == {"ok": 1}
    assert s.calls == 2 and sleeps == [3.0]


def test_repeated_500_exhausts_budget_without_trailing_sleep():
    c, s, sleeps = client([Resp(500)] * 3, attempts=3)
    with pytest.raises(TMDBRequestError) as err:
        c.get_json("movie/1")
    assert err.value.attempts == 3 and err.value.status_code == 500
    assert sleeps == [1.0, 2.0]  # attempts-1 sleeps; none after the final failure


def test_404_not_retried():
    c, s, _ = client([Resp(404, text="nope")])
    with pytest.raises(TMDBRequestError) as err:
        c.get_json("movie/1")
    assert err.value.status_code == 404 and s.calls == 1


def test_429_cooldown_is_shared_across_calls():
    clock = [0.0]
    sleeps = []
    gate = RateGate(clock=lambda: clock[0], sleeper=lambda s: (sleeps.append(s), clock.__setitem__(0, clock[0] + s)))
    gate.penalize(5)
    gate.wait()
    assert sleeps == [5] and clock[0] == 5


def test_retry_after_parsing():
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after("-4") is None
    assert parse_retry_after("nan") is None
    assert parse_retry_after("garbage") is None
    assert RetryPolicy(cap_seconds=60).delay(1, "9999") == 60


def test_api_key_never_in_error_text_or_cause():
    boom = requests.ConnectionError(f"HTTPSConnectionPool: /3/movie/1?api_key={API_KEY}&language=en")
    c, _, _ = client([boom, boom], attempts=2)
    with pytest.raises(TMDBRequestError) as err:
        c.get_json("movie/1")
    assert API_KEY not in str(err.value)
    assert err.value.__cause__ is None and err.value.__suppress_context__


def test_redacting_filter_scrubs_messages_and_tracebacks(caplog):
    logger = logging.getLogger("redact-test")
    handler = logging.Handler()
    records = []
    handler.emit = records.append
    handler.addFilter(RedactingFilter(API_KEY))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        raise RuntimeError(f"url ?api_key={API_KEY}")
    except RuntimeError:
        logger.error("failed %s", f"key={API_KEY}", exc_info=True)
    text = records[0].getMessage() + (records[0].exc_text or "")
    assert API_KEY not in text


def test_redact_helper():
    assert redact("a?api_key=abc&b=1") == "a?api_key=***&b=1"
