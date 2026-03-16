from dataclasses import dataclass
import os


SENTINEL_PROVIDER_KEY = "stage1-sentinel-provider-key"
DEFAULT_PROVIDER_BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True)
class LiteLLMSettings:
    service_name: str
    stage: str
    response_mode: str
    provider_api_key: str
    provider_base_url: str


def litellm_settings() -> LiteLLMSettings:
    response_mode = os.environ.get(
        "RSI_LITELLM_RESPONSE_MODE",
        "deterministic_mock",
    ).strip()
    assert response_mode in {
        "deterministic_mock",
        "provider_passthrough",
    }, f"unsupported RSI_LITELLM_RESPONSE_MODE: {response_mode}"

    provider_api_key = os.environ.get(
        "OPENAI_API_KEY",
        SENTINEL_PROVIDER_KEY,
    ).strip()
    provider_base_url = os.environ.get(
        "RSI_OPENAI_BASE_URL",
        DEFAULT_PROVIDER_BASE_URL,
    ).strip()
    assert provider_base_url, "RSI_OPENAI_BASE_URL must not be empty on litellm"

    if response_mode == "provider_passthrough":
        assert provider_api_key, "OPENAI_API_KEY must not be empty on litellm"
        assert provider_api_key != SENTINEL_PROVIDER_KEY, (
            "OPENAI_API_KEY must be set to a real provider key for provider_passthrough mode"
        )

    return LiteLLMSettings(
        service_name="litellm",
        stage="stage6_read_only_browser",
        response_mode=response_mode,
        provider_api_key=provider_api_key,
        provider_base_url=provider_base_url.rstrip("/"),
    )
