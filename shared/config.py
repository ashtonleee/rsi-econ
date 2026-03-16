from dataclasses import dataclass
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRUSTED_STATE_DIR = ROOT / "runtime" / "trusted_state"
DEFAULT_LOG_DIR = DEFAULT_TRUSTED_STATE_DIR / "logs"
DEFAULT_STATE_DIR = DEFAULT_TRUSTED_STATE_DIR / "state"
DEFAULT_CHECKPOINT_DIR = DEFAULT_TRUSTED_STATE_DIR / "checkpoints"
DEFAULT_BRIDGE_URL = "http://bridge:8000"
DEFAULT_LITELLM_URL = "http://litellm:4000"
DEFAULT_FETCHER_URL = "http://fetcher:8082"
DEFAULT_BROWSER_URL = "http://browser:8083"
DEFAULT_EGRESS_URL = "http://egress:8084"
DEFAULT_AGENT_URL = "http://agent:8001"
DEFAULT_AGENT_WORKSPACE_DIR = ROOT / "untrusted" / "agent_workspace"
DEFAULT_AGENT_RUNTIME_CODE_DIR = ROOT / "untrusted"
DEFAULT_SEED_BASELINE_DIR = ROOT / "trusted" / "recovery" / "seed_workspace_baseline"
DEFAULT_PUBLIC_PROBE_URL = "http://1.1.1.1"
DEFAULT_PROVIDER_PROBE_URL = "https://api.openai.com/v1/models"
DEFAULT_LLM_BUDGET_TOKEN_CAP = 100
DEFAULT_BUDGET_UNIT = "mock_tokens"
DEFAULT_WEB_ALLOWLIST_HOSTS = ("example.com",)
DEFAULT_FETCH_ALLOWED_CONTENT_TYPES = ("text/plain", "text/html")
DEFAULT_WEB_MAX_RESPONSE_BYTES = 8192
DEFAULT_WEB_MAX_PREVIEW_CHARS = 1024
DEFAULT_WEB_MAX_REDIRECTS = 3
DEFAULT_WEB_TIMEOUT_SECONDS = 5.0
DEFAULT_FETCH_USER_AGENT = "rsi-fetcher/0.1"
DEFAULT_BROWSER_VIEWPORT_WIDTH = 1280
DEFAULT_BROWSER_VIEWPORT_HEIGHT = 720
DEFAULT_BROWSER_TIMEOUT_SECONDS = 10.0
DEFAULT_BROWSER_SETTLE_TIME_MS = 500
DEFAULT_BROWSER_MAX_RENDERED_TEXT_BYTES = 16384
DEFAULT_BROWSER_MAX_SCREENSHOT_BYTES = 1024 * 1024
DEFAULT_BROWSER_MAX_FOLLOWABLE_LINKS = 20
DEFAULT_BROWSER_MAX_FOLLOW_HOPS = 1


def _resolve_path(raw: str | None, default: Path) -> Path:
    if raw is None or raw.strip() == "":
        return default

    path = Path(raw)
    if path.is_absolute():
        return path
    return ROOT / path


