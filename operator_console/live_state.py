from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from shared.schemas import ProposalRecord


ACTIVE_STATUSES = {"starting", "running"}
BROWSER_TOOLS = {"bridge_browser_render", "bridge_browser_follow_href"}


@dataclass(frozen=True)
class LaunchDiagnostic:
    severity: str
    title: str
    body: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LaunchPhaseSummary:
    phase_label: str
    phase_tone: str
    headline: str
    next_action: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TimelineDisplayItem:
    timestamp: str
    event_kind: str
    run_id: str
    step_index: int | None
    tool_name: str | None
    summary: dict
    title: str
    summary_text: str
    tone: str

    def to_dict(self) -> dict:
        return asdict(self)


def build_live_snapshot(
    snapshot: dict,
    *,
    related_proposals: list[ProposalRecord] | None = None,
    allowlist_hosts: list[str] | None = None,
    bridge_error: str = "",
) -> dict:
    launch = dict(snapshot.get("launch", {}))
    proposal_dicts = [
        proposal.model_dump() if hasattr(proposal, "model_dump") else dict(proposal)
        for proposal in (related_proposals or [])
    ]
    recent_screenshots = _coerce_screenshots(snapshot)
    current_screenshot = recent_screenshots[0] if recent_screenshots else None
    timeline = [
        build_timeline_display_item(event).to_dict()
        for event in reversed(snapshot.get("timeline", []))
        if isinstance(event, dict)
    ]
    diagnostics = _build_diagnostics(
        launch,
        snapshot=snapshot,
        related_proposals=proposal_dicts,
        allowlist_hosts=allowlist_hosts or [],
        bridge_error=bridge_error,
    )
    phase = _build_phase_summary(launch, timeline=timeline, related_proposals=proposal_dicts)
    version_token = _version_token(
        snapshot,
        phase=phase,
        diagnostics=diagnostics,
        proposal_dicts=proposal_dicts,
        current_screenshot=current_screenshot,
        bridge_error=bridge_error,
    )
    return {
        **snapshot,
        "timeline": timeline,
        "related_proposals": proposal_dicts,
        "bridge_error": bridge_error,
        "recent_screenshots": recent_screenshots,
        "current_screenshot": current_screenshot,
        "latest_screenshot": current_screenshot,
        "diagnostics": [item.to_dict() for item in diagnostics],
        **phase.to_dict(),
        "version_token": version_token,
    }


def build_timeline_display_item(event: dict) -> TimelineDisplayItem:
    summary = event.get("summary")
    if not isinstance(summary, dict):
        summary = {}

    tool_name = event.get("tool_name")
    event_kind = str(event.get("event_kind", ""))
    title = "Recorded event"
    summary_text = "The bridge recorded another event for this run."
    tone = "ok"

    if event_kind == "run_start":
        task = str(summary.get("task", "")).strip()
        title = "Run started"
        summary_text = f"Started task: {task}" if task else "The seeded run has started."
        tone = "warn"
    elif tool_name in BROWSER_TOOLS and _event_looks_failed(summary):
        title = "Step failed"
        summary_text = _failure_summary(summary)
        tone = "bad"
    elif tool_name == "bridge_browser_render":
        url = str(summary.get("final_url") or summary.get("normalized_url") or "")
        page_title = str(summary.get("page_title", "")).strip()
        status = summary.get("http_status")
        title = "Opened page"
        summary_text = _trim(
            f"Loaded {page_title or url or 'the page'}"
            + (f" ({status})" if status else "")
            + (f" from {url}" if url and page_title else ""),
            limit=180,
        )
    elif tool_name == "bridge_browser_follow_href":
        target = str(summary.get("final_url") or summary.get("requested_target_url") or "")
        label = str(summary.get("matched_link_text", "")).strip()
        status = summary.get("http_status")
        title = "Followed link"
        summary_text = _trim(
            f"Followed {label or 'a link'} to {target or 'the target page'}"
            + (f" ({status})" if status else ""),
            limit=180,
        )
    elif tool_name == "bridge_create_proposal":
        title = "Prepared approval request"
        target = str(summary.get("target_url", "")).strip()
        proposal_id = str(summary.get("proposal_id", "")).strip()
        summary_text = _trim(
            f"Created proposal {proposal_id or 'for a consequential action'}"
            + (f" targeting {target}." if target else "."),
            limit=180,
        )
        tone = "warn"
    elif tool_name == "write_binary_base64":
        path = str(summary.get("path", "")).strip()
        title = "Saved binary artifact"
        if path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            title = "Saved screenshot"
        summary_text = f"Wrote {path or 'a binary artifact'} to the workspace."
    elif tool_name == "write_file":
        path = str(summary.get("path", "")).strip()
        title = "Saved file"
        summary_text = f"Wrote {path or 'a text file'} to the workspace."
    elif event_kind == "run_end":
        success = bool(summary.get("success"))
        title = "Run finished" if success else "Run ended with error"
        finished_reason = str(summary.get("finished_reason", "")).strip()
        finish_summary = str(summary.get("finish_summary", "")).strip()
        summary_text = finish_summary or finished_reason or "The seeded run reached its end state."
        tone = "ok" if success else "bad"
    elif _event_looks_failed(summary):
        title = "Step failed"
        summary_text = _failure_summary(summary)
        tone = "bad"
    elif tool_name == "bridge_status":
        stage = str(summary.get("stage", "")).strip()
        title = "Checked bridge status"
        summary_text = f"Bridge reported {stage or 'the current stage'} and current budget state."
    else:
        title = _humanize_tool_name(tool_name or event_kind or "event")
        preview = str(summary.get("content_preview", "")).strip()
        summary_text = _trim(preview or "The run recorded another step.", limit=180)

    return TimelineDisplayItem(
        timestamp=str(event.get("timestamp", "")),
        event_kind=event_kind,
        run_id=str(event.get("run_id", "")),
        step_index=event.get("step_index"),
        tool_name=tool_name,
        summary=summary,
        title=title,
        summary_text=summary_text,
        tone=tone,
    )


