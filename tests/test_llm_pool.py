"""Failover behaviour of the Gemini pool, driven by fake clients."""

from __future__ import annotations

import pytest

from app.core.llm_pool import FailureKind, GeminiPool, PoolExhausted, classify


class FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeModels:
    def __init__(self, behaviour) -> None:
        self._behaviour = behaviour

    async def generate_content(self, *, model, contents, config):
        return self._behaviour(model)


class FakeAio:
    def __init__(self, behaviour) -> None:
        self.models = FakeModels(behaviour)


class FakeClient:
    def __init__(self, behaviour) -> None:
        self.aio = FakeAio(behaviour)


def _pool(keys: list[str], models: list[str], behaviours: dict[int, object]) -> GeminiPool:
    pool = GeminiPool(keys, models, quota_cooldown_s=30.0, max_retries_per_attempt=1)
    pool._clients = {i: FakeClient(b) for i, b in behaviours.items()}  # noqa: SLF001
    return pool


class Quota(Exception):
    code = 429
    def __str__(self) -> str: return "RESOURCE_EXHAUSTED: quota exceeded"


class Auth(Exception):
    code = 403
    def __str__(self) -> str: return "API_KEY_INVALID"


class Unavailable(Exception):
    code = 503
    def __str__(self) -> str: return "model is overloaded"


class NotFound(Exception):
    code = 404
    def __str__(self) -> str: return "model not found"


# --- classification -------------------------------------------------------------

@pytest.mark.parametrize(
    "exc,kind",
    [
        (Quota(), FailureKind.QUOTA),
        (Auth(), FailureKind.AUTH),
        (Unavailable(), FailureKind.TRANSIENT),
        (NotFound(), FailureKind.MODEL),
        (ValueError("something odd"), FailureKind.FATAL),
    ],
)
def test_error_classification(exc, kind) -> None:
    assert classify(exc) is kind


# --- failover -------------------------------------------------------------------

async def test_second_key_used_when_first_is_out_of_quota() -> None:
    def dead(model): raise Quota()
    def alive(model): return FakeResp('{"ok":true}')

    pool = _pool(["k1", "k2"], ["m1"], {0: dead, 1: alive})
    text, meta = await pool.generate(contents="x", config=None)

    assert text == '{"ok":true}'
    assert meta["key"].startswith("key1"), "the healthy second key should have served it"
    assert pool.health()["available_keys"] >= 1


async def test_quota_exhausted_key_is_cooled_down_not_disabled() -> None:
    def dead(model): raise Quota()
    def alive(model): return FakeResp("{}")

    pool = _pool(["k1", "k2"], ["m1"], {0: dead, 1: alive})
    await pool.generate(contents="x", config=None)

    cooled = [s for s in pool.health()["keys"] if s["cooling_down"]]
    assert cooled, "an out-of-quota key should be cooling down"
    assert not any(s["disabled"] for s in cooled), "quota is temporary, not fatal"


async def test_invalid_key_is_disabled_permanently() -> None:
    def bad(model): raise Auth()
    def alive(model): return FakeResp("{}")

    pool = _pool(["k1", "k2"], ["m1"], {0: bad, 1: alive})
    await pool.generate(contents="x", config=None)

    assert any(s["disabled"] for s in pool.health()["keys"])


async def test_falls_through_to_next_model() -> None:
    seen: list[str] = []

    def behaviour(model):
        seen.append(model)
        if model == "m1":
            raise NotFound()
        return FakeResp('{"model":"m2"}')

    pool = _pool(["k1"], ["m1", "m2"], {0: behaviour})
    text, meta = await pool.generate(contents="x", config=None)

    assert meta["model"] == "m2"
    assert "m1" in seen and "m2" in seen


async def test_all_keys_on_a_model_tried_before_next_model() -> None:
    """A healthy key must not be demoted to a weaker model because another key died."""
    seen: list[tuple[str, str]] = []

    def make(name):
        def behaviour(model):
            seen.append((name, model))
            raise Quota()
        return behaviour

    pool = _pool(["k1", "k2"], ["m1", "m2"], {0: make("k1"), 1: make("k2")})
    with pytest.raises(PoolExhausted):
        await pool.generate(contents="x", config=None)

    models_in_order = [m for _, m in seen]
    assert models_in_order == ["m1", "m1", "m2", "m2"], seen


async def test_pool_exhausted_when_everything_fails() -> None:
    def dead(model): raise Quota()

    pool = _pool(["k1", "k2"], ["m1"], {0: dead, 1: dead})
    with pytest.raises(PoolExhausted) as exc:
        await pool.generate(contents="x", config=None)
    assert exc.value.attempts == 2


async def test_transient_failure_is_retried_on_same_key() -> None:
    calls = {"n": 0}

    def flaky(model):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Unavailable()
        return FakeResp("{}")

    pool = GeminiPool(["k1"], ["m1"], max_retries_per_attempt=3)
    pool._clients = {0: FakeClient(flaky)}  # noqa: SLF001
    text, _ = await pool.generate(contents="x", config=None)

    assert text == "{}"
    assert calls["n"] == 2, "transient error should retry the same key"


def test_pool_requires_a_key() -> None:
    with pytest.raises(ValueError):
        GeminiPool([], ["m1"])


def test_load_spreads_across_keys() -> None:
    """Round-robin: consecutive plans should not always start on key 0."""
    pool = GeminiPool(["k1", "k2", "k3"], ["m1"])
    firsts = {pool._plan()[0].key.index for _ in range(6)}  # noqa: SLF001
    assert len(firsts) > 1, "all requests started on the same key"
