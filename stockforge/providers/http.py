"""HTTP provider adapters for OpenAI-compatible REST endpoints (SF-005)."""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import uuid
from typing import Any, Dict, Optional
import httpx
from PIL import Image

from stockforge.errors.classifier import ErrorClassifier
from stockforge.providers.base import (
    ImageProvider,
    ProviderCapability,
    ProviderImageResponse,
    ProviderKind,
    ProviderResponse,
    ProviderTextResponse,
    ProviderVisionResponse,
    RoleType,
    TextProvider,
    VisionProvider,
)
from stockforge.providers.router import redact_sensitive_data

logger = logging.getLogger("stockforge.providers.http")


class HttpTextProvider(TextProvider):
    """OpenAI-compatible text generation provider using httpx."""

    def __init__(
        self,
        provider_name: str,
        base_url: str,
        api_key: str,
        timeout: float = 60.0,
    ) -> None:
        self.provider_name = provider_name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    async def generate_text(
        self,
        prompt: str,
        model: str,
        max_tokens: int = 4000,
        temperature: float = 0.7,
        operation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ProviderTextResponse:
        req_id = f"req-{uuid.uuid4().hex[:8]}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "max_completion_tokens": max_tokens,
            "max_new_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }

        sanitized_payload = redact_sensitive_data(payload)
        logger.info("Calling provider %s at %s with model %s", self.provider_name, self.base_url, model)

        timeout_config = httpx.Timeout(connect=15.0, read=90.0, write=15.0, pool=15.0)
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
            except Exception as e:
                err_str = str(e).lower()
                if any(k in err_str for k in ["connect", "timeout", "remoteprotocol", "broken pipe", "connection reset"]):
                    try:
                        from stockforge.bot.alerts import gateway_alerts
                        gateway_alerts.on_ai_gateway_disconnected(self.provider_name, self.base_url, str(e))
                    except Exception:
                        pass
                logger.error("Connection error to %s: %s", self.provider_name, e)
                raise RuntimeError(f"Connection error to {self.provider_name}: {e}") from e

        if resp.status_code == 200:
            try:
                from stockforge.bot.alerts import gateway_alerts
                gateway_alerts.on_ai_gateway_reconnected(self.provider_name, self.base_url)
            except Exception:
                pass
        else:
            logger.error("Provider %s error HTTP %s: %s", self.provider_name, resp.status_code, resp.text[:200])
            classifier = ErrorClassifier()
            norm = classifier.classify(
                http_status=resp.status_code,
                headers=dict(resp.headers),
                body=resp.text,
                provider=self.provider_name,
                model=model,
            )
            raise RuntimeError(f"Provider {self.provider_name} failed: [{norm.code.value}] {norm.safe_user_message}")

        raw_text_body = resp.text
        if "data: [DONE]" in raw_text_body:
            raw_text_body = raw_text_body.replace("data: [DONE]", "").strip()

        try:
            data = json.loads(raw_text_body)
        except Exception:
            data = resp.json()

        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        text = msg.get("content") or ""
        if not text and "reasoning_content" in msg:
            text = msg.get("reasoning_content", "")
        if not text and "reasoning" in msg:
            text = msg.get("reasoning", "")

        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        return ProviderTextResponse(
            request_id=req_id,
            provider=self.provider_name,
            model=model,
            role=RoleType.RESEARCH,
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_cents=1,
            actual_cost_cents=1,
            raw_response_redacted=str(sanitized_payload),
        )


class HttpImageProvider(ImageProvider):
    """OpenAI-compatible image generation provider using httpx."""

    def __init__(
        self,
        provider_name: str,
        base_url: str,
        api_key: str,
        timeout: float = 120.0,
    ) -> None:
        self.provider_name = provider_name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    async def generate_image(
        self,
        prompt: str,
        model: str,
        width: int = 2048,
        height: int = 2048,
        operation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ProviderImageResponse:
        req_id = f"req-{uuid.uuid4().hex[:8]}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        size_str = f"{width}x{height}"
        payload = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size_str,
            "response_format": "b64_json",
        }
        sanitized_payload = redact_sensitive_data(payload)
        logger.info("Calling image provider %s at %s with model %s", self.provider_name, self.base_url, model)

        endpoint = self.base_url.rstrip("/")
        if endpoint.endswith("/images/generations") or endpoint.endswith("/generations"):
            url = endpoint
        elif endpoint.endswith("/images"):
            url = f"{endpoint}/generations"
        else:
            url = f"{endpoint}/images/generations"

        timeout_config = httpx.Timeout(connect=15.0, read=120.0, write=15.0, pool=15.0)
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            try:
                resp = await client.post(
                    url,
                    json=payload,
                    headers=headers,
                )
            except Exception as e:
                err_str = str(e).lower()
                if any(k in err_str for k in ["connect", "timeout", "remoteprotocol", "broken pipe", "connection reset"]):
                    try:
                        from stockforge.bot.alerts import gateway_alerts
                        gateway_alerts.on_ai_gateway_disconnected(self.provider_name, self.base_url, str(e))
                    except Exception:
                        pass
                logger.error("Connection error to %s: %s", self.provider_name, e)
                raise RuntimeError(f"Connection error to {self.provider_name}: {e}") from e

        if resp.status_code == 200:
            try:
                from stockforge.bot.alerts import gateway_alerts
                gateway_alerts.on_ai_gateway_reconnected(self.provider_name, self.base_url)
            except Exception:
                pass
        else:
            logger.error("Image Provider %s error HTTP %s: %s", self.provider_name, resp.status_code, resp.text[:200])
            classifier = ErrorClassifier()
            norm = classifier.classify(
                http_status=resp.status_code,
                headers=dict(resp.headers),
                body=resp.text,
                provider=self.provider_name,
                model=model,
            )
            raise RuntimeError(f"Provider {self.provider_name} failed: [{norm.code.value}] {norm.safe_user_message}")

        try:
            data = resp.json()
            item = data.get("data", [{}])[0]
            if "b64_json" in item:
                img_bytes = base64.b64decode(item["b64_json"])
            elif "url" in item:
                async with httpx.AsyncClient(timeout=timeout_config) as client:
                    img_dl = await client.get(item["url"])
                    img_bytes = img_dl.content
            else:
                raise ValueError("Data response tidak memiliki b64_json atau url.")
        except Exception as ex:
            raise RuntimeError(f"Format respon image provider {self.provider_name} tidak valid: {ex}") from ex

        img = Image.open(io.BytesIO(img_bytes))
        actual_w, actual_h = img.size
        buf = io.BytesIO()
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=95)
        jpeg_bytes = buf.getvalue()

        return ProviderImageResponse(
            request_id=req_id,
            provider=self.provider_name,
            model=model,
            role=RoleType.IMAGE,
            image_bytes=jpeg_bytes,
            width=actual_w,
            height=actual_h,
            format="JPEG",
            mime_type="image/jpeg",
            estimated_cost_cents=10,
            actual_cost_cents=10,
            raw_response_redacted=str(sanitized_payload),
        )