def _build_phase_summary(
    launch: dict,
    *,
    timeline: list[dict],
    related_proposals: list[dict],
) -> LaunchPhaseSummary:
    statuses = {proposal.get("status", "") for proposal in related_proposals}
    launch_status = str(launch.get("status", ""))
    run_id = str(launch.get("run_id") or "")

    if launch_status == "failed":
        return LaunchPhaseSummary(
            phase_label="failed",
            phase_tone="bad",
            headline="The launch failed before it produced a clean result.",
            next_action="Read the diagnostics below, then inspect the launch log tail before rerunning.",
        )
    if "executed" in statuses:
        return LaunchPhaseSummary(
            phase_label="executed",
            phase_tone="ok",
            headline="The consequential action has already been executed.",
            next_action="Review the execution result and any follow-up artifacts from this run.",
        )
    if "approved" in statuses:
        return LaunchPhaseSummary(
            phase_label="ready_to_execute",
            phase_tone="warn",
            headline="Approval is recorded and the external action is ready to execute.",
            next_action="Execute the approved proposal when you want the external action to run.",
        )
    if "pending" in statuses:
        return LaunchPhaseSummary(
            phase_label="waiting_for_approval",
            phase_tone="warn",
            headline="The agent is paused at an approval gate.",
            next_action="Review the pending proposal below and approve or reject it.",
        )
    if not run_id and launch_status in ACTIVE_STATUSES:
        return LaunchPhaseSummary(
            phase_label="starting",
            phase_tone="warn",
            headline="The launch is starting and the console is waiting for the first run event.",
            next_action="Leave this page open; a live timeline will appear once run_start is recorded.",
        )
    if launch_status == "finished":
        return LaunchPhaseSummary(
            phase_label="finished",
            phase_tone="ok",
            headline="The run finished without a pending operator action.",
            next_action="Review the saved run summary and artifacts, or start another run.",
        )
    if _latest_browser_event(timeline) is not None:
        return LaunchPhaseSummary(
            phase_label="browsing",
            phase_tone="ok",
            headline="The agent is reading a page and collecting browser output.",
            next_action="Watch for a screenshot or the next step summary.",
        )
    return LaunchPhaseSummary(
        phase_label="answering",
        phase_tone="ok",
        headline="The agent is turning the captured material into workspace outputs.",
        next_action="Wait for the run to finish or surface an approval request.",
    )


