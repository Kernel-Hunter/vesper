"""Chat models for many providers.

Every other module asks this file for a chat model, so a provider
is added here and nowhere else. Most providers speak the OpenAI API, so one client with a
different base URL covers them. Anthropic and Ollama get their own clients.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

APP_NAME = "Vesper"


def resolve_home(user_home: Path | None = None) -> Path:
    """Per-user folder: settings, keys and the session database. VESPER_HOME overrides ~/.vesper."""
    override = os.getenv("VESPER_HOME")
    if override:
        return Path(override)
    return (Path(user_home) if user_home is not None else Path.home()) / ".vesper"


HOME = resolve_home()
USER_ENV = HOME / ".env"

# Only the app's own key file. A .env in whatever folder Vesper runs in belongs to that project.
# Variables set in the shell win over the file. The terminal and the desktop app both write
# this file, so it is read again whenever it changes (see refresh_keys).
_FROM_SHELL = frozenset(os.environ)
_env_state: dict = {"mtime": None, "keys": set()}


def refresh_keys() -> None:
    """Pick up keys another Vesper process saved (the terminal app or the desktop app)."""
    try:
        mtime = USER_ENV.stat().st_mtime_ns
    except OSError:
        mtime = None
    if mtime == _env_state["mtime"]:
        return
    values = {k: v for k, v in (dotenv_values(USER_ENV) if mtime is not None else {}).items() if v}
    for key in _env_state["keys"] - set(values):  # removed from the file since the last read
        if key not in _FROM_SHELL:
            os.environ.pop(key, None)
    for key, value in values.items():
        if key not in _FROM_SHELL:
            os.environ[key] = value
    _env_state.update(mtime=mtime, keys=set(values))


refresh_keys()


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    kind: str  # openai | anthropic
    base_url: str | None
    key_env: str | None
    chat_model: str
    key_url: str = ""
    suggestions: tuple[str, ...] = field(default_factory=tuple)
    local: bool = False


PROVIDERS: dict[str, Provider] = {
    p.id: p
    for p in [
        Provider("openai", "OpenAI", "openai", None, "OPENAI_API_KEY", "gpt-4.1-nano",
                 "https://platform.openai.com/api-keys", ("gpt-4.1-nano", "gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini")),
        Provider("anthropic", "Anthropic Claude", "anthropic", None, "ANTHROPIC_API_KEY", "claude-haiku-5-5",
                 "https://console.anthropic.com/settings/keys", ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")),
        Provider("google", "Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY", "gemini-2.5-flash",
                 "https://aistudio.google.com/apikey", ("gemini-2.5-flash", "gemini-2.5-pro")),
        Provider("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat",
                 "https://platform.deepseek.com/api_keys", ("deepseek-chat", "deepseek-flash", "deepseek-v4-pro", "deepseek-reasoner")),
        Provider("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", "mistral-small-latest",
                 "https://console.mistral.ai/api-keys", ("mistral-small-latest", "mistral-medium-latest", "mistral-large-latest")),
        Provider("groq", "Groq", "openai", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile",
                 "https://console.groq.com/keys", ("llama-3.3-70b-versatile", "llama-3.1-8b-instant")),
        Provider("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "openrouter/auto",
                 "https://openrouter.ai/keys", ("openrouter/auto", "deepseek/deepseek-chat", "meta-llama/llama-3.3-70b-instruct")),
        Provider("xai", "xAI Grok", "openai", "https://api.x.ai/v1", "XAI_API_KEY", "grok-3-mini",
                 "https://console.x.ai", ("grok-3-mini", "grok-3")),
        Provider("together", "Together AI", "openai", "https://api.together.xyz/v1", "TOGETHER_API_KEY", "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                 "https://api.together.ai/settings/api-keys"),
        Provider("fireworks", "Fireworks AI", "openai", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY", "accounts/fireworks/models/llama-v3p3-70b-instruct",
                 "https://fireworks.ai/account/api-keys"),
        Provider("cerebras", "Cerebras", "openai", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "llama-3.3-70b",
                 "https://cloud.cerebras.ai"),
        Provider("nvidia", "NVIDIA NIM", "openai", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY", "meta/llama-3.3-70b-instruct",
                 "https://build.nvidia.com"),
        Provider("huggingface", "Hugging Face", "openai", "https://router.huggingface.co/v1", "HF_TOKEN", "meta-llama/Llama-3.3-70B-Instruct",
                 "https://huggingface.co/settings/tokens"),
        Provider("moonshot", "Moonshot Kimi", "openai", "https://api.moonshot.ai/v1", "MOONSHOT_API_KEY", "kimi-k2-0905-preview",
                 "https://platform.moonshot.ai"),
        Provider("ollama", "Ollama (local)", "openai", "http://localhost:11434/v1", None, "qwen2.5:7b-instruct",
                 "https://ollama.com/download", ("qwen2.5:7b-instruct", "llama3.2", "gemma3"), local=True),
        Provider("lmstudio", "LM Studio (local)", "openai", "http://localhost:1234/v1", None, "local-model",
                 "https://lmstudio.ai", local=True),
        Provider("custom", "Custom (OpenAI-compatible)", "openai", "", "CUSTOM_API_KEY", "",
                 ""),
    ]
}



# Ollama loads a model with a 4096-token window unless told otherwise. That is too small for
# an agent (the system prompt, the tool list and one file fill it), so Vesper asks for more.
OLLAMA_CONTEXT_TOKENS = 16_384
DEFAULT_CONTEXT_TOKENS = 120_000  # assumed for hosted models


def ollama_context_tokens() -> int:
    """Context window requested from Ollama. VESPER_OLLAMA_CTX overrides it."""
    try:
        return max(2048, int(os.getenv("VESPER_OLLAMA_CTX", "")))
    except ValueError:
        return OLLAMA_CONTEXT_TOKENS


def context_window(provider: str | None, model: str | None = None) -> tuple[int, str]:
    """Window and provenance. Unknown aliases remain an assumption, not a model claim.

    Provider documentation: platform.openai.com/docs/models, docs.anthropic.com/en/docs/about-claude/models/overview,
    console.groq.com/docs/models. Only match documented IDs on the provider that serves them;
    routing services and custom endpoints may impose different limits.
    """
    if provider == "ollama":
        return ollama_context_tokens(), "requested"
    p = PROVIDERS.get(provider or "")
    name = (model or (p.chat_model if p else "")).lower()
    if provider == "openai":
        if re.fullmatch(r"gpt-4\.1(?:-(?:mini|nano))?(?:-2025-04-14)?", name):
            return 1_047_576, "documented"
        if re.fullmatch(r"gpt-4o(?:-mini)?(?:-\d{4}-\d{2}-\d{2})?", name):
            return 128_000, "documented"
    if provider == "anthropic":
        if re.fullmatch(r"claude-(?:opus|sonnet|haiku)-5-5(?:-\d{8})?", name) or re.fullmatch(r"claude-(?:opus|sonnet)-4-6(?:-\d{8})?", name):
            return 1_000_000, "documented"
        if re.fullmatch(r"claude-(?:opus|sonnet|haiku)-4-5(?:-\d{8})?", name):
            return 200_000, "documented"
    if provider == "groq" and name in {"llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-120b", "openai/gpt-oss-20b"}:
        return 131_072, "documented"
    return DEFAULT_CONTEXT_TOKENS, "assumed"


def context_tokens(provider: str | None, model: str | None = None) -> int:
    """The documented, requested or assumed context window for this model."""
    return context_window(provider, model)[0]


def _key(provider: Provider) -> str | None:
    refresh_keys()
    return os.getenv(provider.key_env) if provider.key_env else None


def _base_url(provider: Provider) -> str | None:
    refresh_keys()
    if provider.id == "custom":
        return os.getenv("CUSTOM_BASE_URL") or None
    if provider.id == "ollama":
        return os.getenv("OLLAMA_BASE_URL", provider.base_url)
    return provider.base_url


FAKE_ANSWER = "Hello from the Vesper demo model."


def fake_llm_enabled() -> bool:
    """VESPER_FAKE_LLM=1: offline fake chat model, for UI end-to-end tests."""
    return os.getenv("VESPER_FAKE_LLM", "").strip().lower() in {"1", "true", "yes"}


def _fake_chat_model():
    import itertools

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class FakeChat(GenericFakeChatModel):
        """Streams FAKE_ANSWER word by word. With the folder tools bound it plays a short script
        instead (list the folder, write a note, answer) so the tool and approval UI can be tested."""

        folder: bool = False

        def bind_tools(self, tools, **kwargs):
            names = {getattr(t, "name", "") for t in tools}
            if "write_file" in names:
                return FakeChat(messages=self.messages, folder=True, disable_streaming=True)
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if not self.folder:
                return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            last = messages[-1] if messages else None
            if not isinstance(last, ToolMessage) and "task list" in str(getattr(last, "content", "")).lower():
                msg = AIMessage(content="", tool_calls=[{"name": "update_tasks", "id": "fake-tasks", "args": {
                    "tasks": "[x] Understand the request\n[>] Look at the folder\n[ ] Write the note"}}])
            elif not isinstance(last, ToolMessage) or last.name == "update_tasks":
                msg = AIMessage(content="<think>The user wants something done in this folder. "
                                        "I will list it, then write a short note.</think>Let me look at the folder first.",
                                tool_calls=[{"name": "list_files", "args": {"path": "."}, "id": "fake-list"}])
            elif last.name == "list_files":
                msg = AIMessage(content="", tool_calls=[{"name": "write_file", "id": "fake-write", "args": {
                    "path": "vesper-notes.md", "content": "# Notes\n\nWritten by the Vesper demo model.\n"}}])
            else:
                msg = AIMessage(content=f"Done. The write said: {last.content}")
            return ChatResult(generations=[ChatGeneration(message=msg)])

    return FakeChat(messages=itertools.repeat(AIMessage(content=FAKE_ANSWER)))


THINKING = ("off", "low", "medium", "high")
# Claude models from before adaptive thinking: they take a fixed thinking budget instead.
OLD_CLAUDE = ("claude-3", "claude-haiku-4", "claude-sonnet-4-0", "claude-sonnet-4-5", "claude-opus-4-0",
              "claude-opus-4-1", "claude-opus-4-5", "claude-sonnet-4-2", "claude-opus-4-2")


def get_llm(provider: str | None = None, model: str | None = None, temperature: float = 0,
            thinking: str = "off"):
    """The chat model. `thinking` (low | medium | high) asks reasoning models to think and to
    show it; "off" sends nothing extra, so each model does what it does by default."""
    thinking = thinking if thinking in THINKING else "off"
    if fake_llm_enabled():
        return _fake_chat_model()
    pid = (provider or os.getenv("LLM_PROVIDER", "openai")).lower()
    if pid not in PROVIDERS:
        raise ValueError(f"Unknown LLM_PROVIDER {pid!r}. Use one of {sorted(PROVIDERS)}.")
    p = PROVIDERS[pid]
    model = model or os.getenv("LLM_MODEL") or p.chat_model
    if p.kind == "anthropic":
        from langchain_anthropic import ChatAnthropic

        # No temperature: current Claude models reject any value but their default.
        extra: dict = {}
        if thinking != "off" and model.startswith(OLD_CLAUDE):
            extra = {"thinking": {"type": "enabled", "budget_tokens": 4096}, "max_tokens": 16_000}
        elif thinking != "off":
            extra = {"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": thinking}}
        return ChatAnthropic(model=model, api_key=_key(p) or "not-set", **extra)
    if p.id == "ollama":
        from langchain_ollama import ChatOllama

        # The native API, not the OpenAI-compatible one: only it accepts a context size.
        base = (_base_url(p) or "").rstrip("/").removesuffix("/v1")
        return ChatOllama(model=model, temperature=temperature, base_url=base, num_ctx=ollama_context_tokens(),
                          reasoning=True if thinking != "off" else None)
    from langchain_openai import ChatOpenAI

    if p.id == "openai":
        # The Responses API, with no temperature: OpenAI's newer models refuse tools on the
        # older chat endpoint and refuse a temperature, and every chat model accepts this.
        extra = {"reasoning": {"effort": thinking, "summary": "auto"}} if thinking != "off" else {}
        # No key yet: build anyway (the openai package refuses a missing key at construction);
        # the first request then fails with a clear "add your key" error instead of a crash.
        return ChatOpenAI(model=model, api_key=_key(p) or "not-set", use_responses_api=True, **extra)
    if thinking != "off":  # reasoning models take an effort level and no temperature
        return ChatOpenAI(model=model, base_url=_base_url(p), api_key=_key(p) or "not-needed",
                          reasoning_effort=thinking)
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        base_url=_base_url(p),
        api_key=_key(p) or "not-needed",  # local servers ignore it
    )


REFUSED = ("unsupported", "not supported", "does not support", "doesn't support", "not allowed", "only the default",
           "invalid", "unknown parameter", "unrecognized", "extra inputs")
# Optional settings a provider may refuse, by the word its error message uses for them.
OPTIONAL_SETTINGS = {
    "temperature": ("temperature",),
    "reasoning": ("reasoning_effort", "reasoning"),
    "thinking": ("thinking", "output_config", "reasoning"),
    "think": ("reasoning",),
}


def adapt(llm, exc: BaseException):
    """A copy of `llm` without the optional setting the provider just refused, or None.

    Providers disagree about which models take a temperature or a thinking level, and the
    list changes with every model release. So the request is the test: when a model answers
    "temperature is not supported", the next try goes without one.
    """
    text = str(exc).lower()
    if not any(word in text for word in REFUSED):
        return None
    update = {}
    for word, fields in OPTIONAL_SETTINGS.items():
        if word in text:
            update.update({f: None for f in fields if getattr(llm, f, None) is not None})
    if not update or not hasattr(llm, "model_copy"):
        return None
    return llm.model_copy(update=update)


# Model ids that are not chat models, so they are left out of the model picker.
NOT_CHAT = ("embedding", "embed-", "tts", "whisper", "image", "audio", "realtime", "moderation", "transcribe",
            "dall-e", "sora", "rerank", "guard")


def agent_middleware(llm) -> list:
    """Provider-specific helpers for the agent loop. For Claude: prompt caching, so the system
    prompt, the tool list and the earlier conversation are billed at a fraction on every step."""
    if type(llm).__name__ != "ChatAnthropic":
        return []
    from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware

    return [AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore")]


# Where a provider's own model list has no prices, the same model on OpenRouter gives the list
# price (OpenRouter charges what the model's maker charges). Only exact id matches are used.
PRICE_SOURCES = {"openai": "openai", "anthropic": "anthropic", "google": "google", "deepseek": "deepseek",
                 "mistral": "mistralai", "xai": "x-ai", "moonshot": "moonshotai"}
CATALOG_TIMEOUT = 20.0
PRICE_LIST_TTL = 24 * 3600


def _per_million(value) -> float | None:
    """A price per token (OpenRouter sends strings) as dollars per million tokens. Negative means "varies"."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return None if price < 0 else round(price * 1_000_000, 6)


