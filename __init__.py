"""New API image generation backend for Hermes Agent.

A fork of the built-in ``openai-codex`` plugin that routes
``image_generation`` tool calls through an OpenAI-compatible gateway
(New API, OneAPI, FastGPT, LiteLLM, etc.) instead of ChatGPT's direct
Codex backend.

Use case: you pay for an OpenAI Codex subscription (ChatGPT Plus/Pro
with Codex access) but proxy it through a gateway that wraps the Codex
upstream as an OpenAI-compatible endpoint, and you want Hermes to drive
``gpt-image-2`` through that gateway — without holding a separate
``OPENAI_API_KEY``.

Selection precedence for the quality tier (first hit wins):

1. ``OPENAI_IMAGE_MODEL`` env var (escape hatch for scripts / tests)
2. ``image_gen.new-api-image.model`` in ``config.yaml``
3. ``image_gen.model`` in ``config.yaml`` (when it's one of our tier IDs)
4. :data:`DEFAULT_MODEL` — ``gpt-image-2-medium``

Output is saved as PNG under ``$HERMES_HOME/cache/images/``.

Required env: ``NEW_API_KEY``, ``NEW_API_BASE_URL``
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)

logger = logging.getLogger(__name__)


API_MODEL = "gpt-image-2"

_MODELS: Dict[str, Dict[str, Any]] = {
    "gpt-image-2-low": {
        "display": "GPT Image 2 (Low)",
        "speed": "~15s",
        "strengths": "Fast iteration, lowest cost",
        "quality": "low",
    },
    "gpt-image-2-medium": {
        "display": "GPT Image 2 (Medium)",
        "speed": "~40s",
        "strengths": "Balanced — default",
        "quality": "medium",
    },
    "gpt-image-2-high": {
        "display": "GPT Image 2 (High)",
        "speed": "~2min",
        "strengths": "Highest fidelity, strongest prompt adherence",
        "quality": "high",
    },
}

DEFAULT_MODEL = "gpt-image-2-medium"

_SIZES = {
    "landscape": "1536x1024",
    "square": "1024x1024",
    "portrait": "1024x1536",
}

# Chat model that hosts the image_generation tool call. Codex upstream
# accepts gpt-5.4 here; gpt-5.x variants generally work too.
_CHAT_MODEL = "gpt-5.4"

_INSTRUCTIONS = (
    "You are an assistant that must fulfill image generation requests by "
    "using the image_generation tool when provided."
)

PROVIDER_NAME = "new-api-image"


def _resolve_model() -> Tuple[str, Dict[str, Any]]:
    """Pick a tier id + metadata using env → plugin config → global → default."""
    candidates: List[str] = []

    env_choice = (os.environ.get("OPENAI_IMAGE_MODEL") or "").strip()
    if env_choice:
        candidates.append(env_choice)

    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        ig = cfg.get("image_gen") if isinstance(cfg, dict) else None
        if isinstance(ig, dict):
            sub = ig.get(PROVIDER_NAME)
            if isinstance(sub, dict):
                v = (sub.get("model") or "").strip()
                if v:
                    candidates.append(v)
            v = (ig.get("model") or "").strip()
            if v:
                candidates.append(v)
    except Exception:
        pass

    candidates.append(DEFAULT_MODEL)

    for c in candidates:
        if c in _MODELS:
            return c, _MODELS[c]
    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


def _read_api_key() -> Optional[str]:
    key = (os.environ.get("NEW_API_KEY") or "").strip()
    return key or None


def _read_base_url() -> Optional[str]:
    url = (os.environ.get("NEW_API_BASE_URL") or "").strip()
    return url or None


def _collect_image_b64(*, prompt: str, size: str, quality: str) -> Optional[str]:
    """Stream a Responses image_generation call and return the b64 image.

    Uses raw httpx instead of the openai SDK's ``stream()`` helper because
    some New-API-style gateways return a non-RFC-compliant final chunk
    footer (``0\\r`` instead of ``0\\r\\n\\r\\n``). httpx raises
    ``RemoteProtocolError`` on that, but by then the image data has
    already been delivered in the
    ``response.image_generation_call.partial_image`` event — so we just
    need a parser that tolerates the malformed trailer.
    """
    api_key = _read_api_key()
    if not api_key:
        raise RuntimeError("NEW_API_KEY not set")
    base_url = _read_base_url()
    if not base_url:
        raise RuntimeError("NEW_API_BASE_URL not set")

    url = base_url.rstrip("/") + "/responses"
    payload = {
        "model": _CHAT_MODEL,
        "store": False,
        "stream": True,
        "instructions": _INSTRUCTIONS,
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": prompt}],
        }],
        "tools": [{
            "type": "image_generation",
            "model": API_MODEL,
            "size": size,
            "quality": quality,
            "output_format": "png",
            "background": "opaque",
            "partial_images": 1,
        }],
        "tool_choice": {
            "type": "allowed_tools",
            "mode": "required",
            "tools": [{"type": "image_generation"}],
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    image_b64: Optional[str] = None
    buffer = b""

    def _consume_events():
        nonlocal buffer, image_b64
        while b"\n\n" in buffer:
            event_block, buffer = buffer.split(b"\n\n", 1)
            data_line = None
            for line in event_block.split(b"\n"):
                if line.startswith(b"data: "):
                    data_line = line[6:]
                    break
            if not data_line:
                continue
            try:
                d = json.loads(data_line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if t == "response.image_generation_call.partial_image":
                partial = d.get("partial_image_b64")
                if isinstance(partial, str) and partial:
                    image_b64 = partial
            elif t == "response.output_item.done":
                item = d.get("item") or {}
                if item.get("type") == "image_generation_call":
                    result = item.get("result")
                    if isinstance(result, str) and result:
                        image_b64 = result
            elif t == "response.completed":
                for item in (d.get("response") or {}).get("output") or []:
                    if item.get("type") == "image_generation_call":
                        result = item.get("result")
                        if isinstance(result, str) and result:
                            image_b64 = result

    try:
        with httpx.stream(
            "POST", url, json=payload, headers=headers, timeout=180.0
        ) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes():
                buffer += chunk
                _consume_events()
    except httpx.RemoteProtocolError as exc:
        # Upstream gateway sends malformed chunked trailer. The image
        # bytes are delivered earlier in the stream, so swallow this
        # only if we already got the result.
        _consume_events()
        if not image_b64:
            raise RuntimeError(
                f"Stream parse error before image was received: {exc}"
            ) from exc
        logger.debug(
            "Tolerated upstream RemoteProtocolError after image received: %s",
            exc,
        )

    # Final sweep in case the last event arrived without a trailing \n\n
    if buffer.startswith(b"data: "):
        try:
            d = json.loads(buffer[6:])
            t = d.get("type")
            if t == "response.image_generation_call.partial_image":
                partial = d.get("partial_image_b64")
                if isinstance(partial, str) and partial:
                    image_b64 = partial
        except Exception:
            pass

    return image_b64


class NewApiImageGenProvider(ImageGenProvider):
    """gpt-image-2 via a New API-style gateway-wrapped Codex channel."""

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "New API (gpt-image-2 via codex channel)"

    def is_available(self) -> bool:
        return bool(_read_api_key()) and bool(_read_base_url())

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": "via your gateway",
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "New API (codex channel)",
            "badge": "gateway",
            "tag": (
                "gpt-image-2 via an OpenAI-compatible gateway's codex channel"
            ),
            "env_vars": [
                {
                    "key": "NEW_API_KEY",
                    "prompt": (
                        "Gateway token (the codex channel must have image "
                        "capability enabled)"
                    ),
                },
                {
                    "key": "NEW_API_BASE_URL",
                    "prompt": (
                        "Gateway base URL ending in /v1 "
                        "(e.g. https://your-gateway.example.com/v1)"
                    ),
                },
            ],
            "post_setup_hint": (
                "Both NEW_API_KEY and NEW_API_BASE_URL must be set in "
                "~/.hermes/.env."
            ),
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider=PROVIDER_NAME,
                aspect_ratio=aspect,
            )

        if not _read_api_key():
            return error_response(
                error="NEW_API_KEY is not set in environment (~/.hermes/.env)",
                error_type="auth_required",
                provider=PROVIDER_NAME,
                aspect_ratio=aspect,
            )

        if not _read_base_url():
            return error_response(
                error=(
                    "NEW_API_BASE_URL is not set in environment "
                    "(~/.hermes/.env). Example: "
                    "https://your-gateway.example.com/v1"
                ),
                error_type="config_required",
                provider=PROVIDER_NAME,
                aspect_ratio=aspect,
            )

        tier_id, meta = _resolve_model()
        size = _SIZES.get(aspect, _SIZES["square"])

        try:
            b64 = _collect_image_b64(
                prompt=prompt,
                size=size,
                quality=meta["quality"],
            )
        except Exception as exc:
            logger.debug("New API image generation failed", exc_info=True)
            return error_response(
                error=f"Image generation via New API failed: {exc}",
                error_type="api_error",
                provider=PROVIDER_NAME,
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if not b64:
            return error_response(
                error="Response contained no image_generation_call result",
                error_type="empty_response",
                provider=PROVIDER_NAME,
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            saved_path = save_b64_image(b64, prefix=f"new_api_{tier_id}")
        except Exception as exc:
            return error_response(
                error=f"Could not save image to cache: {exc}",
                error_type="io_error",
                provider=PROVIDER_NAME,
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        return success_response(
            image=str(saved_path),
            model=tier_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider=PROVIDER_NAME,
            extra={"size": size, "quality": meta["quality"]},
        )


def register(ctx) -> None:
    """Plugin entry point — register the New API image-gen provider."""
    ctx.register_image_gen_provider(NewApiImageGenProvider())
