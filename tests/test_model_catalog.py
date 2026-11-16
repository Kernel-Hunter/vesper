"""Model lists: every model a key can use, with prices, free models and tool support (offline)."""

import io
import json
import urllib.error

import pytest

from vesper import onboarding
from vesper import providers as prov


@pytest.fixture(autouse=True)
def fresh_cache(tmp_path, monkeypatch):
    """Each test gets its own cache folder: saved lists and the day-long price list."""
    monkeypatch.setattr(prov, "_cache_dir", lambda: tmp_path / "cache")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def serve(monkeypatch, routes: dict):
    """routes: URL prefix -> JSON body, or an exception to raise. Records the URLs asked for."""
    asked = []

    def urlopen(req, timeout):
        url = req.full_url if hasattr(req, "full_url") else req
        asked.append(url)
        for prefix, body in routes.items():
            if url.startswith(prefix):
                if isinstance(body, Exception):
                    raise body
                return _Resp(json.dumps(body).encode())
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(prov.urllib.request, "urlopen", urlopen)
    return asked


OPENROUTER = {"data": [
    {"id": "meta-llama/llama-3.3-70b-instruct:free", "name": "Llama 3.3 70B (free)", "context_length": 131072,
     "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools", "temperature"],
     "architecture": {"output_modalities": ["text"]}},
    {"id": "openai/gpt-4.1", "name": "OpenAI: GPT-4.1", "context_length": 1047576,
     "pricing": {"prompt": "0.000002", "completion": "0.000008"}, "supported_parameters": ["tools"],
     "architecture": {"output_modalities": ["text"]}},
    {"id": "some/story-model", "name": "No tools", "context_length": 8192,
     "pricing": {"prompt": "0.0000001", "completion": "0.0000001"}, "supported_parameters": ["temperature"]},
    {"id": "google/image-maker", "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"output_modalities": ["image"]}},
    {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}, "supported_parameters": ["tools"]},
]}


