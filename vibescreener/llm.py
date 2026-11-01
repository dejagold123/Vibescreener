"""Small chat client for any OpenAI-compatible API (OpenRouter's free models by default).

Models are tried in order: a rate limit, outage or unknown model moves on to the next one.
A rejected key stops immediately, since retrying cannot help and burns quota.
The key is only ever sent as a header, so it cannot appear in a URL or a log line."""
import re
import time

import requests

from . import config as cfg


class LLMError(Exception):
    pass


class LLMAuthError(LLMError):
    pass


class LLMBusy(LLMError):
    """Every model is rate-limited right now (or cooling down after a limit)."""


class LLMRejected(LLMError):
    """Models answered, but every answer failed the caller's check."""


def enabled(env=None):
    return bool(cfg.llm_settings(env)["key"])


def _clean_reply(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    return text.strip()


_found = {"at": 0.0, "ids": []}
_cool = {}                       # model -> time when it may be tried again after a rate limit


def _retry_after(r):
    try:
        v = float((getattr(r, "headers", None) or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        v = 60.0
    return min(300.0, max(10.0, v))


def discover_free(st, session, tried, now=None):
    """OpenRouter rotates its free models. When every configured one fails, ask which free ones exist now."""
    now = time.time() if now is None else now
    if now - _found["at"] > 3600 or not _found["ids"]:
        try:
            r = session.get(st["base_url"] + "/models", headers={"Authorization": "Bearer " + st["key"]}, timeout=20)
            ids = [m.get("id", "") for m in (r.json().get("data") or []) if isinstance(m, dict)] if r.status_code == 200 else []
        except (requests.RequestException, ValueError, AttributeError):
            return []
        _found.update(at=now, ids=[i for i in ids if i.endswith(":free") and not re.search(r"vision|image|embed|guard|r1|reason|think|nemotron", i)])
    return [i for i in _found["ids"] if i not in tried][:4]


def chat(messages, max_tokens=400, temperature=0.2, env=None, session=None, timeout=45, accept=None,
         overall_timeout=60):
    """Return (text, model). Raises LLMError when no model could answer.
    `accept(text) -> bool` lets the caller refuse an answer; the next model is then tried.

    M10: `overall_timeout` (default 60s) caps the whole multi-model attempt:
    each request uses min(timeout, time remaining), and once the deadline
    passes no further model (including discovered free models) is tried.
    Previously 2 configured + up to 4 discovered models at 45s each could
    hold one Ask request for ~270s.
    """
    st = cfg.llm_settings(env)
    if not st["key"]:
        raise LLMError("no API key configured")
    session = session or requests
    headers = {"Authorization": "Bearer " + st["key"], "Content-Type": "application/json",
               "X-Title": "VibeScreener"}
    deadline = time.time() + overall_timeout if overall_timeout else None
    last = "no model answered"
    models, tried, pass_no, rejected, limited, asked = list(st["models"]), set(), 0, None, False, 0
    while models:
        for model in models:
            if deadline is not None and time.time() >= deadline:
                last = "overall LLM deadline reached"
                break
            tried.add(model)
            if _cool.get(model, 0) > time.time():
                limited = True
                last = f"{model}: cooling down after a rate limit"
                continue
            asked += 1
            per_call = timeout
            if deadline is not None:
                per_call = max(1, min(timeout, deadline - time.time()))
            try:
                r = session.post(st["base_url"] + "/chat/completions", headers=headers, timeout=per_call,
                                 json={"model": model, "messages": messages, "max_tokens": max_tokens,
                                       "temperature": temperature})
            except requests.RequestException as e:
                last = f"{model}: {type(e).__name__}"
                continue
            if r.status_code in (401, 403):
                raise LLMAuthError("the AI provider rejected the API key")
            if r.status_code == 429:
                _cool[model] = time.time() + _retry_after(r)
                limited = True
                last = f"{model}: rate limited"
                continue
            if r.status_code != 200:
                last = f"{model}: http {r.status_code}"
                continue
            try:
                text = _clean_reply(r.json()["choices"][0]["message"]["content"])
            except (ValueError, KeyError, IndexError, TypeError):
                last = f"{model}: unreadable reply"
                continue
            if text and (accept is None or accept(text)):
                return text, model
            if text:
                rejected = text
                last = f"{model}: answer refused by the check"
                continue
            last = f"{model}: empty reply"
        models = []
        if (deadline is None or time.time() < deadline) and (pass_no == 0 and not limited
                and st["base_url"].startswith("https://openrouter.ai")
                and hasattr(session, "get")):
            models = discover_free(st, session, tried)
        pass_no += 1
    if rejected is not None:
        e = LLMRejected(last)
        e.text = rejected
        raise e
    if limited:
        raise LLMBusy(last)
    raise LLMError(last)