class HttpVisionProvider(VisionProvider):
    """OpenAI-compatible vision provider using httpx and multimodal chat completions."""

    def __init__(
        self,
        provider_name: str,
        base_url: str,
        api_key: str,
        timeout: float = 90.0,
    ) -> None:
        self.provider_name = provider_name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @staticmethod
    def _prepare_vision_image_bytes(
        image_bytes: bytes,
        max_dim: int = 1024,
        max_size_bytes: int = 500_000,
    ) -> bytes:
        """Downscale in-memory copy of image for vision inspection API payload.

        Prevents HTTP 413 (Request Entity Too Large) from reverse proxies (e.g. Nginx).
        Master commercial raw image on disk remains untouched at target megapixels.
        """
        try:
            img = Image.open(io.BytesIO(image_bytes))
            orig_w, orig_h = img.size
            scale = min(1.0, max_dim / max(orig_w, orig_h, 1))
            if scale < 1.0 or len(image_bytes) > max_size_bytes or img.mode != "RGB":
                new_w = max(1, int(orig_w * scale))
                new_h = max(1, int(orig_h * scale))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                if (new_w, new_h) != (orig_w, orig_h):
                    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85, optimize=True)
                return buf.getvalue()
            return image_bytes
        except Exception as e:
            logger.warning("Could not optimize image bytes for vision provider: %s", e)
            return image_bytes

    async def analyze_image(
        self,
        image_bytes: bytes,
        prompt: str,
        model: str,
        operation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ProviderVisionResponse:
        req_id = f"req-{uuid.uuid4().hex[:8]}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        optimized_bytes = self._prepare_vision_image_bytes(image_bytes)
        b64_img = base64.b64encode(optimized_bytes).decode("utf-8")
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_img}"
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 2000,
            "max_completion_tokens": 2000,
            "max_new_tokens": 2000,
            "temperature": 0.2,
            "stream": False,
        }
        sanitized_payload = redact_sensitive_data(payload)
        logger.info("Calling vision provider %s at %s with model %s", self.provider_name, self.base_url, model)

        timeout_config = httpx.Timeout(connect=15.0, read=90.0, write=15.0, pool=15.0)
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
            except Exception as e:
                err_str = str(e).lower()
                if any(k in err_str for k in ["connect", "timeout", "remoteprotocol", "broken pipe", "connection reset"]):
                    try:
                        from stockforge.bot.alerts import gateway_alerts
                        gateway_alerts.on_ai_gateway_disconnected(self.provider_name, self.base_url, str(e))
                    except Exception:
                        pass
                logger.error("Connection error to %s: %s", self.provider_name, e)
                raise RuntimeError(f"Connection error to {self.provider_name}: {e}") from e

        if resp.status_code == 200:
            try:
                from stockforge.bot.alerts import gateway_alerts
                gateway_alerts.on_ai_gateway_reconnected(self.provider_name, self.base_url)
            except Exception:
                pass
        else:
            logger.error("Vision Provider %s error HTTP %s: %s", self.provider_name, resp.status_code, resp.text[:200])
            classifier = ErrorClassifier()
            norm = classifier.classify(
                http_status=resp.status_code,
                headers=dict(resp.headers),
                body=resp.text,
                provider=self.provider_name,
                model=model,
            )
            raise RuntimeError(f"Provider {self.provider_name} failed: [{norm.code.value}] {norm.safe_user_message}")

        data = resp.json()
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        raw_text = msg.get("content") or ""
        if not raw_text and "reasoning_content" in msg:
            raw_text = msg.get("reasoning_content", "")

        from stockforge.qc.vision import safe_extract_json
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)
        analysis_dict = safe_extract_json(cleaned)

        passed = bool(analysis_dict.get("passed", True))

        return ProviderVisionResponse(
            request_id=req_id,
            provider=self.provider_name,
            model=model,
            role=RoleType.VISION_QC,
            analysis=analysis_dict,
            passed=passed,
            estimated_cost_cents=2,
            actual_cost_cents=2,
            raw_response_redacted=str(sanitized_payload),
        )
