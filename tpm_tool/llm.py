"""Thin Hugging Face client. Everything degrades gracefully when no token/model is available.

Model chosen from the livebench.ai open-weights leaderboard (Data Analysis category);
see README. Override with env vars - no code change needed:

    HF_TOKEN     Hugging Face access token (read scope is enough)
    HF_MODEL     model repo id, e.g. "Qwen/Qwen3-32B"
    HF_PROVIDER  Inference Providers name, or "auto" (default)
    HF_BASE_URL  optional OpenAI-compatible endpoint (TGI / vLLM / Ollama) instead of HF-hosted
"""
from __future__ import annotations

import os
import re

DEFAULT_MODEL = os.getenv("HF_MODEL", "Qwen/Qwen3-32B")


class LLMError(RuntimeError):
    pass


def scrub(text: str) -> str:
    """Reasoning models (Qwen3, DeepSeek-R1...) emit <think>...</think>; never show that to users."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    text = re.sub(r"^.*?</think>", "", text, flags=re.S | re.I)  # unterminated opening tag
    return text.strip()


class LLM:
    def __init__(self, token: str | None = None, model: str | None = None,
                 provider: str | None = None, base_url: str | None = None):
        self.token = token or os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACEHUB_API_TOKEN")
        self.model = model or DEFAULT_MODEL
        self.provider = provider or os.getenv("HF_PROVIDER", "auto")
        self.base_url = base_url or os.getenv("HF_BASE_URL")

    @property
    def available(self) -> bool:
        return bool(self.token or self.base_url)

    def complete(self, system: str, user: str | None = None, history: list[dict] | None = None,
                 max_tokens: int = 900, temperature: float = 0.2) -> str:
        if not self.available:
            raise LLMError("No HF_TOKEN configured.")
        try:
            from huggingface_hub import InferenceClient
        except ImportError as exc:
            raise LLMError("huggingface_hub is not installed (pip install -r requirements.txt).") from exc
        msgs = [{"role": "system", "content": system}] + list(history or [])
        if user:
            msgs.append({"role": "user", "content": user})
        try:
            if self.base_url:
                client = InferenceClient(base_url=self.base_url, api_key=self.token or "none", timeout=60)
                out = client.chat_completion(messages=msgs, max_tokens=max_tokens, temperature=temperature)
            else:
                client = InferenceClient(provider=self.provider, api_key=self.token, timeout=60)
                out = client.chat_completion(model=self.model, messages=msgs,
                                             max_tokens=max_tokens, temperature=temperature)
            text = scrub(out.choices[0].message.content)
        except Exception as exc:  # network, quota, model not served, etc.
            msg = str(exc)
            if "403" in msg:
                msg = ("Token lacks permission. Create a fine-grained token with 'Make calls to Inference "
                       "Providers' enabled at huggingface.co/settings/tokens.")
            elif "401" in msg:
                msg = "Token rejected (invalid or expired)."
            raise LLMError(msg[:300]) from exc
        if not text:
            raise LLMError("Model returned an empty response.")
        return text
