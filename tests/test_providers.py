"""Acceptance tests for SF-005 Multi-provider routing and capability validation."""
import asyncio
from pathlib import Path
import pytest

from stockforge.providers.base import (
    ProviderCapability,
    ProviderKind,
    RoleType,
)
from stockforge.providers.mock import MockImageProvider, MockTextProvider, MockVisionProvider
from stockforge.providers.registry import (
    IncompatibleFallbackError,
    IncompatibleProviderError,
    ProviderRegistry,
)
from stockforge.providers.router import ProviderRouter, redact_sensitive_data


def test_each_role_uses_different_mocked_provider():
    """Verify each role can use a different configured mock provider."""
    registry = ProviderRegistry()

    # 1. Register 5 distinct providers
    registry.register_provider("p_research", ProviderKind.TEXT, "https://api.research.com", "KEY_RES")
    registry.register_provider("p_prompt", ProviderKind.TEXT, "https://api.prompt.com", "KEY_PRM")
    registry.register_provider("p_image", ProviderKind.IMAGE, "https://api.image.com", "KEY_IMG")
    registry.register_provider("p_vision", ProviderKind.VISION, "https://api.vision.com", "KEY_VIS")
    registry.register_provider("p_metadata", ProviderKind.VISION, "https://api.meta.com", "KEY_MET")

    # 2. Register distinct models with verified capabilities
    registry.register_model(
        "model-res-1", "p_research", ProviderKind.TEXT, {ProviderCapability.TEXT_GENERATION}
    )
    registry.register_model(
        "model-prm-1", "p_prompt", ProviderKind.TEXT, {ProviderCapability.TEXT_GENERATION}
    )
    registry.register_model(
        "model-img-1",
        "p_image",
        ProviderKind.IMAGE,
        {ProviderCapability.IMAGE_GENERATION, ProviderCapability.COMMERCIAL_LICENSE_VERIFIED},
        license_evidence="license_stock_verified",
    )
    registry.register_model(
        "model-vis-1", "p_vision", ProviderKind.VISION, {ProviderCapability.VISION_INSPECTION}
    )
    registry.register_model(
        "model-met-1", "p_metadata", ProviderKind.VISION, {ProviderCapability.VISION_INSPECTION}
    )

    router = ProviderRouter(registry)
    router.set_route(RoleType.RESEARCH, "model-res-1")
    router.set_route(RoleType.PROMPT, "model-prm-1")
    router.set_route(RoleType.IMAGE, "model-img-1")
    router.set_route(RoleType.VISION_QC, "model-vis-1")
    router.set_route(RoleType.METADATA, "model-met-1")

    # Verify distinct resolution per role
    assert router.resolve_model(RoleType.RESEARCH) == "model-res-1"
    assert router.resolve_model(RoleType.PROMPT) == "model-prm-1"
    assert router.resolve_model(RoleType.IMAGE) == "model-img-1"
    assert router.resolve_model(RoleType.VISION_QC) == "model-vis-1"
    assert router.resolve_model(RoleType.METADATA) == "model-met-1"

    # Verify provider origins are distinct
    assert registry.get_model("model-res-1").provider_name == "p_research"
    assert registry.get_model("model-prm-1").provider_name == "p_prompt"
    assert registry.get_model("model-img-1").provider_name == "p_image"
    assert registry.get_model("model-vis-1").provider_name == "p_vision"
    assert registry.get_model("model-met-1").provider_name == "p_metadata"


def test_incompatible_fallback_rejected():
    """Verify that incompatible fallback candidates (e.g. text model for image role) are rejected."""
    registry = ProviderRegistry()
    registry.register_provider("p_img", ProviderKind.IMAGE, "https://api.image.com", "KEY_IMG")
    registry.register_provider("p_text", ProviderKind.TEXT, "https://api.text.com", "KEY_TXT")

    # Valid image model
    registry.register_model(
        "flux-pro",
        "p_img",
        ProviderKind.IMAGE,
        {ProviderCapability.IMAGE_GENERATION, ProviderCapability.COMMERCIAL_LICENSE_VERIFIED},
    )

    # Incompatible text model (no image generation, no commercial image license)
    registry.register_model(
        "llama-chat",
        "p_text",
        ProviderKind.TEXT,
        {ProviderCapability.TEXT_GENERATION},
    )

    router = ProviderRouter(registry)

    # Attempting to assign llama-chat as fallback for IMAGE role must raise IncompatibleFallbackError
    with pytest.raises(IncompatibleFallbackError, match="tidak kompatibel untuk role 'image'"):
        router.set_route(RoleType.IMAGE, primary_model_id="flux-pro", fallbacks=["llama-chat"])


def test_incompatible_primary_rejected():
    """Verify that assigning a text model as primary for image role is rejected immediately."""
    registry = ProviderRegistry()
    registry.register_provider("p_text", ProviderKind.TEXT, "https://api.text.com", "KEY_TXT")
    registry.register_model("gpt-text", "p_text", ProviderKind.TEXT, {ProviderCapability.TEXT_GENERATION})

    router = ProviderRouter(registry)
    with pytest.raises(IncompatibleProviderError, match="tidak kompatibel untuk role 'image'"):
        router.set_route(RoleType.IMAGE, primary_model_id="gpt-text")


