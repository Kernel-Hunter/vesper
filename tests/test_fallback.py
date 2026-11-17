"""Automatic switching: a model that runs out of credits hands the turn to the next one."""

import time

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from vesper import fallback
from vesper import providers as prov
from vesper.coder import Session
from vesper.store import Store
from vesper.workspace import Workspace


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status_code = status


class ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class OutOfCredits(ToolFake):
    def _generate(self, *args, **kwargs):
        raise ApiError(402, "Insufficient credits. Add more using https://openrouter.ai/credits")

    def _stream(self, *args, **kwargs):
        raise ApiError(402, "Insufficient credits. Add more using https://openrouter.ai/credits")


def test_classify_knows_credit_rate_key_and_missing_model_errors():
    assert fallback.classify(ApiError(402, "Insufficient credits")) == "credits"
    assert fallback.classify(ApiError(429, "You exceeded your current quota (insufficient_quota)")) == "credits"
    assert fallback.classify(ApiError(400, "Your credit balance is too low to access the Anthropic API")) == "credits"
    assert fallback.classify(ApiError(403, "Budget limit exceeded")) == "credits"
    assert fallback.classify(Exception("Make a single top-up of $5 or more (billing_error)")) == "credits"
    assert fallback.classify(ApiError(429, "Rate limit reached for model llama-3.3-70b (rate_limit_exceeded)")) == "rate"
    groq = ("Rate limit reached for model `qwen/qwen3.8-27b` on tokens per minute. Please try again in 7.5s. "
            "Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing")
    assert fallback.classify(ApiError(429, groq)) == "rate"  # a few seconds, not hours
    assert fallback.classify(ApiError(429, "RESOURCE_EXHAUSTED")) == "rate"
    assert fallback.classify(ApiError(401, "Incorrect API key provided")) == "auth"
    assert fallback.classify(ApiError(404, "The model `x` does not exist")) == "gone"
    assert fallback.classify(ApiError(400, "maximum context length is 8192 tokens")) is None
    assert fallback.classify(ApiError(400, "messages: field required")) is None
    assert fallback.classify(ConnectionError("network down")) is None


def test_switcher_skips_failed_models_until_their_wait_ends(monkeypatch):
    chain = [{"provider": "a", "model": "1"}, {"provider": "b", "model": "2"}, {"provider": "c", "model": "3"}]
    sw = fallback.Switcher(lambda: chain)
    assert sw.preferred() is None  # nothing failed: stay on the current model
    sw.mark("a", "1", "credits")
    assert sw.next_after("a", "1")["model"] == "2"
    sw.mark("b", "2", "rate")
    assert sw.next_after("b", "2")["model"] == "3"
    assert sw.preferred()["model"] == "3"
    now = time.time()
    monkeypatch.setattr(fallback.time, "time", lambda: now + fallback.RATE_COOLDOWN_S + 1)
    assert sw.preferred()["model"] == "2"  # the rate limit is over, the credits are not
    monkeypatch.setattr(fallback.time, "time", lambda: now + fallback.CREDIT_COOLDOWN_S + 1)
    assert sw.preferred()["model"] == "1"


def test_ranking_puts_stronger_families_first():
    ids = ["meta-llama/llama-3.3-70b-instruct:free", "moonshotai/kimi-k2:free", "openai/gpt-oss-120b:free",
           "google/gemma-3-27b-it:free", "gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.5-flash-lite"]
    ranked = sorted(ids, key=fallback.rank)
    assert ranked == ["gemini-4-hydrogen", "gemini-3.8-flash", "moonshotai/kimi-k2:free", "openai/gpt-oss-120b:free",
                      "gemini-3.5-flash-lite", "meta-llama/llama-3.3-70b-instruct:free", "google/gemma-3-27b-it:free"]


def test_a_turn_switches_models_and_keeps_going(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    store = Store(tmp_path / "db" / "v.db")
    s = Session(OutOfCredits(messages=iter([])), Workspace(proj, mode="yolo"), store, provider="openrouter", model="paid-model")
    backup = ToolFake(disable_streaming=True, messages=iter([AIMessage("Finished on the free model.")]))
    monkeypatch.setattr(prov, "get_llm", lambda provider=None, model=None, **kw: backup)
    s.switcher = fallback.Switcher([{"provider": "openrouter", "model": "paid-model", "label": "OpenRouter"},
                                    {"provider": "groq", "model": "free-model", "label": "Groq"}])
    events = list(s.ask("fix the bug"))
    texts = [d.get("text", "") for k, d in events if k == "notice"]
    assert any("switched to Groq free-model" in t for t in texts)
    assert ("model", {"provider": "groq", "model": "free-model"}) in events
    assert any(k == "done" for k, _ in events) and not any(k == "error" for k, _ in events)
    assert (s.provider, s.model) == ("groq", "free-model")
    assert store.get_session(s.id)["model"] == "free-model"
    assert "Finished on the free model." in [m.content for m in store.load_messages(s.id)][-1]
    store.close()


def test_without_switching_the_error_still_stops_the_turn(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    store = Store(tmp_path / "db" / "v.db")
    s = Session(OutOfCredits(messages=iter([])), Workspace(proj, mode="yolo"), store, provider="openrouter", model="m")
    try:
        list(s.ask("fix it"))
        raised = False
    except ApiError:
        raised = True
    assert raised
    store.close()


def test_fallback_api_saves_the_setting_and_a_custom_order(client):
    r = client.get("/api/fallback")
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["chain"] is None
    assert client.post("/api/fallback", json={"enabled": True}).json()["enabled"] is True
    order = [{"provider": "groq", "model": "llama-3.3-70b-versatile"}, {"provider": "ollama", "model": "llama3.2"}]
    assert client.post("/api/fallback", json={"chain": order}).json()["chain"] == order
    assert client.post("/api/fallback", json={"chain": [{"provider": "nope", "model": "x"}]}).status_code == 400
    assert client.post("/api/fallback", json={"chain": None}).json()["chain"] is None
    assert client.get("/api/fallback").json()["enabled"] is True
