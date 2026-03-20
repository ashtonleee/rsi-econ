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
    browser_session = dict(raw_snapshot.get("browser_session", {}))
    workspace_state = dict(raw_snapshot.get("workspace_state", {}))
    proposal = _session_proposal(session, workspace_state)
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
    capability_profile = str(session.get("capability_profile", "bounded_packet") or "bounded_packet")
    phase_label, phase_tone, headline, next_action = _phase_summary(session, transcript, browser_session)
    screenshots = [
        item
        for item in raw_snapshot.get("recent_screenshots", [])
        if isinstance(item, dict)
    ]
    related_artifacts = [
        item
        for item in raw_snapshot.get("related_artifacts", [])
        if isinstance(item, dict)
    ]
    current_screenshot = raw_snapshot.get("current_screenshot")
    if not isinstance(current_screenshot, dict):
        current_screenshot = screenshots[0] if screenshots else None
    result = _result_summary(
        session=session,
        transcript=transcript,
        summary_url=str(raw_snapshot.get("summary_url", "")),
        related_artifacts=related_artifacts,
    )
    version_token = "|".join(
        [
            str(session.get("status", "")),
            str(session.get("updated_at", "")),
            str(raw_snapshot.get("summary_url", "")),
            str(proposal.get("status", "")),
            str(browser_session.get("session_id", "")),
            str(browser_session.get("snapshot_id", "")),
            str(browser_session.get("current_url", "")),
            str((current_screenshot or {}).get("relative_path", "")),
            str(len(transcript)),
            str(raw_snapshot.get("log_tail", "")),
        ]
    )
    return {
        **raw_snapshot,
        "workspace_state": workspace_state,
        "browser_session": browser_session,
        "proposal": proposal,
        "transcript": transcript,
        "recent_screenshots": screenshots,
        "related_artifacts": related_artifacts,
        "current_screenshot": current_screenshot,
        "diagnostics": [item.to_dict() for item in diagnostics],
        "result": result,
        "boundary_note": (
            "This session uses trusted browser mediation for public workflow browsing. It shows observable actions and outputs, not unrestricted browsing or hidden reasoning."
            if capability_profile == "workflow_browser_public"
            else "This session uses trusted browser mediation on allowlisted sites. It shows observable actions and outputs, not unrestricted browsing or hidden reasoning."
        ),
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
        elif tool == "bridge_browser_session_open":
            summary = f"Opened interactive browser session on {result.get('current_url') or 'a page'}."
        elif tool == "bridge_browser_session_snapshot":
            summary = f"Refreshed the interactive browser snapshot for {result.get('current_url') or 'the current page'}."
        elif tool == "bridge_browser_session_click":
            summary = f"Clicked an interactive element and now sees {result.get('current_url') or 'the updated page'}."
            if result.get("outcome") == "proposal_required":
                summary = "Clicked an interactive element and paused a real browser request for approval."
                tone = "warn"
        elif tool == "bridge_browser_session_navigate":
            summary = f"Navigated the trusted browser to {result.get('current_url') or 'the requested page'}."
        elif tool == "bridge_browser_session_fill":
            summary = "Filled a form field."
        elif tool == "bridge_browser_session_type":
            summary = "Typed into a form field."
        elif tool == "bridge_browser_session_select":
            summary = "Updated a select field."
            if result.get("outcome") == "proposal_required":
                summary = "Updated a select field and paused a real browser request for approval."
                tone = "warn"
        elif tool == "bridge_browser_session_set_checked":
            summary = "Updated a checkbox or radio field."
            if result.get("outcome") == "proposal_required":
                summary = "Updated a checkbox or radio field and paused a real browser request for approval."
                tone = "warn"
        elif tool == "bridge_browser_session_press":
            summary = "Pressed a key in the trusted browser."
            if result.get("outcome") == "proposal_required":
                summary = "Pressed a key and paused a real browser request for approval."
                tone = "warn"
        elif tool == "bridge_browser_session_hover":
            summary = "Hovered an element in the trusted browser."
        elif tool == "bridge_browser_session_wait_for":
            summary = "Waited for the page to settle."
        elif tool == "bridge_browser_session_back":
            summary = "Moved back in trusted browser history."
        elif tool == "bridge_browser_session_forward":
            summary = "Moved forward in trusted browser history."
        elif tool == "bridge_browser_session_new_tab":
            summary = "Opened a new trusted browser tab."
        elif tool == "bridge_browser_session_switch_tab":
            summary = "Switched the active trusted browser tab."
        elif tool == "bridge_browser_session_close_tab":
            summary = "Closed a trusted browser tab."
        elif tool == "bridge_browser_submit_proposal":
            summary = f"Created browser submit proposal {result.get('proposal_id', '')} and paused for approval."
            tone = "warn"
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


def _phase_summary(session: dict, transcript: list[dict], browser_session: dict) -> tuple[str, str, str, str]:
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
        if (proposal.get("action_type") if isinstance(proposal, dict) else "") == "browser_submit":
            return (
                "running",
                "ok",
                "The approved browser submit executed and the session is continuing on the resulting page.",
                "Watch the live page packet and transcript for the next answer step.",
            )
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
        if (proposal.get("action_type") if isinstance(proposal, dict) else "") == "browser_submit":
            return (
                "waiting_for_approval",
                "warn",
                "The session prepared a real form submit and is paused for operator approval.",
                "Review the target and field preview, then approve or reject the submit.",
            )
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
    if any(
        item.get("tool") in {
            "bridge_browser_render",
            "bridge_browser_follow_href",
            "bridge_browser_session_open",
            "bridge_browser_session_snapshot",
            "bridge_browser_session_click",
            "bridge_browser_session_type",
            "bridge_browser_session_select",
            "bridge_browser_session_set_checked",
        }
        for item in transcript[-4:]
    ):
        return (
            "running",
            "ok",
            "The session is actively working inside the interactive browser packet.",
            "Watch the preview, interactables, and transcript for the next step.",
        )
    if browser_session.get("session_id"):
        return (
            "running",
            "ok",
            "The session has an active trusted browser session open.",
            "Watch for the next interactive browser action or approval gate.",
        )
    return (
        "running" if status in {"starting", "running"} else status or "starting",
        "warn" if status in {"starting", "running"} else "ok",
        "The session is building its next action.",
        "Wait for the transcript to update or the next approval gate to appear.",
    )


def _session_proposal(session: dict, workspace_state: dict) -> dict:
    proposal = workspace_state.get("last_proposal", {})
    if not isinstance(proposal, dict):
        proposal = {}
    if proposal:
        return proposal
    last_proposal_id = str(session.get("last_proposal_id", "")).strip()
    return {"proposal_id": last_proposal_id} if last_proposal_id else {}


def _result_summary(
    *,
    session: dict,
    transcript: list[dict],
    summary_url: str,
    related_artifacts: list[dict],
) -> dict:
    if summary_url:
        return {
            "title": "Latest Result",
            "body": "A session result is available. Open the saved summary to review the current answer and final state.",
            "link_url": summary_url,
            "link_label": "Open Session Result",
            "artifact_name": "",
        }
    for artifact in related_artifacts:
        if artifact.get("name") == "final_answer.md":
            return {
                "title": "Latest Result",
                "body": "The session wrote a final answer artifact.",
                "link_url": artifact.get("url", ""),
                "link_label": "Open Final Answer",
                "artifact_name": artifact.get("name", ""),
            }
    if related_artifacts:
        artifact = related_artifacts[0]
        return {
            "title": "Latest Result",
            "body": "The session has written workspace output, even if a final answer is not ready yet.",
            "link_url": artifact.get("url", ""),
            "link_label": f"Open {artifact.get('name', 'latest artifact')}",
            "artifact_name": artifact.get("name", ""),
        }
    if transcript:
        latest = transcript[-1]
        return {
            "title": "No result yet",
            "body": latest.get("summary_text", "The session is still acting and has not written a result yet."),
            "link_url": "",
            "link_label": "",
            "artifact_name": "",
        }
    return {
        "title": "No result yet",
        "body": "The session is still acting. Watch the Actions and Transcript sections for the next visible step.",
        "link_url": "",
        "link_label": "",
        "artifact_name": "",
    }


def _build_diagnostics(*, session: dict, transcript: list[dict], allowlist_hosts: list[str], bridge_error: str) -> list[SessionDiagnostic]:
    diagnostics: list[SessionDiagnostic] = []
    capability_profile = str(session.get("capability_profile", "bounded_packet") or "bounded_packet")
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
        if capability_profile != "workflow_browser_public" and host and allowlist_hosts and host not in allowlist_hosts:
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
            if proposal.get("action_type") == "browser_submit":
                target_url = ((proposal.get("action_payload") or {}).get("target_url", "") if isinstance(proposal.get("action_payload"), dict) else "")
                diagnostics.append(
                    SessionDiagnostic(
                        severity="warn",
                        title="Waiting for browser submit approval",
                        body=f"The session prepared a submit to {target_url or 'the current form target'} and is paused for operator approval.",
                    )
                )
                return diagnostics
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
