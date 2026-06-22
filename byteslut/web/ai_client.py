"""
web/ai_client.py — Multi-provider AI client
=============================================
Routes AI requests to the correct provider based on user settings.

Supported providers:
  claude   → api.anthropic.com   (model: claude-sonnet-4-20250514)
  gpt      → api.openai.com      (model: gpt-4o)
  gemini   → generativelanguage.googleapis.com (model: gemini-1.5-pro)
  ollama   → localhost:11434     (local, no key needed)
  custom   → user-specified URL  (OpenAI-compatible format)

ALL calls are made SERVER-SIDE (from Flask, not the browser).
Browsers cannot call api.anthropic.com directly — CORS blocks it.

Usage:
    from web.ai_client import call_ai
    result = call_ai(prompt="...", config=load_config())
    if result["ok"]:
        print(result["text"])
    else:
        print(result["error"])
"""

import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)


def call_ai(prompt: str, config: dict) -> dict:
    """
    Send a prompt to whichever AI provider the user has configured.

    Args:
        prompt: The user message to send.
        config: The full settings dict (from load_config()).

    Returns:
        {"ok": True,  "text": "response text"}
      or
        {"ok": False, "error": "human-readable error message"}
    """
    ai_cfg  = config.get("ai_coach", {})
    model   = ai_cfg.get("ai_model", "claude").lower().strip()
    api_key = config.get("anthropic_api_key", "").strip()
    custom_url = ai_cfg.get("custom_api_url", "").strip()

    if not prompt:
        return {"ok": False, "error": "Empty prompt"}

    # Dispatch to the correct provider
    if model == "claude":
        return _call_anthropic(prompt, api_key)
    elif model == "gpt":
        return _call_openai(prompt, api_key)
    elif model == "gemini":
        return _call_gemini(prompt, api_key)
    elif model == "ollama":
        return _call_ollama(prompt, custom_url or "http://localhost:11434")
    elif model == "custom":
        if not custom_url:
            return {"ok": False,
                    "error": "Custom endpoint URL not set. Go to Settings → AI Coach."}
        return _call_custom(prompt, api_key, custom_url)
    else:
        return {"ok": False, "error": f"Unknown AI model '{model}'"}


# ─────────────────────────────────────────────────────────────────────────────
# Provider implementations
# ─────────────────────────────────────────────────────────────────────────────

def _http_post(url: str, payload: dict, headers: dict, timeout: int = 30) -> dict:
    """
    Shared HTTP POST helper. Returns parsed JSON response dict.
    Raises urllib.error.HTTPError on non-2xx responses.
    """
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wrap_error(e: Exception, provider: str) -> dict:
    """Convert an exception into a user-friendly error dict."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body     = e.read().decode("utf-8", errors="replace")
            err_data = json.loads(body)
            # Different providers use different error shapes
            msg = (err_data.get("error", {}).get("message")         # OpenAI / Anthropic
                   or err_data.get("error", {}).get("status")       # Google
                   or err_data.get("message")                       # Ollama
                   or body[:200])
        except Exception:
            msg = str(e)
        if e.code == 401:
            msg = f"Invalid API key for {provider}. Check Settings → AI Coach."
        elif e.code == 429:
            msg = f"{provider} rate limit hit. Wait a moment and try again."
        return {"ok": False, "error": f"{provider} error {e.code}: {msg}"}
    return {"ok": False, "error": f"{provider} request failed: {str(e)}"}


def _call_anthropic(prompt: str, api_key: str) -> dict:
    """
    Call Anthropic Claude API.
    Docs: https://docs.anthropic.com/en/api/messages
    """
    if not api_key:
        return {
            "ok": False, "error": "no_api_key",
            "message": ("No API key set. Go to Settings → AI Coach → "
                        "paste your Anthropic key from console.anthropic.com")
        }
    try:
        result = _http_post(
            url="https://api.anthropic.com/v1/messages",
            payload={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 800,
                "messages":   [{"role": "user", "content": prompt}],
            },
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
        )
        text = "".join(
            b.get("text", "") for b in result.get("content", [])
            if b.get("type") == "text"
        )
        return {"ok": True, "text": text, "provider": "Claude"}
    except Exception as e:
        return _wrap_error(e, "Anthropic")


def _call_openai(prompt: str, api_key: str) -> dict:
    """
    Call OpenAI GPT-4o API.
    Docs: https://platform.openai.com/docs/api-reference/chat
    """
    if not api_key:
        return {
            "ok": False, "error": "no_api_key",
            "message": ("No API key set. Go to Settings → AI Coach → "
                        "paste your OpenAI key from platform.openai.com")
        }
    try:
        result = _http_post(
            url="https://api.openai.com/v1/chat/completions",
            payload={
                "model":       "gpt-4o",
                "max_tokens":  800,
                "messages":    [{"role": "user", "content": prompt}],
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type":  "application/json",
            },
        )
        text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {"ok": True, "text": text, "provider": "GPT-4o"}
    except Exception as e:
        return _wrap_error(e, "OpenAI")


def _call_gemini(prompt: str, api_key: str) -> dict:
    """
    Call Google Gemini API.
    Docs: https://ai.google.dev/api/generate-content
    """
    if not api_key:
        return {
            "ok": False, "error": "no_api_key",
            "message": ("No API key set. Go to Settings → AI Coach → "
                        "paste your Google AI key from ai.google.dev")
        }
    try:
        result = _http_post(
            url=f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-1.5-pro:generateContent?key={api_key}",
            payload={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 800},
            },
            headers={"content-type": "application/json"},
        )
        text = (result.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", ""))
        return {"ok": True, "text": text, "provider": "Gemini"}
    except Exception as e:
        return _wrap_error(e, "Gemini")


def _call_ollama(prompt: str, base_url: str) -> dict:
    """
    Call local Ollama server.
    Docs: https://github.com/ollama/ollama/blob/main/docs/api.md
    No API key needed — runs on your machine.
    """
    base_url = base_url.rstrip("/")
    try:
        result = _http_post(
            url=f"{base_url}/api/chat",
            payload={
                "model":    "llama3.2",   # user can change model in Ollama
                "stream":   False,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers={"content-type": "application/json"},
            timeout=60,  # local LLMs can be slow
        )
        text = result.get("message", {}).get("content", "")
        return {"ok": True, "text": text, "provider": "Ollama"}
    except urllib.error.URLError as e:
        return {"ok": False,
                "error": f"Cannot reach Ollama at {base_url}. "
                         f"Is it running? Start with: ollama serve"}
    except Exception as e:
        return _wrap_error(e, "Ollama")


def _call_custom(prompt: str, api_key: str, url: str) -> dict:
    """
    Call a custom OpenAI-compatible endpoint.
    Works with: LM Studio, text-generation-webui, llama.cpp server, etc.
    """
    try:
        headers = {"content-type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        result = _http_post(
            url=url,
            payload={
                "model":    "local-model",
                "messages": [{"role": "user", "content": prompt}],
            },
            headers=headers,
            timeout=60,
        )
        # Try OpenAI format first, then Ollama format
        text = (result.get("choices", [{}])[0].get("message", {}).get("content")
                or result.get("message", {}).get("content")
                or str(result))
        return {"ok": True, "text": text, "provider": "Custom"}
    except Exception as e:
        return _wrap_error(e, "Custom endpoint")
