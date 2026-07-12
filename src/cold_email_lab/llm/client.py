"""Phase 9: pluggable LLM provider client.

Every LLM call in the codebase (research synthesis, email generation, lead-sourcing
fit scoring) routes through `complete()` below instead of talking to a provider
SDK directly. This lets Mash switch between Anthropic (default) and NVIDIA
(build.nvidia.com, OpenAI-compatible chat completions) via one env var,
`LLM_PROVIDER`, without touching call sites.

Provider + credentials are read from the environment *inside* `complete()` on
every call (not cached at import time), so a `.env` change takes effect on the
very next call within the same process — useful for tests and for anyone
running two provider configs back to back.

Callers pass a `tool_schema` describing the JSON shape they want back (the same
shape previously passed straight to Anthropic's `tools=[...]` parameter). The
Anthropic backend still uses native tool-forcing (`tool_choice`), so its
behaviour is unchanged from pre-Phase-9. The NVIDIA backend has no equivalent
tool-forcing in this plain-httpx implementation, so the schema is instead
appended to the prompt as an explicit "respond with only this JSON shape"
instruction. Either way, callers get back an `LLMResult.text` that is a JSON
string ready for `json.loads()` — markdown fences (```json ... ```) are
stripped before it's returned, since GLM-family models on NVIDIA sometimes wrap
JSON output in them (harmless to strip for Claude too, which normally doesn't).
"""

import asyncio
import json
import os
import re
from dataclasses import dataclass

import anthropic
import httpx
from loguru import logger

_ANTHROPIC_MODEL_DEFAULT = "claude-sonnet-4-6"
_ANTHROPIC_INPUT_COST_PER_MTOK = 3.0
_ANTHROPIC_OUTPUT_COST_PER_MTOK = 15.0

_NVIDIA_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
_NVIDIA_TIMEOUT_S = 120

_OPENAI_COMPAT_PROVIDER_NAMES = ("nvidia", "openai-compat")

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)

# Phase 13: log the NVIDIA_*-fallback deprecation notice once per process, not per call.
_nvidia_fallback_logged = False


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None  # None/unknown for providers with no known pricing (nvidia)


def strip_markdown_fences(text: str) -> str:
    """Strip a ```json ... ``` or ``` ... ``` fence wrapper, if the whole string is one."""
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    if m:
        return m.group(1).strip()
    return stripped


def _get_provider() -> str:
    return (os.environ.get("LLM_PROVIDER") or "anthropic").strip().lower()


def _get_openai_compat_config() -> tuple[str, str | None, str | None]:
    """Resolve (base_url, api_key, model) for the openai-compat-style backend
    (provider value 'nvidia' or 'openai-compat' — same backend either way).

    Phase 13: `OPENAI_COMPAT_BASE_URL` / `OPENAI_COMPAT_API_KEY` / `OPENAI_COMPAT_MODEL`
    are preferred; `NVIDIA_*` is read as a fallback for any of the three that isn't
    set under the new names, with a one-time deprecation log (per process) the
    first time a fallback value is actually used.
    """
    global _nvidia_fallback_logged

    base_url = os.environ.get("OPENAI_COMPAT_BASE_URL")
    api_key = os.environ.get("OPENAI_COMPAT_API_KEY")
    model = os.environ.get("OPENAI_COMPAT_MODEL")

    used_fallback = False
    if not base_url and os.environ.get("NVIDIA_BASE_URL"):
        base_url = os.environ["NVIDIA_BASE_URL"]
        used_fallback = True
    if not api_key and os.environ.get("NVIDIA_API_KEY"):
        api_key = os.environ["NVIDIA_API_KEY"]
        used_fallback = True
    if not model and os.environ.get("NVIDIA_MODEL"):
        model = os.environ["NVIDIA_MODEL"]
        used_fallback = True

    if used_fallback and not _nvidia_fallback_logged:
        logger.warning(
            "NVIDIA_* env vars are deprecated in favour of OPENAI_COMPAT_BASE_URL / "
            "OPENAI_COMPAT_API_KEY / OPENAI_COMPAT_MODEL — please rename them in .env. "
            "NVIDIA_* will keep working as a fallback for now."
        )
        _nvidia_fallback_logged = True

    return base_url or _NVIDIA_DEFAULT_BASE_URL, api_key, model


