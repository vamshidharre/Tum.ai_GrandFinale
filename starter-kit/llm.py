"""The agents' model client: one JSON chat call to the hosted Qwen, built to survive a busy run.

Every decision the agents take goes through `chat_json`: JSON mode, thinking on or off per call, a cap on how many
calls run at once (the hosted model serves 32 per team across all its runs), retries with backoff and a tolerant
parse of the reply. It never raises: when the last attempt fails it returns {} and the caller decides how to go on
without an answer. `STATS` adds up calls, tokens and seconds; `summary` is the line a run prints at its end.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
import time

from openai import APIStatusError, AsyncOpenAI

MODEL = "qwen"
MAX_CONCURRENT = 16  # the team's limit is 32 across all its runs, and test runs may overlap
ATTEMPTS = 5
CALL_TIMEOUT = 150  # seconds: a model that scaled to zero is slow to answer its first call
WARM_TIMEOUT = 120  # a quick call (thinking off) this late once the model is awake is stuck: ask again. 75 s cut off
# a first turn with a large prompt under load (submit agent, sandbox test run 54c78c38: 85 s lost to the retry).
# Calls with thinking on (fact extraction) keep CALL_TIMEOUT: under load they legitimately take longer.
MAX_TOKENS_CEILING = 16000  # a reply cut off by its token limit is asked again with twice the room, up to this
PERMANENT = (400, 401, 403, 404, 413, 422)  # asking again rarely helps: one more try, then give up
# The hosted Qwen's simulated prices in USD per million tokens, for the estimate in `summary`.
PRICE_INPUT, PRICE_CACHED, PRICE_OUTPUT = 0.15, 0.016, 0.47
REMINDER = "That reply was not a single JSON object. Answer again with exactly one JSON object and nothing else."

STATS = {"calls": 0, "failed": 0, "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0, "seconds": 0.0}
BY_LABEL: dict[str, list] = {}  # caller kind -> [calls, seconds, prompt tokens]: where a run spends time and money
_warm = {"done": False}

_bound: dict = {}  # the client and the gate belong to one event loop; another loop (tests) gets its own


def _load_local_env() -> None:
    """Local development only: KEY=VALUE lines from the repo root's .env.local (never shipped: it sits outside the
    package folder). Values already in the environment win. Nothing is printed."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.local")
    if not os.path.isfile(path):
        return
    for line in open(path, encoding="utf-8"):
        name, sep, value = line.strip().partition("=")
        if sep and name and not name.startswith("#"):
            os.environ.setdefault(name.strip(), value.strip().strip('"'))


_load_local_env()

# Fact extraction can go to OpenAI instead of the hosted Qwen: EXTRACT_PROVIDER=openai and OPENAI_MODEL=<model>.
# In a run, OPENAI_BASE_URL points at the metering proxy and the key is the team secret OPENAI_API_KEY.
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")


def _session(provider: str = "qwen") -> tuple[AsyncOpenAI, asyncio.Semaphore]:
    loop = asyncio.get_running_loop()
    if _bound.get("loop") is not loop:
        _bound.clear()
        _bound["loop"] = loop
    if provider not in _bound:
        if provider == "openai":
            client = AsyncOpenAI(base_url=os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
                                 api_key=os.environ["OPENAI_API_KEY"], timeout=CALL_TIMEOUT, max_retries=0)
        else:
            client = AsyncOpenAI(base_url=os.environ["QWEN_BASE_URL"], api_key=os.environ["QWEN_API_KEY"],
                                 timeout=CALL_TIMEOUT, max_retries=0)  # the retries are ours, so that they are counted
        _bound[provider] = (client, asyncio.Semaphore(MAX_CONCURRENT))
    return _bound[provider]