def _model_key(model_id: str) -> str:
    """claude-sonnet-4-5-20250929, anthropic/claude-sonnet-4.5 and models/x compare equal."""
    key = model_id.lower().removeprefix("models/").split("/")[-1]
    key = re.sub(r"(-\d{8}|-\d{4}-\d{2}-\d{2})$", "", key)
    return key.replace(".", "-")


def _price_fields(prompt, completion) -> dict:
    price_in, price_out = _per_million(prompt), _per_million(completion)
    free = None if price_in is None or price_out is None else price_in == 0 and price_out == 0
    return {"input": price_in, "output": price_out, "free": free}


def _from_openrouter(item: dict) -> dict | None:
    arch = item.get("architecture") or {}
    outputs = arch.get("output_modalities")
    if outputs and "text" not in outputs:
        return None  # image, audio or embedding models
    params = item.get("supported_parameters")
    pricing = item.get("pricing") or {}
    return {"id": item["id"], "name": item.get("name") or "", "context": item.get("context_length"),
            "tools": None if params is None else "tools" in params,
            **_price_fields(pricing.get("prompt"), pricing.get("completion")), "price_source": "provider"}


def _from_together(item: dict) -> dict | None:
    if item.get("type") not in (None, "chat", "language", "code"):
        return None
    pricing = item.get("pricing") or {}
    entry = {"id": item["id"], "name": item.get("display_name") or "", "context": item.get("context_length"),
             "tools": None, "input": None, "output": None, "free": None, "price_source": None}
    if "input" in pricing and "output" in pricing:  # Together prices per million tokens already
        price_in, price_out = float(pricing["input"]), float(pricing["output"])
        entry.update(input=price_in, output=price_out, free=price_in == 0 and price_out == 0, price_source="provider")
    return entry