def is_configured() -> tuple[bool, str]:
    """Check (without raising) whether the *selected* provider has its required env
    vars set. Returns (True, "") if ready, else (False, human-readable reason) — used
    by callers (e.g. Phase 8 fit scoring) that want to skip an optional LLM step
    gracefully rather than error out."""
    provider = _get_provider()
    if provider == "anthropic":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return True, ""
        return False, "ANTHROPIC_API_KEY not set"
    if provider in _OPENAI_COMPAT_PROVIDER_NAMES:
        _, api_key, model = _get_openai_compat_config()
        missing = []
        if not api_key:
            missing.append("OPENAI_COMPAT_API_KEY (or NVIDIA_API_KEY)")
        if not model:
            missing.append("OPENAI_COMPAT_MODEL (or NVIDIA_MODEL)")
        if not missing:
            return True, ""
        return False, f"{', '.join(missing)} not set"
    return False, f"unknown LLM_PROVIDER {provider!r} (expected 'anthropic', 'nvidia', or 'openai-compat')"


def require_configured() -> None:
    """Raise a clear `ValueError` up front (no network call) if the selected
    provider's required env vars are missing — same style as the SMTP/IMAP env
    guards elsewhere in the codebase. Callers that need to fail *before* any
    other side effect (e.g. `enrich_lead`, which must not create a run row if
    the LLM isn't configured) should call this first."""
    provider = _get_provider()
    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError(
                "LLM_PROVIDER is 'anthropic' (the default) but ANTHROPIC_API_KEY is not set. "
                "Add ANTHROPIC_API_KEY to your .env, or set LLM_PROVIDER=openai-compat and "
                "OPENAI_COMPAT_API_KEY/OPENAI_COMPAT_MODEL to use an OpenAI-compatible backend instead."
            )
    elif provider in _OPENAI_COMPAT_PROVIDER_NAMES:
        _, api_key, model = _get_openai_compat_config()
        missing = []
        if not api_key:
            missing.append("OPENAI_COMPAT_API_KEY (or NVIDIA_API_KEY)")
        if not model:
            missing.append("OPENAI_COMPAT_MODEL (or NVIDIA_MODEL)")
        if missing:
            raise ValueError(
                f"LLM_PROVIDER is {provider!r} but missing required env var(s): {', '.join(missing)}. "
                "Set OPENAI_COMPAT_API_KEY and OPENAI_COMPAT_MODEL (the exact model id) in your .env "
                "— NVIDIA_API_KEY/NVIDIA_MODEL still work as a deprecated fallback."
            )
    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER {provider!r} — expected 'anthropic', 'nvidia', or 'openai-compat'."
        )


async def complete(
    prompt: str,
    *,
    max_tokens: int,
    purpose: str,
    tool_schema: dict,
    model: str | None = None,
    max_retries: int = 3,
) -> LLMResult:
    """Run one structured completion against the selected provider.

    `tool_schema` is `{"name": ..., "description": ..., "input_schema": {...}}` — the
    same shape previously built for Anthropic's `tools=[...]`. `purpose` is a short
    label used in logs (e.g. "synthesis", "email_generation", "fit_score").
    `model` is an optional Anthropic model override (e.g. the cheaper Haiku model
    for fit scoring); it is ignored by the nvidia backend, which always uses
    `NVIDIA_MODEL`.

    Raises `ValueError` up front (no network call) if the selected provider's
    required env vars are missing, naming them explicitly.
    """
    require_configured()
    provider = _get_provider()
    if provider == "anthropic":
        return await _complete_anthropic(
            prompt,
            max_tokens=max_tokens,
            purpose=purpose,
            tool_schema=tool_schema,
            model=model,
            max_retries=max_retries,
        )
    if provider in _OPENAI_COMPAT_PROVIDER_NAMES:
        return await _complete_openai_compat(
            prompt,
            max_tokens=max_tokens,
            purpose=purpose,
            tool_schema=tool_schema,
            max_retries=max_retries,
        )
    raise ValueError(
        f"Unknown LLM_PROVIDER {provider!r} — expected 'anthropic', 'nvidia', or 'openai-compat'."
    )