def parse_json_object(text: str | None) -> dict | None:
    """The JSON object in a model's reply, or None: a thinking block, code fences and text around it are ignored."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    if not text:
        return None
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate, strict=False)  # models put raw line breaks into strings
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    if start >= 0:  # an object followed by text that has braces of its own
        try:
            parsed, _ = json.JSONDecoder(strict=False).raw_decode(text[start:])
        except ValueError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _count(usage, provider: str = "qwen") -> None:
    if usage is None:
        return
    if provider != "qwen":  # priced differently: kept apart from the Qwen estimate
        STATS[f"{provider}_tokens"] = STATS.get(f"{provider}_tokens", 0) + (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)
        return
    STATS["prompt_tokens"] += usage.prompt_tokens or 0
    STATS["completion_tokens"] += usage.completion_tokens or 0
    STATS["cached_tokens"] += getattr(usage.prompt_tokens_details, "cached_tokens", None) or 0


async def chat_json(messages: list[dict], *, think: bool = True, max_tokens: int = 6000, label: str = "",
                    provider: str = "qwen") -> dict:
    """One chat call that answers with a JSON object. {} when every attempt failed; it never raises.

    `think` turns the model's reasoning on or off: off is about twice as fast, for the easy steps. `label` names the
    caller in the failure lines on stderr.
    """
    try:
        client, gate = _session(provider)
    except KeyError as missing:
        STATS["failed"] += 1
        print(f"[llm] {label or 'call'}: {missing} is not set, no model to ask", file=sys.stderr)
        return {}
    conversation = messages
    for attempt in range(1, ATTEMPTS + 1):
        permanent = False
        try:
            async with gate:
                STATS["calls"] += 1
                started = time.monotonic()
                try:
                    if provider == "openai":
                        request = client.chat.completions.create(
                            model=OPENAI_MODEL, messages=conversation, max_completion_tokens=max_tokens,
                            response_format={"type": "json_object"})
                    else:
                        request = client.chat.completions.create(
                            model=MODEL, messages=conversation, temperature=0, max_tokens=max_tokens,
                            response_format={"type": "json_object"},
                            extra_body={"chat_template_kwargs": {"enable_thinking": think}})
                    reply = await asyncio.wait_for(request,
                        WARM_TIMEOUT if _warm["done"] and not think else CALL_TIMEOUT)
                    _warm["done"] = True
                finally:
                    spent = time.monotonic() - started
                    STATS["seconds"] += spent
                    kind = BY_LABEL.setdefault(label.split(":")[0] if label.startswith("extract") else (label or "call"), [0, 0.0, 0])
                    kind[0] += 1
                    kind[1] += spent
            _count(reply.usage, provider)
            kind[2] += getattr(reply.usage, "prompt_tokens", 0) or 0
            choice = reply.choices[0]
            content = choice.message.content or ""
            parsed = parse_json_object(content)
            if parsed is not None:
                return parsed
            if choice.finish_reason == "length":
                problem = f"the reply was cut off at {max_tokens} tokens"
                if max_tokens >= MAX_TOKENS_CEILING:
                    think = False
                max_tokens = min(max_tokens * 2, MAX_TOKENS_CEILING)
            else:
                problem = f"the reply is not a JSON object: {content[:120]!r}"
                conversation = [*messages, {"role": "assistant", "content": content[:2000]},
                                {"role": "user", "content": REMINDER}]
        except asyncio.TimeoutError:
            problem = f"no reply within {WARM_TIMEOUT if _warm['done'] and not think else CALL_TIMEOUT} s"
        except APIStatusError as error:
            problem = f"HTTP {error.status_code}: {str(error)[:200]}"
            permanent = error.status_code in PERMANENT
            if error.status_code == 401 and "finished or expired" in str(error).lower():
                _warm["expired"] = True
        except Exception as error:  # the connection, the proxy, a reply without choices
            problem = f"{type(error).__name__}: {str(error)[:200]}"
        STATS["failed"] += 1
        last = attempt == ATTEMPTS or bool(_warm.get("expired")) or (permanent and attempt >= 2)
        print(f"[llm] {label or 'call'}: attempt {attempt} failed, {problem}" + ("; giving up" if last else ""),
              file=sys.stderr)
        if last:
            break
        await asyncio.sleep(min(2 ** attempt, 20) * random.uniform(0.5, 1))
    return {}


def summary() -> str:
    """The run's model use in one line, with what it costs at the hosted model's simulated prices."""
    fresh = STATS["prompt_tokens"] - STATS["cached_tokens"]
    cost = (fresh * PRICE_INPUT + STATS["cached_tokens"] * PRICE_CACHED
            + STATS["completion_tokens"] * PRICE_OUTPUT) / 1_000_000
    return (f"LLM: {STATS['calls']} calls ({STATS['failed']} failed), {STATS['prompt_tokens']} prompt tokens "
            f"({STATS['cached_tokens']} cached), {STATS['completion_tokens']} completion tokens, "
            f"{STATS['seconds']:.0f} s in calls, about ${cost:.4f}; by caller: "
            + ", ".join(f"{name} {calls}x/{seconds:.0f}s/{tokens // 1000}k in" for name, (calls, seconds, tokens) in sorted(BY_LABEL.items()))
            + (f"; openai tokens {STATS['openai_tokens']}" if STATS.get("openai_tokens") else ""))
