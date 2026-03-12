from dataclasses import dataclass
import os


@dataclass(frozen=True)
class LiteLLMSettings:
    service_name: str
    stage: str
    provider_api_key: str


def litellm_settings() -> LiteLLMSettings:
    provider_api_key = os.environ.get(
        "OPENAI_API_KEY",
        "stage1-sentinel-provider-key",
    ).strip()
    assert provider_api_key, "OPENAI_API_KEY must not be empty on litellm"

    return LiteLLMSettings(
        service_name="litellm",
        stage="stage1_hard_boundary",
        provider_api_key=provider_api_key,
    )
