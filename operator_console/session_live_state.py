from dataclasses import asdict, dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class SessionDiagnostic:
    severity: str
    title: str
    body: str

    def to_dict(self) -> dict:
        return asdict(self)


def build_session_snapshot(
    raw_snapshot: dict,
    *,
    allowlist_hosts: list[str] | None = None,
    bridge_error: str = "",
) -> dict:
    session = dict(raw_snapshot.get("session", {}))
    transcript = [
        _display_transcript_item(item)
        for item in raw_snapshot.get("transcript", [])
        if isinstance(item, dict)
    ]
    diagnostics = _build_diagnostics(
        session=session,
        transcript=transcript,
        allowlist_hosts=allowlist_hosts or raw_snapshot.get("allowlist_hosts", []) or [],
        bridge_error=bridge_error or raw_snapshot.get("bridge_error", ""),
    )
    phase_label, phase_tone, headline, next_action = _phase_summary(session, transcript)
    screenshots = [
        item
        for item in raw_snapshot.get("recent_screenshots", [])
        if isinstance(item, dict)
    ]
    current_screenshot = raw_snapshot.get("current_screenshot")
    if not isinstance(current_screenshot, dict):
        current_screenshot = screenshots[0] if screenshots else None
    version_token = "|".join(
        [
            str(session.get("status", "")),
            str(session.get("updated_at", "")),
            str(raw_snapshot.get("summary_url", "")),
            str((current_screenshot or {}).get("relative_path", "")),
            str(len(transcript)),
            str(raw_snapshot.get("log_tail", "")),
        ]
    )
    return {
        **raw_snapshot,
        "transcript": transcript,
        "recent_screenshots": screenshots,
        "current_screenshot": current_screenshot,
        "diagnostics": [item.to_dict() for item in diagnostics],
        "phase_label": phase_label,
        "phase_tone": phase_tone,
        "headline": headline,
        "next_action": next_action,
        "version_token": version_token,
    }


def _display_transcript_item(item: dict) -> dict:
    kind = str(item.get("kind", ""))
    tool = str(item.get("tool", ""))
    result = item.get("result", {})
    title = "Session update"
    summary = str(item.get("summary", "")).strip() or "The session recorded another event."
    tone = "ok"
    if kind == "model_action":
        title = f"Planned {tool or 'next step'}"
        summary = str(item.get("reason", "")).strip() or "The model selected the next step."
        tone = "warn"
    elif kind == "tool_result":
        title = tool or "Tool result"
        if tool == "bridge_browser_render":
            summary = f"Opened {result.get('final_url') or result.get('normalized_url') or 'a page'}."
        elif tool == "bridge_browser_follow_href":
            summary = f"Followed to {result.get('final_url') or result.get('requested_target_url') or 'a page'}."
        elif tool == "bridge_create_proposal":
            summary = f"Created proposal {result.get('proposal_id', '')} and paused for approval."
            tone = "warn"
        elif tool == "write_file":
            summary = f"Wrote {result.get('path', 'a file')}."
        elif tool == "bridge_status":
            summary = f"Bridge reported stage {result.get('stage', 'unknown')}."
    elif kind == "finish":
        title = "Session finished"
        summary = str(item.get("summary", "")).strip() or "The session finished."
    elif kind == "error":
        title = "Session error"
        summary = str(item.get("detail", "")).strip() or "The session failed."
        tone = "bad"
    elif kind == "operator_state":
        title = "Operator state update"
        tone = "warn"
    return {
        **item,
        "title": title,
        "summary_text": summary,
        "tone": tone,
    }


def _phase_summary(session: dict, transcript: list[dict]) -> tuple[str, str, str, str]:
    status = str(session.get("status", ""))
    proposal = session.get("last_proposal", {})
    proposal_status = proposal.get("status") if isinstance(proposal, dict) else ""
    if status == "failed":
        return (
            "failed",
            "bad",
            "The session failed before it reached a clean answer.",
            "Read the diagnostics and log tail, then decide whether to retry.",
        )
    if proposal_status == "executed":
        return (
            "running",
            "ok",
            "The approved action executed and the session is continuing.",
            "Leave the page open while the next session turn finishes.",
        )
    if proposal_status == "approved":
        return (
            "waiting_for_approval",
            "warn",
            "Approval is recorded and the session is waiting for execute.",
            "Execute the approved proposal to let the session continue.",
        )
    if proposal_status == "pending" or status == "waiting_for_approval":
        return (
            "waiting_for_approval",
            "warn",
            "The session is paused for operator approval.",
            "Approve or reject the pending proposal below.",
        )
    if status == "finished":
        return (
            "finished",
            "ok",
            "The session finished and wrote a final answer.",
            "Review the final answer and any saved artifacts.",
        )
    if status == "resuming":
        return (
            "resuming",
            "warn",
            "The operator console is relaunching the next session turn.",
            "Wait for the transcript to resume and the next browser packet to arrive.",
        )
    if any(item.get("tool") in {"bridge_browser_render", "bridge_browser_follow_href"} for item in transcript[-3:]):
        return (
            "running",
            "ok",
            "The session is reading a page through the packet browser.",
            "Watch the preview and transcript for the next step.",
        )
    return (
        "running" if status in {"starting", "running"} else status or "starting",
        "warn" if status in {"starting", "running"} else "ok",
        "The session is building its next action.",
        "Wait for the transcript to update or the next approval gate to appear.",
    )


def _build_diagnostics(*, session: dict, transcript: list[dict], allowlist_hosts: list[str], bridge_error: str) -> list[SessionDiagnostic]:
    diagnostics: list[SessionDiagnostic] = []
    if bridge_error:
        diagnostics.append(
            SessionDiagnostic(
                severity="bad",
                title="Bridge unavailable",
                body="Live trusted data is unavailable and proposal actions may not work until the bridge can be reached.",
            )
        )
    input_url = str(session.get("input_url", ""))
    if input_url:
        host = urlparse(input_url).hostname or ""
        if host and allowlist_hosts and host not in allowlist_hosts:
            if any("403" in json_blob(item) for item in transcript[-4:]):
                diagnostics.append(
                    SessionDiagnostic(
                        severity="bad",
                        title="Host not allowlisted",
                        body=f"The requested host {host} is blocked by the current web allowlist.",
                    )
                )
    if str(session.get("proposal_target_url", "")) == "" and "approval" in str(session.get("task", "")).lower():
        diagnostics.append(
            SessionDiagnostic(
                severity="warn",
                title="Proposal target missing",
                body="This session can browse, but it cannot submit a useful approval request without a proposal target URL.",
            )
        )
    if str(session.get("status", "")) == "failed" and not str(session.get("summary_path", "")):
        diagnostics.append(
            SessionDiagnostic(
                severity="bad",
                title="Session failed before summary",
                body="The session exited early before it wrote a summary artifact. Check the log tail for the failing step.",
            )
        )
    proposal = session.get("last_proposal", {})
    if isinstance(proposal, dict):
        if proposal.get("status") == "pending":
            diagnostics.append(
                SessionDiagnostic(
                    severity="warn",
                    title="Waiting for approval",
                    body="The agent has paused until an operator approves or rejects the pending proposal.",
                )
            )
        if proposal.get("status") == "approved":
            diagnostics.append(
                SessionDiagnostic(
                    severity="warn",
                    title="Ready to execute",
                    body="Approval is recorded. Execute the proposal now to let the session continue.",
                )
            )
    return diagnostics


def json_blob(value: dict) -> str:
    try:
        import json

        return json.dumps(value, sort_keys=True)
    except Exception:  # noqa: BLE001
        return str(value)