def test_openrouter_lists_every_model_with_price_free_and_tools(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    serve(monkeypatch, {"https://openrouter.ai/api/v1/models": OPENROUTER})
    cat = prov.model_catalog("openrouter")
    assert cat["live"] and cat["error"] is None
    by_id = {m["id"]: m for m in cat["models"]}
    assert "google/image-maker" not in by_id  # makes images, cannot chat
    assert len(by_id) == 4
    free = by_id["meta-llama/llama-3.3-70b-instruct:free"]
    assert free["free"] is True and free["input"] == 0 and free["tools"] is True and free["context"] == 131072
    gpt = by_id["openai/gpt-4.1"]
    assert (gpt["input"], gpt["output"], gpt["free"]) == (2.0, 8.0, False)
    assert by_id["some/story-model"]["tools"] is False
    assert by_id["openrouter/auto"]["input"] is None and by_id["openrouter/auto"]["free"] is None  # price varies
    assert cat["models"][0]["id"] == "openrouter/auto"  # the suggested models come first


def test_no_model_list_is_cut_short(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    many = {"data": [{"id": f"v/m{i}", "pricing": {"prompt": "0.000001", "completion": "0.000001"}} for i in range(450)]}
    serve(monkeypatch, {"https://openrouter.ai/api/v1/models": many})
    assert len(prov.list_models("openrouter")) == 450


def test_openai_gets_list_prices_by_exact_id_only(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    served = {"data": [{"id": "gpt-4.1"}, {"id": "gpt-4.1-2025-04-14"}, {"id": "my-fine-tune"}, {"id": "text-embedding-3-small"}]}
    public = {"data": [
        {"id": "openai/gpt-4.1", "pricing": {"prompt": "0.000002", "completion": "0.000008"}, "context_length": 1047576},
        {"id": "openai/gpt-4.1:batch", "pricing": {"prompt": "0.000001", "completion": "0.000004"}},
    ]}
    # The public OpenRouter list is asked without a key; the provider's own list with one.
    serve(monkeypatch, {"https://api.openai.com/v1/models": served, "https://openrouter.ai/api/v1/models": public})
    by_id = {m["id"]: m for m in prov.model_catalog("openai")["models"]}
    assert set(by_id) == {"gpt-4.1", "gpt-4.1-2025-04-14", "my-fine-tune"}
    assert by_id["gpt-4.1"]["input"] == 2.0 and by_id["gpt-4.1"]["price_source"] == "openrouter"
    assert by_id["gpt-4.1-2025-04-14"]["input"] == 2.0  # a dated snapshot has the same price
    assert by_id["gpt-4.1"]["context"] == 1047576
    assert by_id["my-fine-tune"]["input"] is None and by_id["my-fine-tune"]["free"] is None  # unknown, not guessed


def test_anthropic_list_is_paged_and_dotted_names_match(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    asked = serve(monkeypatch, {
        "https://api.anthropic.com/v1/models?limit=1000&after_id=b": {"data": [{"id": "claude-sonnet-4-5-20250929"}], "has_more": False},
        "https://api.anthropic.com/v1/models?limit=1000": {"data": [{"id": "claude-a"}, {"id": "claude-b"}], "has_more": True, "last_id": "b"},
        "https://openrouter.ai/api/v1/models": {"data": [
            {"id": "anthropic/claude-sonnet-4.5", "pricing": {"prompt": "0.000003", "completion": "0.000015"}}]},
    })
    by_id = {m["id"]: m for m in prov.model_catalog("anthropic")["models"]}
    assert set(by_id) == {"claude-a", "claude-b", "claude-sonnet-4-5-20250929"}
    assert by_id["claude-sonnet-4-5-20250929"]["output"] == 15.0
    assert any("after_id=b" in url for url in asked)


def test_together_prices_are_read_and_non_chat_models_left_out(monkeypatch):
    monkeypatch.setenv("TOGETHER_API_KEY", "k")
    serve(monkeypatch, {"https://api.together.xyz/v1/models": [
        {"id": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "type": "chat", "context_length": 131072,
         "pricing": {"input": 0.88, "output": 0.88}},
        {"id": "BAAI/bge-large", "type": "embedding", "pricing": {"input": 0.02, "output": 0}},
    ]})
    models = prov.model_catalog("together")["models"]
    assert [m["id"] for m in models] == ["meta-llama/Llama-3.3-70B-Instruct-Turbo"]
    assert models[0]["input"] == 0.88 and models[0]["free"] is False


def test_a_refused_key_says_so_and_falls_back_to_the_last_good_list(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    serve(monkeypatch, {"https://openrouter.ai/api/v1/models": OPENROUTER})
    assert prov.model_catalog("openrouter")["live"]
    refused = urllib.error.HTTPError("https://openrouter.ai/api/v1/models", 401, "Unauthorized", {}, None)
    serve(monkeypatch, {"https://openrouter.ai/api/v1/models": refused})
    cat = prov.model_catalog("openrouter")
    assert not cat["live"] and "refused the API key (HTTP 401)" in cat["error"]
    assert len(cat["models"]) == 4 and cat["fetched_at"]  # the list from before, marked as old
    monkeypatch.setenv("OPENROUTER_API_KEY", "another")  # a different key does not reuse it
    cat = prov.model_catalog("openrouter")
    assert [m["id"] for m in cat["models"]] == list(prov.PROVIDERS["openrouter"].suggestions)


def test_without_a_key_the_suggestions_come_with_the_reason(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    asked = serve(monkeypatch, {})
    cat = prov.model_catalog("groq")
    assert not cat["live"] and "Add a Groq API key" in cat["error"]
    assert [m["id"] for m in cat["models"]] == list(prov.PROVIDERS["groq"].suggestions)
    assert asked == []


def test_local_models_are_free(monkeypatch):
    serve(monkeypatch, {"http://localhost:11434/v1/models": {"data": [{"id": "qwen2.5:7b-instruct"}]}})
    (m,) = prov.model_catalog("ollama")["models"]
    assert m["free"] is True and m["price_source"] == "local"


# ----- the terminal chooser -----

CATALOG = {"live": True, "error": None, "fetched_at": 1.0, "models": [
    {"id": "openrouter/auto", "name": "", "context": None, "tools": True, "input": None, "output": None, "free": None},
    {"id": "openai/gpt-4.1", "name": "GPT-4.1", "context": 1047576, "tools": True, "input": 2.0, "output": 8.0, "free": False},
    {"id": "meta-llama/llama-3.3-70b-instruct:free", "name": "", "context": 131072, "tools": True, "input": 0.0, "output": 0.0, "free": True},
    {"id": "google/gemma-3-27b-it:free", "name": "", "context": 96000, "tools": False, "input": 0.0, "output": 0.0, "free": True},
] + [{"id": f"vendor/model-{i:02d}", "name": "", "context": None, "tools": None, "input": 1.0, "output": 1.0, "free": False}
     for i in range(40)]}


def answers(monkeypatch, *typed):
    it = iter(typed)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def test_terminal_chooser_searches_filters_free_and_pages(monkeypatch, capsys):
    p = prov.PROVIDERS["openrouter"]
    answers(monkeypatch, "free", "2")
    assert onboarding.choose_model(p, CATALOG, None) == "google/gemma-3-27b-it:free"
    out = capsys.readouterr().out
    assert "44 of 44" in out and "2 free" in out and "Free" in out and "no tools" in out
    answers(monkeypatch, "gpt", "1")
    assert onboarding.choose_model(p, CATALOG, None) == "openai/gpt-4.1"
    answers(monkeypatch, "", "44")  # Enter shows the next page; numbers count across pages
    assert onboarding.choose_model(p, CATALOG, None) == "vendor/model-39"
    assert "model-39" in capsys.readouterr().out
    answers(monkeypatch, "openai/gpt-4.1")
    assert onboarding.choose_model(p, CATALOG, None) == "openai/gpt-4.1"
    answers(monkeypatch, "acme/brand-new-model", "y")
    assert onboarding.choose_model(p, CATALOG, None) == "acme/brand-new-model"
    answers(monkeypatch, "", "")  # Enter on the last page keeps the current model
    assert onboarding.choose_model(p, CATALOG, "openai/gpt-4.1") == "openai/gpt-4.1"


def test_terminal_chooser_shows_why_the_list_is_short(monkeypatch, capsys):
    cat = {"live": False, "error": "OpenRouter refused the API key (HTTP 401).", "fetched_at": None,
           "models": CATALOG["models"][:1]}
    answers(monkeypatch, "1")
    assert onboarding.choose_model(prov.PROVIDERS["openrouter"], cat, None) == "openrouter/auto"
    assert "refused the API key" in capsys.readouterr().out


@pytest.mark.parametrize("value,text", [({"input": 0.15, "output": 0.6}, "$0.15 / $0.6"), ({"input": 3, "output": 15}, "$3 / $15"),
                                        ({"free": True}, "Free"), ({"free": True, "price_source": "local"}, "Local"), ({}, "")])
def test_price_text(value, text):
    assert onboarding.model_price(value) == text