def _build_diagnostics(
    launch: dict,
    *,
    snapshot: dict,
    related_proposals: list[dict],
    allowlist_hosts: list[str],
    bridge_error: str,
) -> list[LaunchDiagnostic]:
    diagnostics: list[LaunchDiagnostic] = []
    input_host = urlparse(str(launch.get("input_url", ""))).hostname or ""

    if bridge_error:
        diagnostics.append(
            LaunchDiagnostic(
                severity="bad",
                title="Bridge unavailable",
                body="Live trusted data is unavailable and operator actions may not work until bridge access is restored.",
            )
        )

    if (
        str(launch.get("script", "")) == "stage8_real_site_approval_demo.json"
        and not str(launch.get("proposal_target_url", "")).strip()
    ):
        diagnostics.append(
            LaunchDiagnostic(
                severity="warn",
                title="Proposal target missing",
                body="This script can browse, but it cannot create a useful approval request without a POST destination.",
            )
        )

    if (
        input_host
        and input_host not in allowlist_hosts
        and _has_forbidden_signal(snapshot, launch)
    ):
        diagnostics.append(
            LaunchDiagnostic(
                severity="bad",
                title="Host not allowlisted",
                body=f"The requested host {input_host} is blocked by the current web policy. Add it to the allowlist before rerunning this launch.",
            )
        )

    if str(launch.get("status", "")) == "failed" and not str(launch.get("summary_path", "")).strip():
        diagnostics.append(
            LaunchDiagnostic(
                severity="bad",
                title="Launch failed before summary",
                body="The seed runner exited early and did not leave a final run summary. Inspect the launch log tail before rerunning.",
            )
        )

    statuses = {proposal.get("status", "") for proposal in related_proposals}
    if "pending" in statuses:
        diagnostics.append(
            LaunchDiagnostic(
                severity="warn",
                title="Waiting for approval",
                body="The agent has finished its current run segment and now needs operator action to continue.",
            )
        )
    elif "approved" in statuses:
        diagnostics.append(
            LaunchDiagnostic(
                severity="warn",
                title="Ready to execute",
                body="The approval is in place and the operator can execute the pending consequential action now.",
            )
        )

    return diagnostics


def _coerce_screenshots(snapshot: dict) -> list[dict]:
    recent = snapshot.get("recent_screenshots")
    if isinstance(recent, list) and recent:
        return [dict(item) for item in recent if isinstance(item, dict)]

    latest = snapshot.get("current_screenshot") or snapshot.get("latest_screenshot")
    if isinstance(latest, dict):
        return [dict(latest)]
    return []


def _latest_browser_event(timeline: list[dict]) -> dict | None:
    for item in timeline:
        if item.get("tool_name") in BROWSER_TOOLS:
            return item
    return None


def _event_looks_failed(summary: dict) -> bool:
    status = summary.get("http_status")
    if isinstance(status, int) and status >= 400:
        return True
    detail = str(summary.get("detail", "")).strip()
    return "403 Forbidden" in detail or "HTTPStatusError" in detail or "error" in detail.lower()


def _has_forbidden_signal(snapshot: dict, launch: dict) -> bool:
    text = "\n".join(
        [
            str(launch.get("error", "")),
            str(snapshot.get("log_tail", "")),
        ]
    )
    if "403 Forbidden" in text or "HTTPStatusError" in text:
        return True
    for event in snapshot.get("timeline", []):
        if not isinstance(event, dict):
            continue
        summary = event.get("summary")
        if not isinstance(summary, dict):
            continue
        status = summary.get("http_status")
        if status == 403:
            return True
        detail = str(summary.get("detail", ""))
        if "403 Forbidden" in detail or "HTTPStatusError" in detail:
            return True
    return False


def _failure_summary(summary: dict) -> str:
    detail = str(summary.get("detail", "")).strip()
    if detail:
        return _trim(detail, limit=180)
    status = summary.get("http_status")
    if status:
        return f"The step returned HTTP {status}."
    return "The bridge recorded a failed step."


def _humanize_tool_name(name: str) -> str:
    text = name.replace("_", " ").strip()
    if text.startswith("bridge "):
        text = text.replace("bridge ", "", 1)
    return text[:1].upper() + text[1:] if text else "Event"


def _trim(text: str, *, limit: int) -> str:
    stripped = " ".join(text.split())
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 3].rstrip() + "..."


def _version_token(
    snapshot: dict,
    *,
    phase: LaunchPhaseSummary,
    diagnostics: list[LaunchDiagnostic],
    proposal_dicts: list[dict],
    current_screenshot: dict | None,
    bridge_error: str,
) -> str:
    proposal_statuses = ",".join(
        f"{proposal.get('proposal_id', '')}:{proposal.get('status', '')}"
        for proposal in proposal_dicts
    )
    diagnostic_titles = ",".join(item.title for item in diagnostics)
    screenshot_path = current_screenshot.get("relative_path", "") if current_screenshot else ""
    return "|".join(
        [
            str(snapshot.get("version_token", "")),
            phase.phase_label,
            proposal_statuses,
            diagnostic_titles,
            screenshot_path,
            bridge_error,
        ]
    )
