"""Small chat client for any OpenAI-compatible API (OpenRouter's free models by default).

Models are tried in order: a rate limit, outage or unknown model moves on to the next one.
A rejected key stops immediately, since retrying cannot help and burns quota.
The key is only ever sent as a header, so it cannot appear in a URL or a log line."""
import re

import requests

from . import config as cfg


class LLMError(Exception):
    pass


class LLMAuthError(LLMError):
    pass


def enabled(env=None):
    return bool(cfg.llm_settings(env)["key"])


def _clean_reply(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    return text.strip()


def chat(messages, max_tokens=400, temperature=0.2, env=None, session=None, timeout=45):
    """Return (text, model). Raises LLMError when no model could answer."""
    st = cfg.llm_settings(env)
    if not st["key"]:
        raise LLMError("no API key configured")
    session = session or requests
    headers = {"Authorization": "Bearer " + st["key"], "Content-Type": "application/json",
               "X-Title": "VibeScreener"}
    last = "no model answered"
    for model in st["models"]:
        try:
            r = session.post(st["base_url"] + "/chat/completions", headers=headers, timeout=timeout,
                             json={"model": model, "messages": messages, "max_tokens": max_tokens,
                                   "temperature": temperature})
        except requests.RequestException as e:
            last = f"{model}: {type(e).__name__}"
            continue
        if r.status_code in (401, 403):
            raise LLMAuthError("the AI provider rejected the API key")
        if r.status_code != 200:
            last = f"{model}: http {r.status_code}"
            continue
        try:
            text = _clean_reply(r.json()["choices"][0]["message"]["content"])
        except (ValueError, KeyError, IndexError, TypeError):
            last = f"{model}: unreadable reply"
            continue
        if text:
            return text, model
        last = f"{model}: empty reply"
    raise LLMError(last)
