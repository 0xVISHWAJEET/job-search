"""
Minimal, dependency-free Gemini text-generation client (stdlib urllib only -
no venv/pip available in this environment, and this project's scripts don't
need anything heavier than one text-in/text-out call). Reads GOOGLE_API_KEY
from the environment - export it, or run scripts with:
  GOOGLE_API_KEY=$(grep GOOGLE_API_KEY ../VishuGPT/.env | cut -d= -f2) python3 script.py
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

# Each of these has its own independent free-tier daily quota (confirmed by
# probing: gemini-3.5-flash-lite capped at 500 req/day, gemini-3.8-flash
# (what the "gemini-flash-latest" alias happened to route to) capped at
# just 20/day) - "latest" aliases route unpredictably to whichever backing
# model Google's load balancer picks, so this chain uses concrete model
# names instead, tried in order, falling through to the next one on
# RESOURCE_EXHAUSTED rather than waiting out a quota that won't reset for
# hours. This is the free-tier-only strategy - enabling billing on the
# project behind either key would remove all of this, but the user
# explicitly chose to keep using the free tier.
MODEL_CHAIN = [
    os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.7-flash",
]

# Per-model retries, for transient (503/short-window 429) failures only -
# quota exhaustion (429 with a long/day-scale "retry in") moves to the next
# model in MODEL_CHAIN instead of waiting here.
MAX_RETRIES_PER_MODEL = 2


class GeminiError(RuntimeError):
    pass


def _call_model(model: str, api_key: str, payload: bytes) -> dict:
    url = API_URL_TEMPLATE.format(model=model, key=api_key)
    last_error = None

    for attempt in range(MAX_RETRIES_PER_MODEL):
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            last_error = GeminiError(f"Gemini API error {exc.code} ({model}): {body[:500]}")

            if exc.code == 429:
                raise last_error from exc  # quota exhausted for this model - caller moves to the next one
            if exc.code != 503 or attempt == MAX_RETRIES_PER_MODEL - 1:
                raise last_error from exc

            wait_match = re.search(r"retry in ([\d.]+)s", body)
            wait_seconds = float(wait_match.group(1)) + 1 if wait_match else (5 * (attempt + 1))
            time.sleep(wait_seconds)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # Network-level failure (DNS, connection reset, read timeout) -
            # not an HTTP response at all, so it has no status code to branch
            # on. Treat like a transient 503: retry within this model, then
            # let the caller fall through to the next one in MODEL_CHAIN.
            last_error = GeminiError(f"Network error calling Gemini ({model}): {exc}")
            if attempt == MAX_RETRIES_PER_MODEL - 1:
                raise last_error from exc
            time.sleep(5 * (attempt + 1))

    raise last_error


def generate(prompt: str, model: str | None = None) -> str:
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise GeminiError("GOOGLE_API_KEY is not set in the environment.")

    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    chain = [model] if model else MODEL_CHAIN

    last_error = None
    for candidate_model in chain:
        try:
            data = _call_model(candidate_model, api_key, payload)
            break
        except GeminiError as exc:
            last_error = exc
            continue
    else:
        raise last_error

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise GeminiError(f"Unexpected Gemini response shape: {data}") from exc