async def _complete_anthropic(
    prompt: str,
    *,
    max_tokens: int,
    purpose: str,
    tool_schema: dict,
    model: str | None,
    max_retries: int,
) -> LLMResult:
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    use_model = model or _ANTHROPIC_MODEL_DEFAULT

    last_exc: Exception = RuntimeError("unreachable")
    for attempt in range(max_retries):
        try:
            response = await client.messages.create(
                model=use_model,
                max_tokens=max_tokens,
                tools=[tool_schema],
                tool_choice={"type": "tool", "name": tool_schema["name"]},
                messages=[{"role": "user", "content": prompt}],
            )
            block = next((b for b in response.content if b.type == "tool_use"), None)
            if block is None:
                raise RuntimeError("No tool_use block in response")

            in_tok = response.usage.input_tokens
            out_tok = response.usage.output_tokens
            cost = (
                in_tok * _ANTHROPIC_INPUT_COST_PER_MTOK
                + out_tok * _ANTHROPIC_OUTPUT_COST_PER_MTOK
            ) / 1_000_000
            logger.info(
                f"[{purpose}] anthropic/{use_model} — {in_tok} in / {out_tok} out tokens, ${cost:.4f} USD"
            )
            return LLMResult(
                text=json.dumps(block.input),
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
            )
        except Exception as exc:
            last_exc = exc
            logger.warning(f"[{purpose}] anthropic attempt {attempt + 1}/{max_retries} failed: {exc}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2**attempt)
    raise RuntimeError(f"All {max_retries} {purpose} attempts failed (anthropic)") from last_exc


async def _complete_openai_compat(
    prompt: str,
    *,
    max_tokens: int,
    purpose: str,
    tool_schema: dict,
    max_retries: int,
) -> LLMResult:
    base_url, api_key, compat_model = _get_openai_compat_config()
    base_url = base_url.rstrip("/")
    chat_url = f"{base_url}/chat/completions"

    schema_hint = json.dumps(tool_schema.get("input_schema", {}), indent=2)
    json_prompt = (
        f"{prompt}\n\n---\n"
        "Respond with ONLY a single JSON object — no markdown code fences, no "
        "commentary, no extra text before or after — matching exactly this JSON "
        f"schema:\n{schema_hint}"
    )

    # Non-streaming — some NVIDIA-hosted models (e.g. GLM reasoning models) put
    # chain-of-thought in a separate `reasoning`/`reasoning_content` field on the
    # message; the JSON answer we want is in `message.content`.
    payload = {
        "model": compat_model,
        "messages": [{"role": "user", "content": json_prompt}],
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last_exc: Exception = RuntimeError("unreachable")
    async with httpx.AsyncClient(timeout=_NVIDIA_TIMEOUT_S) as client:
        for attempt in range(max_retries):
            try:
                resp = await client.post(chat_url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                message = data["choices"][0]["message"]
                content = message.get("content") or ""
                if not content.strip():
                    raise RuntimeError(
                        f"Empty message.content in openai-compat response (keys: {list(message.keys())})"
                    )
                text = strip_markdown_fences(content)
                usage = data.get("usage") or {}
                in_tok = int(usage.get("prompt_tokens", 0))
                out_tok = int(usage.get("completion_tokens", 0))
                logger.info(
                    f"[{purpose}] openai-compat/{compat_model} — {in_tok} in / {out_tok} out tokens (cost unknown)"
                )
                return LLMResult(text=text, input_tokens=in_tok, output_tokens=out_tok, cost_usd=None)
            except Exception as exc:
                last_exc = exc
                logger.warning(f"[{purpose}] openai-compat attempt {attempt + 1}/{max_retries} failed: {exc}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2**attempt)
    raise RuntimeError(f"All {max_retries} {purpose} attempts failed (openai-compat)") from last_exc
