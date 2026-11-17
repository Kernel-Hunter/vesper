"""Automatic model switching: when a model or key runs out of credits, use the next one.

Turned on with `/fallback on` or in Settings. The chain is the model you picked, then the paid
models your other keys reach, then free models ranked best first, then a local model. You can
also save your own order. When a call fails because of credits, quota, a rate limit, a bad key
or a model that is gone, the agent switches to the next model in the chain and repeats the same
step, so the turn goes on. A model that failed is skipped for a while (minutes after a rate
limit, hours after running out of credits) and tried again later.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from vesper import onboarding

# Model families from strongest to weakest for agentic coding with tools. Unknown models rank
# after every listed family; a model id matches the first family it contains.
FAMILY_RANK: tuple[str, ...] = (
    "claude-opus", "gpt-6", "gemini-4", "claude-sonnet", "gpt-5", "glm-5.3", "inkling-2", "minimax-m3",
    "kimi-k3", "deepseek-v4", "gemini-3.8", "grok-4", "qwen3.7", "muse-spark", "muse-glimmer", "mistral-large-4",
    "mistral-medium-3.5", "step-5", "nemotron-3-ultra", "glm-5", "kimi-k2", "qwen3.8", "qwen-3.8", "minimax-m2",
    "gemini-3.6", "north-mini-code", "nemotron-3.5-super", "qwen3-coder", "glm-4.6", "deepseek-v3", "deepseek-r1",
    "gpt-oss-120b", "gemini-3.5-flash-lite", "gemini-3", "devstral", "mistral-large", "codestral", "gemma-4-31b",
    "gemma-4", "llama-4-maverick", "qwen3-235b", "claude-haiku", "nemotron-3.5-lightning", "mistral-medium",
    "qwen3-32b", "gpt-oss-20b", "llama-3.3-70b", "mistral-small", "gemma-3", "llama-3.1-8b",
)

RATE_COOLDOWN_S = 10 * 60        # a rate limit usually clears within minutes
CREDIT_COOLDOWN_S = 6 * 60 * 60  # credits, quota or a bad key: try again much later
CHAIN_CACHE_S = 30 * 60
MAX_CHAIN = 30

# Clear signs that the money or the quota is gone (a wait of minutes will not help).
_CREDIT = re.compile(r"insufficient[_ ](quota|credits?|balance|funds)|credit balance|exceeded[_ ](your[_ ])?current[_ ]quota|"
                     r"out of (credits|tokens)|top[ -]?up|billing_error|payment[_ ]required|budget limit|\blimit: 0\b|"
                     r"free-models-per-day|monthly included credits|quota exceeded", re.I)
# Weaker words that point at money only when nothing says "rate limit".
_CREDIT_WEAK = re.compile(r"credit|balance|billing|payment|subscription|plan limit|upgrade", re.I)
# A rate limit that says it ends soon.
_SOON = re.compile(r"per[_ ]?minute|try again in \d|retry in \d|retry[_ -]after|requests per|tokens per", re.I)
_RATE = re.compile(r"rate[_ ]?limit|too many requests|resource[_ ]?exhausted|requests per|tokens per|"
                   r"overloaded|capacity|try again later|temporarily unavailable", re.I)
_AUTH = re.compile(r"invalid[_ ]api[_ ]key|incorrect api key|unauthori[sz]ed|authentication|permission denied|"
                   r"api key not valid|no auth credentials", re.I)
_GONE = re.compile(r"model[^.]{0,80}(not found|does not exist|decommissioned|deprecated|no longer available|"
                   r"not available)|no endpoints found|unknown model", re.I)
_CONTEXT = re.compile(r"context[_ ]length|maximum context|too many tokens in|prompt is too long|reduce the length", re.I)


def _status(exc: BaseException) -> int | None:
    for obj in (exc, getattr(exc, "response", None), getattr(exc, "__cause__", None)):
        code = getattr(obj, "status_code", None) or getattr(obj, "status", None) or getattr(obj, "code", None)
        if isinstance(code, int) and 100 <= code < 600:
            return code
    return None


def classify(exc: BaseException) -> str | None:
    """Why a model call failed, when switching models can help: "credits", "rate", "auth" or "gone".

    None for everything else (a bad request, a too-long prompt, a network error, a bug), which the
    caller handles as before.
    """
    text = f"{type(exc).__name__}: {exc}"
    if _CONTEXT.search(text):
        return None  # the next model would fail the same way; trimming handles it
    status = _status(exc)
    if status == 429 and _SOON.search(text):
        return "rate"
    if status == 402 or _CREDIT.search(text):
        return "credits"
    if status in (401, 403) or _AUTH.search(text):
        return "auth"
    if (status == 404 and re.search(r"model", text, re.I)) or _GONE.search(text):
        return "gone"
    if status in (429, 503, 529) or _RATE.search(text):
        return "rate"
    if _CREDIT_WEAK.search(text) and status in (None, 400):
        return "credits"
    return None


REASONS = {
    "credits": "ran out of credits or quota", "rate": "hit a rate limit", "auth": "refused the API key",
    "gone": "is not available any more",
}


def settings() -> dict:
    """{"enabled": bool, "chain": [{"provider", "model"}] or None for the automatic order}."""
    raw = onboarding.load_config().get("fallback")
    raw = raw if isinstance(raw, dict) else {}
    chain = raw.get("chain")
    if isinstance(chain, list):
        chain = [{"provider": str(c["provider"]), "model": str(c["model"])} for c in chain
                 if isinstance(c, dict) and c.get("provider") and c.get("model")][:MAX_CHAIN]
    else:
        chain = None
    return {"enabled": bool(raw.get("enabled")), "chain": chain}


def save(enabled: bool | None = None, chain: list[dict] | None | bool = False) -> dict:
    """Change the setting. `chain=None` goes back to the automatic order; False leaves it as it is."""
    from vesper import providers as prov

    now = settings()
    if enabled is not None:
        now["enabled"] = bool(enabled)
    if chain is not False:
        if chain is not None:
            if not isinstance(chain, list):
                raise ValueError("The order must be a list of models.")
            clean = []
            for c in chain[:MAX_CHAIN]:
                if not isinstance(c, dict) or c.get("provider") not in prov.PROVIDERS or not str(c.get("model", "")).strip():
                    raise ValueError("Each entry needs a known provider and a model id.")
                clean.append({"provider": c["provider"], "model": str(c["model"]).strip()[:200]})
            chain = clean or None  # an empty order means the automatic one
        now["chain"] = chain
    onboarding.save_config({"fallback": now})
    _cache.clear()
    return now


def rank(model_id: str) -> int:
    low = model_id.lower()
    for i, family in enumerate(FAMILY_RANK):
        if family in low:
            return i
    return len(FAMILY_RANK)


def _variant_penalty(model_id: str) -> int:
    """Within a family, prefer the full model over mini, nano, lite or preview builds."""
    low = model_id.lower()
    return sum(word in low for word in ("mini", "nano", "lite", "tiny", "small", "preview", "exp", "-8b", "-3b", "1b"))


def _skip(model_id: str) -> bool:
    """Models that are not chat models: embeddings, speech, images, moderation, guards."""
    return bool(re.search(r"embed|whisper|tts|audio|image|moderation|guard|rerank|ocr|dall|sora|"
                          r"transcri|realtime|search-preview", model_id, re.I))


# Providers whose keys come with a free plan: models they list cost nothing within the limits.
FREE_PLAN_PROVIDERS = frozenset({"groq", "cerebras", "google", "nvidia", "mistral"})

_cache: dict[str, tuple[float, list[dict]]] = {}
_lock = threading.Lock()


def auto_chain(main_provider: str, main_model: str) -> list[dict]:
    """The automatic order: your model, other paid models, free models best first, then local.

    Each entry: provider, model, label, free (bool), why ("your model", "paid", "free", "local").
    Reads the model lists of every provider you have a key for (cached for half an hour).
    """
    from vesper import providers as prov

    key = f"{main_provider}|{main_model}"
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CHAIN_CACHE_S:
            return [dict(e) for e in hit[1]]
    ready = [p for p in prov.PROVIDERS.values() if p.id != "custom" and not p.local and prov._key(p)]
    custom = prov.PROVIDERS["custom"]
    if prov._base_url(custom) and prov._key(custom):
        ready.append(custom)

    def catalog(p):
        try:
            return p, prov.model_catalog(p.id, timeout=6)["models"]
        except Exception:  # noqa: BLE001 - a provider that cannot be reached is skipped
            return p, []

    with ThreadPoolExecutor(max_workers=8) as pool:
        lists = list(pool.map(catalog, ready))

    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(pid: str, model: str, free: bool, why: str) -> None:
        if (pid, model) in seen or not model:
            return
        seen.add((pid, model))
        entries.append({"provider": pid, "model": model, "label": prov.PROVIDERS[pid].label, "free": free, "why": why})

    if main_provider in prov.PROVIDERS and main_model:
        add(main_provider, main_model, False, "your model")
    paid, free = [], []
    for p, models in lists:
        usable = [m for m in models if m.get("tools") is not False and not _skip(m["id"])]
        on_free_plan = p.id in FREE_PLAN_PROVIDERS
        for m in usable:
            if m.get("free") or (on_free_plan and m.get("free") is None and rank(m["id"]) < len(FAMILY_RANK)):
                free.append((p.id, m["id"]))
        paids = [m for m in usable if m.get("free") is False or (m.get("free") is None and not on_free_plan)]
        if paids:  # one model per paid key: its strongest known one
            best = min(paids, key=lambda m: (rank(m["id"]), _variant_penalty(m["id"]), m["id"]))
            if rank(best["id"]) < len(FAMILY_RANK):
                paid.append((p.id, best["id"]))
    for pid, model in sorted(paid, key=lambda e: (rank(e[1]), _variant_penalty(e[1]))):
        add(pid, model, False, "paid")
    for pid, model in sorted(free, key=lambda e: (rank(e[1]), _variant_penalty(e[1]), e[1])):
        add(pid, model, True, "free")
    if onboarding.ollama_running():
        add("ollama", prov.PROVIDERS["ollama"].chat_model, True, "local")
    entries = entries[:MAX_CHAIN]
    with _lock:
        _cache[key] = (time.time(), [dict(e) for e in entries])
    return entries


def chain_for(main_provider: str, main_model: str) -> list[dict]:
    """The order in use: the saved one, or the automatic one."""
    from vesper import providers as prov

    saved = settings()["chain"]
    if saved:
        return [{**c, "label": prov.PROVIDERS[c["provider"]].label, "free": None, "why": "your order"}
                for c in saved if c["provider"] in prov.PROVIDERS]
    return auto_chain(main_provider, main_model)


class Switcher:
    """The fallback order for one conversation, with the models that failed and until when."""

    def __init__(self, chain: list[dict] | Callable[[], list[dict]]):
        # The order can be a function: reading every provider's model list takes a few seconds,
        # so it happens only when a model first fails, not before every answer.
        self._chain = chain
        self.failed: dict[tuple[str, str], float] = {}

    @property
    def chain(self) -> list[dict]:
        if callable(self._chain):
            self._chain = self._chain()
        return self._chain

    def mark(self, provider: str, model: str, reason: str) -> None:
        wait = RATE_COOLDOWN_S if reason == "rate" else CREDIT_COOLDOWN_S
        self.failed[(provider, model)] = time.time() + wait

    def usable(self, entry: dict) -> bool:
        return self.failed.get((entry["provider"], entry["model"]), 0) <= time.time()

    def next_after(self, provider: str, model: str) -> dict | None:
        """The first usable model after (provider, model) in the order, wrapping around."""
        start = next((i + 1 for i, e in enumerate(self.chain) if (e["provider"], e["model"]) == (provider, model)), 0)
        for e in self.chain[start:] + self.chain[:start]:
            if (e["provider"], e["model"]) != (provider, model) and self.usable(e):
                return e
        return None

    def preferred(self) -> dict | None:
        """The best model to start a turn with: the first one not waiting out a failure.
        None while nothing has failed (the current model stays)."""
        if not self.failed:
            return None
        return next((e for e in self.chain if self.usable(e)), None)