def _from_generic(item: dict) -> dict | None:
    model_id = str(item.get("id") or item.get("name") or "").removeprefix("models/")
    if not model_id:
        return None
    caps = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
    if caps.get("completion_chat") is False:  # Mistral marks models that cannot chat
        return None
    context = item.get("context_window") or item.get("context_length") or item.get("max_context_length")
    tools = caps.get("function_calling") if "function_calling" in caps else None
    return {"id": model_id, "name": item.get("display_name") or "", "context": context if isinstance(context, int) else None,
            "tools": tools, "input": None, "output": None, "free": None, "price_source": None}


def _http_json(url: str, headers: dict, timeout: float):
    from vesper.webtools import (
        _tls,  # certifi's list: the packaged app has no usable system one
    )

    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout, context=_tls()) as resp:
        return json.load(resp)


def _friendly_fetch_error(p: Provider, exc: Exception, timeout: float) -> str:
    code = getattr(exc, "code", None)
    if code in (401, 403):
        return f"{p.label} refused the API key (HTTP {code}). Check the key in Settings or with /key."
    if code:
        return f"{p.label} answered HTTP {code} when asked for its models."
    if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
        return f"{p.label} did not send its model list within {timeout:.0f} seconds."
    if p.local:
        return f"{p.label} is not running."
    if p.id == "custom":
        return (f"Could not reach your custom server at {_base_url(p)}. Check the base URL in "
                f"Settings, Models, Local and custom.")
    return f"Could not reach {p.label}: {str(exc)[:120]}"