def test_compatible_fallback_resolution_and_exhaustion():
    """Verify ordered fallback resolution and clean exception on exhaustion."""
    registry = ProviderRegistry()
    registry.register_provider("p_img", ProviderKind.IMAGE, "https://api.image.com", "KEY_IMG")

    registry.register_model(
        "primary-img",
        "p_img",
        ProviderKind.IMAGE,
        {ProviderCapability.IMAGE_GENERATION, ProviderCapability.COMMERCIAL_LICENSE_VERIFIED},
    )
    registry.register_model(
        "fallback-img-1",
        "p_img",
        ProviderKind.IMAGE,
        {ProviderCapability.IMAGE_GENERATION, ProviderCapability.COMMERCIAL_LICENSE_VERIFIED},
    )

    router = ProviderRouter(registry)
    router.set_route(RoleType.IMAGE, primary_model_id="primary-img", fallbacks=["fallback-img-1"])

    assert router.resolve_model(RoleType.IMAGE, fallback_attempt=0) == "primary-img"
    assert router.resolve_model(RoleType.IMAGE, fallback_attempt=1) == "fallback-img-1"

    # Attempt 2 exceeds available fallback candidates -> exhaustion error
    with pytest.raises(IncompatibleFallbackError, match="telah habis"):
        router.resolve_model(RoleType.IMAGE, fallback_attempt=2)


def test_redact_secrets_in_payloads():
    """Verify recursive redaction of sensitive keys and token values."""
    payload = {
        "user": "admin",
        "api_key": "secret_abc123456",
        "headers": {
            "Authorization": "Bearer sk-9876543210abcdef",
            "Content-Type": "application/json",
        },
        "nested": [
            {"secret_token": "token_val_xxx"},
            "raw text with Bearer secret-bearer-token and sk-secretkey1234",
        ],
    }

    sanitized = redact_sensitive_data(payload)
    assert sanitized["user"] == "admin"
    assert sanitized["api_key"] == "***REDACTED***"
    assert sanitized["headers"]["Authorization"] == "***REDACTED***"
    assert sanitized["headers"]["Content-Type"] == "application/json"
    assert sanitized["nested"][0]["secret_token"] == "***REDACTED***"
    assert "secret-bearer-token" not in sanitized["nested"][1]
    assert "***REDACTED***" in sanitized["nested"][1]


def test_yaml_models_config_loading():
    """Verify loading from config/models.yaml file binds all roles."""
    project_root = Path(__file__).parent.parent
    yaml_file = project_root / "config" / "models.yaml"

    registry = ProviderRegistry()
    router = ProviderRouter(registry)

    fake_env = {
        "RESEARCH_BASE_URL": "https://api.research.com/v1",
        "RESEARCH_MODEL": "gemini-flash",
        "PROMPT_MODEL": "gpt-prompt",
        "IMAGE_MODEL": "flux-pro-stock",
        "VISION_QC_MODEL": "gemini-vision",
        "METADATA_MODEL": "gemini-metadata",
    }

    router.load_from_yaml(yaml_file, env_vars=fake_env)

    assert router.resolve_model(RoleType.RESEARCH) == "gemini-flash"
    assert router.resolve_model(RoleType.PROMPT) == "gpt-prompt"
    assert router.resolve_model(RoleType.IMAGE) == "flux-pro-stock"
    assert router.resolve_model(RoleType.VISION_QC) == "gemini-vision"
    assert router.resolve_model(RoleType.METADATA) == "gemini-metadata"


def test_mock_provider_protocols_execution():
    """Verify mock providers execute asynchronously and record request IDs and costs."""
    text_p = MockTextProvider(provider_name="text-mock")
    img_p = MockImageProvider(provider_name="image-mock")
    vis_p = MockVisionProvider(provider_name="vision-mock")

    async def run_all():
        t_res = await text_p.generate_text("Generate prompt for cybersecurity", model="text-m1")
        i_res = await img_p.generate_image("A futuristic cybersecurity server", model="img-m1")
        v_res = await vis_p.analyze_image(i_res.image_bytes, "Check blur", model="vis-m1")
        return t_res, i_res, v_res

    t_res, i_res, v_res = asyncio.run(run_all())

    # Text response
    assert t_res.request_id.startswith("req-")
    assert t_res.prompt_tokens == 25
    assert t_res.completion_tokens == 50
    assert t_res.actual_cost_cents > 0

    # Image response
    assert i_res.request_id.startswith("req-")
    assert i_res.width == 2048
    assert i_res.format == "JPEG"
    assert len(i_res.image_bytes) > 0

    # Vision response
    assert v_res.request_id.startswith("req-")
    assert v_res.passed is True
    assert "clarity" in v_res.analysis