def _split_csv(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None or raw.strip() == "":
        return default
    values = [item.strip().lower() for item in raw.split(",") if item.strip()]
    return tuple(values)


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv_mapping(raw: str | None) -> dict[str, tuple[str, ...]]:
    if raw is None or raw.strip() == "":
        return {}
    entries: dict[str, tuple[str, ...]] = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        host, sep, values = pair.partition("=")
        if not sep:
            continue
        ips = tuple(item.strip() for item in values.split("|") if item.strip())
        if ips:
            entries[host.strip().lower()] = ips
    return entries


@dataclass(frozen=True)
class BridgeSettings:
    service_name: str
    stage: str
    trusted_state_dir: Path
    log_dir: Path
    state_dir: Path
    checkpoint_dir: Path
    seed_baseline_dir: Path
    litellm_url: str
    fetcher_url: str
    browser_url: str
    egress_url: str
    agent_url: str
    llm_budget_token_cap: int
    budget_unit: str
    web_allowlist_hosts: tuple[str, ...]
    web_private_test_hosts: tuple[str, ...]
    web_allowed_content_types: tuple[str, ...]
    web_max_response_bytes: int
    web_max_preview_chars: int
    web_max_redirects: int
    web_timeout_seconds: float
    fetch_user_agent: str
    browser_timeout_seconds: float
    browser_viewport_width: int
    browser_viewport_height: int
    browser_settle_time_ms: int
    browser_max_rendered_text_bytes: int
    browser_max_screenshot_bytes: int
    browser_max_followable_links: int
    browser_max_follow_hops: int
    enable_debug_probes: bool


@dataclass(frozen=True)
class BrowserSettings:
    service_name: str
    stage: str
    browser_url: str
    egress_url: str
    allowlist_hosts: tuple[str, ...]
    private_test_hosts: tuple[str, ...]
    max_redirects: int
    timeout_seconds: float
    viewport_width: int
    viewport_height: int
    settle_time_ms: int
    max_rendered_text_bytes: int
    max_screenshot_bytes: int
    max_followable_links: int
    max_follow_hops: int
    enable_private_test_hosts: bool


@dataclass(frozen=True)
class FetcherSettings:
    service_name: str
    stage: str
    fetcher_url: str
    egress_url: str
    allowlist_hosts: tuple[str, ...]
    private_test_hosts: tuple[str, ...]
    allowed_content_types: tuple[str, ...]
    max_response_bytes: int
    max_preview_chars: int
    max_redirects: int
    timeout_seconds: float
    user_agent: str
    enable_private_test_hosts: bool


@dataclass(frozen=True)
class AgentSettings:
    service_name: str
    stage: str
    bridge_url: str
    workspace_dir: Path
    runtime_code_dir: Path
    public_probe_url: str
    provider_probe_url: str


@dataclass(frozen=True)
class EgressSettings:
    service_name: str
    stage: str
    egress_url: str
    allowlist_hosts: tuple[str, ...]
    private_test_hosts: tuple[str, ...]
    timeout_seconds: float
    allowed_content_types: tuple[str, ...]
    user_agent: str
    max_redirects: int
    enable_private_test_hosts: bool
    test_ip_overrides: dict[str, tuple[str, ...]]


def bridge_settings() -> BridgeSettings:
    trusted_state_dir = _resolve_path(
        os.environ.get("RSI_TRUSTED_STATE_DIR"),
        DEFAULT_TRUSTED_STATE_DIR,
    )
    llm_budget_token_cap = int(
        os.environ.get(
            "RSI_LLM_BUDGET_TOKEN_CAP",
            str(DEFAULT_LLM_BUDGET_TOKEN_CAP),
        ).strip()
    )
    assert llm_budget_token_cap > 0, "RSI_LLM_BUDGET_TOKEN_CAP must be positive"
    web_max_response_bytes = int(
        os.environ.get(
            "RSI_WEB_MAX_RESPONSE_BYTES",
            str(DEFAULT_WEB_MAX_RESPONSE_BYTES),
        ).strip()
    )
    web_max_preview_chars = int(
        os.environ.get(
            "RSI_WEB_MAX_PREVIEW_CHARS",
            str(DEFAULT_WEB_MAX_PREVIEW_CHARS),
        ).strip()
    )
    web_max_redirects = int(
        os.environ.get(
            "RSI_WEB_MAX_REDIRECTS",
            str(DEFAULT_WEB_MAX_REDIRECTS),
        ).strip()
    )
    web_timeout_seconds = float(
        os.environ.get(
            "RSI_WEB_TIMEOUT_SECONDS",
            str(DEFAULT_WEB_TIMEOUT_SECONDS),
        ).strip()
    )
    assert web_max_response_bytes > 0
    assert web_max_preview_chars > 0
    assert web_max_redirects >= 0
    assert web_timeout_seconds > 0
    browser_timeout_seconds = float(
        os.environ.get(
            "RSI_BROWSER_TIMEOUT_SECONDS",
            str(DEFAULT_BROWSER_TIMEOUT_SECONDS),
        ).strip()
    )
    browser_viewport_width = int(
        os.environ.get(
            "RSI_BROWSER_VIEWPORT_WIDTH",
            str(DEFAULT_BROWSER_VIEWPORT_WIDTH),
        ).strip()
    )
    browser_viewport_height = int(
        os.environ.get(
            "RSI_BROWSER_VIEWPORT_HEIGHT",
            str(DEFAULT_BROWSER_VIEWPORT_HEIGHT),
        ).strip()
    )
    browser_settle_time_ms = int(
        os.environ.get(
            "RSI_BROWSER_SETTLE_TIME_MS",
            str(DEFAULT_BROWSER_SETTLE_TIME_MS),
        ).strip()
    )
    browser_max_rendered_text_bytes = int(
        os.environ.get(
            "RSI_BROWSER_MAX_RENDERED_TEXT_BYTES",
            str(DEFAULT_BROWSER_MAX_RENDERED_TEXT_BYTES),
        ).strip()
    )
    browser_max_screenshot_bytes = int(
        os.environ.get(
            "RSI_BROWSER_MAX_SCREENSHOT_BYTES",
            str(DEFAULT_BROWSER_MAX_SCREENSHOT_BYTES),
        ).strip()
    )
    browser_max_followable_links = int(
        os.environ.get(
            "RSI_BROWSER_MAX_FOLLOWABLE_LINKS",
            str(DEFAULT_BROWSER_MAX_FOLLOWABLE_LINKS),
        ).strip()
    )
    browser_max_follow_hops = int(
        os.environ.get(
            "RSI_BROWSER_MAX_FOLLOW_HOPS",
            str(DEFAULT_BROWSER_MAX_FOLLOW_HOPS),
        ).strip()
    )
    assert browser_timeout_seconds > 0
    assert browser_viewport_width > 0
    assert browser_viewport_height > 0
    assert browser_settle_time_ms >= 0
    assert browser_max_rendered_text_bytes > 0
    assert browser_max_screenshot_bytes > 0
    assert browser_max_followable_links > 0
    assert browser_max_follow_hops == 1
    return BridgeSettings(
        service_name="bridge",
        stage="stage6_read_only_browser",
        trusted_state_dir=trusted_state_dir,
        log_dir=_resolve_path(
            os.environ.get("RSI_BRIDGE_LOG_DIR"),
            trusted_state_dir / "logs",
        ),
        state_dir=_resolve_path(
            os.environ.get("RSI_BRIDGE_STATE_DIR"),
            trusted_state_dir / "state",
        ),
        checkpoint_dir=_resolve_path(
            os.environ.get("RSI_CHECKPOINT_DIR"),
            trusted_state_dir / "checkpoints",
        ),
        seed_baseline_dir=_resolve_path(
            os.environ.get("RSI_SEED_BASELINE_DIR"),
            DEFAULT_SEED_BASELINE_DIR,
        ),
        litellm_url=os.environ.get("RSI_LITELLM_URL", DEFAULT_LITELLM_URL).strip(),
        fetcher_url=os.environ.get("RSI_FETCHER_URL", DEFAULT_FETCHER_URL).strip(),
        browser_url=os.environ.get("RSI_BROWSER_URL", DEFAULT_BROWSER_URL).strip(),
        egress_url=os.environ.get("RSI_EGRESS_URL", DEFAULT_EGRESS_URL).strip(),
        agent_url=os.environ.get("RSI_AGENT_URL", DEFAULT_AGENT_URL).strip(),
        llm_budget_token_cap=llm_budget_token_cap,
        budget_unit=os.environ.get("RSI_BUDGET_UNIT", DEFAULT_BUDGET_UNIT).strip(),
        web_allowlist_hosts=_split_csv(
            os.environ.get("RSI_WEB_ALLOWLIST_HOSTS"),
            DEFAULT_WEB_ALLOWLIST_HOSTS,
        ),
        web_private_test_hosts=_split_csv(
            os.environ.get("RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS"),
            (),
        ),
        web_allowed_content_types=_split_csv(
            os.environ.get("RSI_FETCH_ALLOWED_CONTENT_TYPES"),
            DEFAULT_FETCH_ALLOWED_CONTENT_TYPES,
        ),
        web_max_response_bytes=web_max_response_bytes,
        web_max_preview_chars=web_max_preview_chars,
        web_max_redirects=web_max_redirects,
        web_timeout_seconds=web_timeout_seconds,
        fetch_user_agent=os.environ.get(
            "RSI_FETCH_USER_AGENT",
            DEFAULT_FETCH_USER_AGENT,
        ).strip(),
        browser_timeout_seconds=browser_timeout_seconds,
        browser_viewport_width=browser_viewport_width,
        browser_viewport_height=browser_viewport_height,
        browser_settle_time_ms=browser_settle_time_ms,
        browser_max_rendered_text_bytes=browser_max_rendered_text_bytes,
        browser_max_screenshot_bytes=browser_max_screenshot_bytes,
        browser_max_followable_links=browser_max_followable_links,
        browser_max_follow_hops=browser_max_follow_hops,
        enable_debug_probes=_env_flag("RSI_ENABLE_DEBUG_PROBES"),
    )


def fetcher_settings() -> FetcherSettings:
    web_max_response_bytes = int(
        os.environ.get(
            "RSI_WEB_MAX_RESPONSE_BYTES",
            str(DEFAULT_WEB_MAX_RESPONSE_BYTES),
        ).strip()
    )
    web_max_preview_chars = int(
        os.environ.get(
            "RSI_WEB_MAX_PREVIEW_CHARS",
            str(DEFAULT_WEB_MAX_PREVIEW_CHARS),
        ).strip()
    )
    web_max_redirects = int(
        os.environ.get(
            "RSI_WEB_MAX_REDIRECTS",
            str(DEFAULT_WEB_MAX_REDIRECTS),
        ).strip()
    )
    web_timeout_seconds = float(
        os.environ.get(
            "RSI_WEB_TIMEOUT_SECONDS",
            str(DEFAULT_WEB_TIMEOUT_SECONDS),
        ).strip()
    )
    return FetcherSettings(
        service_name="fetcher",
        stage="stage6_read_only_browser",
        fetcher_url=os.environ.get("RSI_FETCHER_URL", DEFAULT_FETCHER_URL).strip(),
        egress_url=os.environ.get("RSI_EGRESS_URL", DEFAULT_EGRESS_URL).strip(),
        allowlist_hosts=_split_csv(
            os.environ.get("RSI_WEB_ALLOWLIST_HOSTS"),
            DEFAULT_WEB_ALLOWLIST_HOSTS,
        ),
        private_test_hosts=_split_csv(
            os.environ.get("RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS"),
            (),
        ),
        allowed_content_types=_split_csv(
            os.environ.get("RSI_FETCH_ALLOWED_CONTENT_TYPES"),
            DEFAULT_FETCH_ALLOWED_CONTENT_TYPES,
        ),
        max_response_bytes=web_max_response_bytes,
        max_preview_chars=web_max_preview_chars,
        max_redirects=web_max_redirects,
        timeout_seconds=web_timeout_seconds,
        user_agent=os.environ.get(
            "RSI_FETCH_USER_AGENT",
            DEFAULT_FETCH_USER_AGENT,
        ).strip(),
        enable_private_test_hosts=_env_flag("RSI_FETCH_ENABLE_PRIVATE_TEST_HOSTS", default=True),
    )


def browser_settings() -> BrowserSettings:
    browser_timeout_seconds = float(
        os.environ.get(
            "RSI_BROWSER_TIMEOUT_SECONDS",
            str(DEFAULT_BROWSER_TIMEOUT_SECONDS),
        ).strip()
    )
    browser_viewport_width = int(
        os.environ.get(
            "RSI_BROWSER_VIEWPORT_WIDTH",
            str(DEFAULT_BROWSER_VIEWPORT_WIDTH),
        ).strip()
    )
    browser_viewport_height = int(
        os.environ.get(
            "RSI_BROWSER_VIEWPORT_HEIGHT",
            str(DEFAULT_BROWSER_VIEWPORT_HEIGHT),
        ).strip()
    )
    browser_settle_time_ms = int(
        os.environ.get(
            "RSI_BROWSER_SETTLE_TIME_MS",
            str(DEFAULT_BROWSER_SETTLE_TIME_MS),
        ).strip()
    )
    browser_max_rendered_text_bytes = int(
        os.environ.get(
            "RSI_BROWSER_MAX_RENDERED_TEXT_BYTES",
            str(DEFAULT_BROWSER_MAX_RENDERED_TEXT_BYTES),
        ).strip()
    )
    browser_max_screenshot_bytes = int(
        os.environ.get(
            "RSI_BROWSER_MAX_SCREENSHOT_BYTES",
            str(DEFAULT_BROWSER_MAX_SCREENSHOT_BYTES),
        ).strip()
    )
    browser_max_followable_links = int(
        os.environ.get(
            "RSI_BROWSER_MAX_FOLLOWABLE_LINKS",
            str(DEFAULT_BROWSER_MAX_FOLLOWABLE_LINKS),
        ).strip()
    )
    browser_max_follow_hops = int(
        os.environ.get(
            "RSI_BROWSER_MAX_FOLLOW_HOPS",
            str(DEFAULT_BROWSER_MAX_FOLLOW_HOPS),
        ).strip()
    )
    assert browser_timeout_seconds > 0
    assert browser_viewport_width > 0
    assert browser_viewport_height > 0
    assert browser_settle_time_ms >= 0
    assert browser_max_rendered_text_bytes > 0
    assert browser_max_screenshot_bytes > 0
    assert browser_max_followable_links > 0
    assert browser_max_follow_hops == 1

    return BrowserSettings(
        service_name="browser",
        stage="stage6_read_only_browser",
        browser_url=os.environ.get("RSI_BROWSER_URL", DEFAULT_BROWSER_URL).strip(),
        egress_url=os.environ.get("RSI_EGRESS_URL", DEFAULT_EGRESS_URL).strip(),
        allowlist_hosts=_split_csv(
            os.environ.get("RSI_WEB_ALLOWLIST_HOSTS"),
            DEFAULT_WEB_ALLOWLIST_HOSTS,
        ),
        private_test_hosts=_split_csv(
            os.environ.get("RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS"),
            (),
        ),
        max_redirects=int(
            os.environ.get(
                "RSI_WEB_MAX_REDIRECTS",
                str(DEFAULT_WEB_MAX_REDIRECTS),
            ).strip()
        ),
        timeout_seconds=browser_timeout_seconds,
        viewport_width=browser_viewport_width,
        viewport_height=browser_viewport_height,
        settle_time_ms=browser_settle_time_ms,
        max_rendered_text_bytes=browser_max_rendered_text_bytes,
        max_screenshot_bytes=browser_max_screenshot_bytes,
        max_followable_links=browser_max_followable_links,
        max_follow_hops=browser_max_follow_hops,
        enable_private_test_hosts=_env_flag(
            "RSI_FETCH_ENABLE_PRIVATE_TEST_HOSTS",
            default=True,
        ),
    )


def agent_settings() -> AgentSettings:
    bridge_url = os.environ.get("RSI_BRIDGE_URL", DEFAULT_BRIDGE_URL).strip()
    assert bridge_url, "RSI_BRIDGE_URL must not be empty"

    return AgentSettings(
        service_name="agent",
        stage="stage6_read_only_browser",
        bridge_url=bridge_url,
        workspace_dir=_resolve_path(
            os.environ.get("RSI_AGENT_WORKSPACE_DIR"),
            DEFAULT_AGENT_WORKSPACE_DIR,
        ),
        runtime_code_dir=_resolve_path(
            os.environ.get("RSI_AGENT_RUNTIME_CODE_DIR"),
            DEFAULT_AGENT_RUNTIME_CODE_DIR,
        ),
        public_probe_url=os.environ.get(
            "RSI_PUBLIC_PROBE_URL",
            DEFAULT_PUBLIC_PROBE_URL,
        ).strip(),
        provider_probe_url=os.environ.get(
            "RSI_PROVIDER_PROBE_URL",
            DEFAULT_PROVIDER_PROBE_URL,
        ).strip(),
    )


def egress_settings() -> EgressSettings:
    web_max_redirects = int(
        os.environ.get(
            "RSI_WEB_MAX_REDIRECTS",
            str(DEFAULT_WEB_MAX_REDIRECTS),
        ).strip()
    )
    web_timeout_seconds = float(
        os.environ.get(
            "RSI_WEB_TIMEOUT_SECONDS",
            str(DEFAULT_WEB_TIMEOUT_SECONDS),
        ).strip()
    )
    assert web_max_redirects >= 0
    assert web_timeout_seconds > 0
    return EgressSettings(
        service_name="egress",
        stage="stage6_read_only_browser",
        egress_url=os.environ.get("RSI_EGRESS_URL", DEFAULT_EGRESS_URL).strip(),
        allowlist_hosts=_split_csv(
            os.environ.get("RSI_WEB_ALLOWLIST_HOSTS"),
            DEFAULT_WEB_ALLOWLIST_HOSTS,
        ),
        private_test_hosts=_split_csv(
            os.environ.get("RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS"),
            (),
        ),
        timeout_seconds=web_timeout_seconds,
        allowed_content_types=_split_csv(
            os.environ.get("RSI_FETCH_ALLOWED_CONTENT_TYPES"),
            DEFAULT_FETCH_ALLOWED_CONTENT_TYPES,
        ),
        user_agent=os.environ.get(
            "RSI_FETCH_USER_AGENT",
            DEFAULT_FETCH_USER_AGENT,
        ).strip(),
        max_redirects=web_max_redirects,
        enable_private_test_hosts=_env_flag(
            "RSI_FETCH_ENABLE_PRIVATE_TEST_HOSTS",
            default=True,
        ),
        test_ip_overrides=_csv_mapping(os.environ.get("RSI_EGRESS_TEST_IP_OVERRIDES")),
    )