def _fetch_models(p: Provider, timeout: float) -> list[dict]:
    if p.kind == "anthropic":
        headers = {"x-api-key": _key(p) or "", "anthropic-version": "2023-06-01"}
        models, after = [], ""
        for _ in range(20):  # the list is paged
            data = _http_json(f"https://api.anthropic.com/v1/models?limit=1000{after}", headers, timeout)
            models += [{"id": m["id"], "name": m.get("display_name") or "", "context": None, "tools": True,
                        "input": None, "output": None, "free": None, "price_source": None} for m in data.get("data", [])]
            if not data.get("has_more") or not data.get("last_id"):
                break
            after = f"&after_id={data['last_id']}"
        return models
    base = (_base_url(p) or "https://api.openai.com/v1").rstrip("/")
    data = _http_json(base + "/models", {"Authorization": f"Bearer {_key(p) or 'not-needed'}"}, timeout)
    items = data if isinstance(data, list) else data.get("data", data.get("models", []))
    parse = _from_openrouter if p.id == "openrouter" else _from_together if p.id == "together" else _from_generic
    models = [m for m in (parse(i) for i in items if isinstance(i, dict) and (i.get("id") or i.get("name"))) if m]
    if p.id not in ("openrouter", "together"):
        chat = [m for m in models if not any(word in m["id"].lower() for word in NOT_CHAT)]
        models = chat or models  # a list of nothing else is shown as it is
    if p.local:
        for m in models:
            m.update(input=0.0, output=0.0, free=True, price_source="local")
    return models


