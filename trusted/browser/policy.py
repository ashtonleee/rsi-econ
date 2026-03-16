from shared.schemas import BrowserFollowLink
from trusted.web.policy import WebPolicy, WebPolicyError, normalize_web_target


def validate_browser_target(url: str, policy: WebPolicy):
    return normalize_web_target(url, policy)


def classify_browser_channel(
    *,
    resource_type: str,
    is_navigation_request: bool,
    is_main_frame: bool,
    headers: dict[str, str],
    top_level_started: bool,
) -> str:
    lowered_headers = {key.lower(): value.lower() for key, value in headers.items()}
    if is_navigation_request:
        if is_main_frame:
            return "redirect" if top_level_started else "top_level_navigation"
        return "frame_navigation"
    if resource_type in {"fetch", "xhr"}:
        return "fetch_xhr"
    if lowered_headers.get("purpose") == "prefetch" or lowered_headers.get("sec-purpose") == "prefetch":
        return "prefetch_preconnect"
    return "subresource"


def browser_channel_violation(channel: str, detail: str) -> WebPolicyError:
    return WebPolicyError(f"{channel}_not_allowed", detail or channel)


def popup_violation(url: str) -> WebPolicyError:
    return WebPolicyError("popup_not_allowed", url or "about:blank")


def download_violation(url: str, *, suggested_filename: str | None) -> WebPolicyError:
    detail = url or "download"
    if suggested_filename:
        detail = f"{detail} -> {suggested_filename}"
    return WebPolicyError("download_not_allowed", detail)


def top_level_navigation_violation(url: str) -> WebPolicyError:
    return WebPolicyError("top_level_navigation_not_allowed", url or "about:blank")


def filechooser_violation(detail: str) -> WebPolicyError:
    return WebPolicyError("upload_not_allowed", detail or "file_chooser")


def select_followable_link(
    target_url: str,
    followable_links: list[BrowserFollowLink],
) -> BrowserFollowLink:
    for link in followable_links:
        if link.target_url == target_url:
            return link
    raise WebPolicyError("requested_target_not_present", target_url)