def _cache_dir() -> Path:
    return HOME / "cache"


def _write_json(path: Path, value) -> None:
    import tempfile

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f)
        os.replace(tmp, path)
    except OSError:
        pass  # a cache only


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _public_prices(timeout: float) -> dict[str, dict]:
    """OpenRouter's public price list (no key needed), kept for a day. {model key: price fields}."""
    import time

    path = _cache_dir() / "openrouter-prices.json"
    cached = _read_json(path)
    if not isinstance(cached, dict) or time.time() - cached.get("at", 0) > PRICE_LIST_TTL:
        try:
            data = _http_json("https://openrouter.ai/api/v1/models", {}, min(timeout, 10.0))
            prices = {}
            for item in data.get("data", []):
                if ":" in item.get("id", ""):  # :free, :batch and other variants are not the base model's price
                    continue
                entry = _from_openrouter(item)
                if entry and entry["input"] is not None and "/" in entry["id"]:
                    vendor = entry["id"].split("/")[0]
                    prices[f"{vendor}/{_model_key(entry['id'])}"] = {k: entry[k] for k in ("input", "output", "free", "context")}
            cached = {"at": time.time(), "prices": prices}
            _write_json(path, cached)
        except Exception:
            return (cached or {}).get("prices", {}) if isinstance(cached, dict) else {}
    return cached.get("prices", {})


def _add_list_prices(p: Provider, models: list[dict], timeout: float) -> None:
    vendor = PRICE_SOURCES.get(p.id)
    if not vendor or all(m["input"] is not None for m in models):
        return
    prices = _public_prices(timeout)
    for m in models:
        found = prices.get(f"{vendor}/{_model_key(m['id'])}")
        if found and m["input"] is None:
            m.update(input=found["input"], output=found["output"], free=found["free"], price_source="openrouter")
            m["context"] = m["context"] or found.get("context")


def _fingerprint(p: Provider) -> str:
    import hashlib

    return hashlib.sha256(f"{p.id}|{_base_url(p) or ''}|{_key(p) or ''}".encode()).hexdigest()[:16]


def model_catalog(provider: str, timeout: float = CATALOG_TIMEOUT) -> dict:
    """Every model the provider serves to this key, with what is known about each one.

    Returns {"models": [...], "live": bool, "error": str | None, "fetched_at": float | None}.
    Each model: id, name, context (tokens), tools (True, False or None when unknown), input and output
    (US dollars per million tokens, None when unknown), free (True, False or None), price_source
    ("provider", "openrouter" for the maker's list price, "local", or None).
    "live" is False when the list could not be fetched: then the last list fetched with the same key,
    or the built-in suggestions, are returned with the reason in "error".
    """
    import time

    p = PROVIDERS[provider]
    blank = {"name": "", "context": None, "tools": None, "input": None, "output": None, "free": None, "price_source": None}
    fallback = [{"id": m, **blank} for m in (list(p.suggestions) or ([p.chat_model] if p.chat_model else []))]
    if p.local:
        for m in fallback:
            m.update(input=0.0, output=0.0, free=True, price_source="local")
    def without_list(error: str) -> dict:
        _add_list_prices(p, fallback, timeout)
        return {"models": fallback, "live": False, "error": error, "fetched_at": None}

    if p.id == "custom" and not _base_url(p):
        return {"models": [], "live": False, "error": "Set the base URL of your server first.", "fetched_at": None}
    if not p.local and p.id != "custom" and not _key(p):
        return without_list(f"Add a {p.label} API key to see its models.")
    cache = _cache_dir() / f"models-{p.id}.json"
    try:
        models = _fetch_models(p, timeout)
    except Exception as exc:
        error = _friendly_fetch_error(p, exc, timeout)
        saved = _read_json(cache)
        if isinstance(saved, dict) and saved.get("fingerprint") == _fingerprint(p) and saved.get("models"):
            return {"models": saved["models"], "live": False, "error": error, "fetched_at": saved.get("at")}
        return without_list(error)
    if not models:
        return without_list(f"{p.label} sent an empty model list.")
    _add_list_prices(p, models, timeout)
    order = {m: i for i, m in enumerate(p.suggestions)}
    unique = {m["id"]: m for m in models}
    models = sorted(unique.values(), key=lambda m: (order.get(m["id"], len(order)), m["id"].lower()))
    now = time.time()
    _write_json(cache, {"fingerprint": _fingerprint(p), "at": now, "models": models})
    return {"models": models, "live": True, "error": None, "fetched_at": now}


def known_price(provider: str, model: str) -> dict | None:
    """{"input", "output"} in dollars per million tokens from the saved lists, or None.

    Never touches the network: it reads the provider's last model list and the saved public
    price list, so working out a cost cannot slow an answer down.
    """
    p = PROVIDERS.get(provider)
    if p is None or not model:
        return None
    if p.local:
        return {"input": 0.0, "output": 0.0}
    saved = _read_json(_cache_dir() / f"models-{p.id}.json")
    for m in (saved or {}).get("models", []) if isinstance(saved, dict) else []:
        if isinstance(m, dict) and m.get("id") == model and m.get("input") is not None and m.get("output") is not None:
            return {"input": float(m["input"]), "output": float(m["output"])}
    vendor = PRICE_SOURCES.get(p.id)
    cached = _read_json(_cache_dir() / "openrouter-prices.json")
    prices = cached.get("prices", {}) if isinstance(cached, dict) else {}
    found = prices.get(f"{vendor}/{_model_key(model)}") if vendor else None
    if found and found.get("input") is not None and found.get("output") is not None:
        return {"input": float(found["input"]), "output": float(found["output"])}
    return None


def list_models(provider: str, timeout: float = CATALOG_TIMEOUT) -> list[str]:
    """The ids from model_catalog: the live list, or the built-in suggestions when it cannot be fetched."""
    return [m["id"] for m in model_catalog(provider, timeout)["models"]]


def _restrict(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:  # Windows ignores POSIX modes
        pass


def save_user_settings(values: dict[str, str]) -> Path:
    """Merge values into HOME/.env (readable only by the current user).

    Values with a line break are refused: they would inject extra variables into the file.
    The file is replaced atomically, so a crash never leaves it half written.
    """
    import tempfile

    for key, value in values.items():
        if not isinstance(key, str) or not key or any(c in key for c in "\r\n=#") or key != key.strip():
            raise ValueError(f"invalid setting name {key!r}")
        if value is not None and ("\n" in str(value) or "\r" in str(value)):
            raise ValueError(f"{key} must not contain a line break")
    HOME.mkdir(parents=True, exist_ok=True)
    _restrict(HOME, 0o700)
    current: dict[str, str] = {}
    if USER_ENV.exists():
        for line in USER_ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                current[key.strip()] = value.strip()
    for key, value in values.items():
        if value:
            current[key] = value
            os.environ[key] = value
        else:
            current.pop(key, None)
            os.environ.pop(key, None)
    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=HOME)  # created 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(f"{k}={v}\n" for k, v in current.items()))
            f.flush()
            os.fsync(f.fileno())
        _restrict(Path(tmp), 0o600)
        os.replace(tmp, USER_ENV)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _restrict(USER_ENV, 0o600)
    return USER_ENV
