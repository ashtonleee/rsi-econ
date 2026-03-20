import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from shared.config import agent_settings
from untrusted.agent.bridge_client import BridgeClient
from untrusted.agent.seed_runner import PlanAction, RunState, SeedRunner


ALLOWED_SESSION_TOOLS = {
    "bridge_status",
    "bridge_chat",
    "bridge_fetch",
    "bridge_browser_render",
    "bridge_browser_follow_href",
    "bridge_browser_session_open",
    "bridge_browser_session_navigate",
    "bridge_browser_session_snapshot",
    "bridge_browser_session_click",
    "bridge_browser_session_fill",
    "bridge_browser_session_type",
    "bridge_browser_session_select",
    "bridge_browser_session_set_checked",
    "bridge_browser_session_press",
    "bridge_browser_session_hover",
    "bridge_browser_session_wait_for",
    "bridge_browser_session_back",
    "bridge_browser_session_forward",
    "bridge_browser_session_new_tab",
    "bridge_browser_session_switch_tab",
    "bridge_browser_session_close_tab",
    "bridge_browser_submit_proposal",
    "bridge_create_proposal",
    "list_files",
    "read_file",
    "write_file",
    "finish",
}


@dataclass(frozen=True)
class SessionToolAction:
    tool: str
    params: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class SessionRunResult:
    session_id: str
    run_id: str
    stop_reason: Literal["finished", "waiting_for_approval", "failed", "max_turns_reached", "budget_exhausted"]
    summary_path: str
    steps_executed: int
    error: str = ""


def validate_session_action(payload: dict[str, Any]) -> SessionToolAction:
    if not isinstance(payload, dict):
        raise ValueError("session action must be a JSON object")
    tool = str(payload.get("tool", "")).strip()
    reason = str(payload.get("reason", "")).strip()
    params = payload.get("params", {})
    if tool not in ALLOWED_SESSION_TOOLS:
        raise ValueError(f"unsupported session tool: {tool}")
    if not isinstance(params, dict):
        raise ValueError("session action params must be an object")
    if not reason:
        raise ValueError("session action reason is required")

    if tool == "bridge_browser_render" and not str(params.get("url", "")).strip():
        raise ValueError("bridge_browser_render requires params.url")
    if tool == "bridge_browser_follow_href":
        if not str(params.get("source_url", "")).strip():
            raise ValueError("bridge_browser_follow_href requires params.source_url")
        if not str(params.get("target_url", "")).strip():
            raise ValueError("bridge_browser_follow_href requires params.target_url")
    if tool == "bridge_chat" and not str(params.get("message", "")).strip():
        raise ValueError("bridge_chat requires params.message")
    if tool == "bridge_fetch" and not str(params.get("url", "")).strip():
        raise ValueError("bridge_fetch requires params.url")
    if tool == "bridge_browser_session_open" and not str(params.get("url", "")).strip():
        raise ValueError("bridge_browser_session_open requires params.url")
    if tool == "bridge_browser_session_navigate" and not str(params.get("url", "")).strip():
        raise ValueError("bridge_browser_session_navigate requires params.url")
    if tool == "bridge_browser_session_snapshot" and not str(params.get("session_id", "")).strip():
        raise ValueError("bridge_browser_session_snapshot requires params.session_id")
    if tool in {
        "bridge_browser_session_navigate",
        "bridge_browser_session_click",
        "bridge_browser_session_fill",
        "bridge_browser_session_type",
        "bridge_browser_session_select",
        "bridge_browser_session_set_checked",
        "bridge_browser_session_hover",
        "bridge_browser_submit_proposal",
    }:
        if not str(params.get("session_id", "")).strip():
            raise ValueError(f"{tool} requires params.session_id")
        if tool != "bridge_browser_session_navigate" and not str(params.get("snapshot_id", "")).strip():
            raise ValueError(f"{tool} requires params.snapshot_id")
        if tool != "bridge_browser_session_navigate" and not str(params.get("element_id", "")).strip():
            raise ValueError(f"{tool} requires params.element_id")
    if tool in {
        "bridge_browser_session_press",
        "bridge_browser_session_wait_for",
        "bridge_browser_session_back",
        "bridge_browser_session_forward",
        "bridge_browser_session_new_tab",
        "bridge_browser_session_switch_tab",
        "bridge_browser_session_close_tab",
    } and not str(params.get("session_id", "")).strip():
        raise ValueError(f"{tool} requires params.session_id")
    if tool == "bridge_browser_session_press" and not str(params.get("key", "")).strip():
        raise ValueError("bridge_browser_session_press requires params.key")
    if tool == "bridge_browser_session_switch_tab" and not str(params.get("tab_id", "")).strip():
        raise ValueError("bridge_browser_session_switch_tab requires params.tab_id")
    if tool == "bridge_browser_session_type" and "text" not in params:
        raise ValueError("bridge_browser_session_type requires params.text")
    if tool == "bridge_browser_session_fill" and "text" not in params:
        raise ValueError("bridge_browser_session_fill requires params.text")
    if tool == "bridge_browser_session_select" and not str(params.get("value", "")).strip():
        raise ValueError("bridge_browser_session_select requires params.value")
    if tool == "bridge_browser_session_set_checked" and "checked" not in params:
        raise ValueError("bridge_browser_session_set_checked requires params.checked")
    if tool == "bridge_browser_session_new_tab" and "url" in params and not isinstance(params.get("url"), str):
        raise ValueError("bridge_browser_session_new_tab params.url must be a string")
    if tool == "bridge_create_proposal":
        if not str(params.get("action_type", "")).strip():
            raise ValueError("bridge_create_proposal requires params.action_type")
        if not isinstance(params.get("action_payload", {}), dict):
            raise ValueError("bridge_create_proposal requires object action_payload")
    if tool == "read_file" and not str(params.get("path", "")).strip():
        raise ValueError("read_file requires params.path")
    if tool == "write_file" and not str(params.get("path", "")).strip():
        raise ValueError("write_file requires params.path")
    if tool == "write_file" and "content" not in params and not str(params.get("content_template", "")).strip():
        raise ValueError("write_file requires params.content or params.content_template")
    if tool == "finish" and not str(params.get("summary", "")).strip():
        fallback = reason or "The session is ready to finish."
        params = {**params, "summary": fallback}

    return SessionToolAction(tool=tool, params=dict(params), reason=reason)


class SessionRunner(SeedRunner):
    def __init__(
        self,
        *,
        workspace_dir: Path,
        bridge_client,
        model: str,
        max_turns_per_resume: int,
        runtime_code_dir: Path | None = None,
    ):
        super().__init__(
            workspace_dir=workspace_dir,
            runtime_code_dir=runtime_code_dir,
            bridge_client=bridge_client,
            planner=None,
            max_steps=max_turns_per_resume,
        )
        self.model = model
        self.max_turns_per_resume = max_turns_per_resume

    def _reportable_result(self, action_kind: str, result: dict[str, Any]) -> dict[str, Any]:
        if action_kind.startswith("bridge_browser_session_"):
            return {
                "outcome": result.get("outcome", "snapshot"),
                "session_id": result.get("session_id", ""),
                "snapshot_id": result.get("snapshot_id", ""),
                "current_url": result.get("current_url", ""),
                "http_status": result.get("http_status"),
                "page_title": result.get("page_title", ""),
                "text_bytes": result.get("text_bytes", 0),
                "text_truncated": result.get("text_truncated", False),
                "screenshot_sha256": result.get("screenshot_sha256", ""),
                "screenshot_bytes": result.get("screenshot_bytes", 0),
                "interactable_count": len(result.get("interactable_elements", [])),
                "tab_count": len(result.get("tabs", [])),
                "proposal_id": ((result.get("proposal") or {}).get("proposal_id", "") if isinstance(result.get("proposal"), dict) else ""),
            }
        if action_kind == "bridge_browser_submit_proposal":
            return {
                "proposal_id": result.get("proposal_id", ""),
                "status": result.get("status", ""),
                "action_type": result.get("action_type", ""),
                "target_url": result.get("target_url", ""),
                "method": result.get("method", ""),
            }
        return super()._reportable_result(action_kind, result)

    async def run_session(
        self,
        *,
        session_id: str,
        task: str = "",
        input_url: str = "",
        proposal_target_url: str = "",
        launch_mode: str = "default",
        model: str = "",
        capability_profile: str = "bounded_packet",
        resume: bool = False,
    ) -> SessionRunResult:
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        state = self._load_state(session_id)
        now = _now_iso()
        if state is None:
            state = {
                "session_id": session_id,
                "created_at": now,
                "updated_at": now,
                "status": "starting",
                "task": task,
                "input_url": input_url,
                "proposal_target_url": proposal_target_url,
                "launch_mode": launch_mode,
                "model": model or self.model,
                "capability_profile": capability_profile,
                "resume_count": 0,
                "current_run_id": "",
                "last_run_id": "",
                "summary_path": "",
                "last_proposal": {},
                "browser_session": {},
                "browser_execution_consumed": False,
                "current_screenshot_path": "",
                "final_answer_path": "",
                "error": "",
            }
        else:
            state["task"] = task or state.get("task", "")
            if input_url:
                state["input_url"] = input_url
            if proposal_target_url:
                state["proposal_target_url"] = proposal_target_url
            if model:
                state["model"] = model
            if launch_mode:
                state["launch_mode"] = launch_mode
            if capability_profile:
                state["capability_profile"] = capability_profile

        run_id = uuid4().hex
        if resume:
            state["resume_count"] = int(state.get("resume_count", 0)) + 1
        run_state = RunState(
            task=str(state.get("task", "")),
            run_id=run_id,
            workspace_dir=self.workspace.workspace_dir,
            runtime_code_dir=self.runtime_code_dir,
            input_url=str(state.get("input_url", "")),
            proposal_target_url=str(state.get("proposal_target_url", "")),
        )
        if isinstance(state.get("last_proposal"), dict):
            run_state.last_proposal = dict(state["last_proposal"])

        state["status"] = "running"
        state["current_run_id"] = run_id
        state["updated_at"] = _now_iso()
        state["error"] = ""
        self._write_state(session_id, state)

        await self._report(
            run_id=run_id,
            event_kind="run_start",
            step_index=None,
            tool_name=None,
            summary={
                "task": run_state.task,
                "workspace_dir": str(self.workspace.workspace_dir),
                "runtime_code_dir": str(self.runtime_code_dir),
                "input_url": run_state.input_url,
                "proposal_target_url": run_state.proposal_target_url,
                "session_id": session_id,
                "capability_profile": state.get("capability_profile", "bounded_packet"),
                "session_resume_count": state["resume_count"],
                "reported_origin": "untrusted_session",
            },
        )

        stop_reason: SessionRunResult["stop_reason"] = "max_turns_reached"
        finish_summary = "session reached max turns for this resume"
        for step_index in range(self.max_turns_per_resume):
            if self._budget_exhausted(run_state.last_bridge_status):
                stop_reason = "budget_exhausted"
                finish_summary = "budget exhausted"
                break

            # If the operator already approved but has not executed yet, pause again.
            proposal = state.get("last_proposal", {})
            if isinstance(proposal, dict) and proposal.get("status") == "approved":
                stop_reason = "waiting_for_approval"
                finish_summary = "approval recorded; waiting for execute"
                state["status"] = "waiting_for_approval"
                self._append_transcript(
                    session_id,
                    {
                        "kind": "operator_state",
                        "timestamp": _now_iso(),
                        "run_id": run_id,
                        "summary": "Approval is recorded. Waiting for execute before resuming consequential work.",
                        "proposal": proposal,
                    },
                )
                break
            if isinstance(proposal, dict) and proposal.get("status") == "executed":
                if (
                    proposal.get("action_type") in {"browser_submit", "browser_http_request"}
                    and not bool(state.get("browser_execution_consumed"))
                ):
                    execution_result = proposal.get("execution_result", {})
                    if isinstance(execution_result, dict):
                        state["browser_session"] = {
                            "session_id": execution_result.get("session_id", ""),
                            "snapshot_id": execution_result.get("snapshot_id", ""),
                            "current_url": execution_result.get("current_url", ""),
                            "page_title": execution_result.get("page_title", ""),
                            "http_status": execution_result.get("http_status"),
                            "active_tab_id": execution_result.get("active_tab_id", ""),
                            "tabs": execution_result.get("tabs", []),
                            "field_preview": execution_result.get("field_preview", []),
                            "pending_request_preview": {},
                        }
                    state["browser_execution_consumed"] = True
                    self._append_transcript(
                        session_id,
                        {
                            "kind": "operator_state",
                            "timestamp": _now_iso(),
                            "run_id": run_id,
                            "summary": "The approved browser request executed. Continue by inspecting the post-action page.",
                            "proposal": proposal,
                        },
                    )
                else:
                    final_answer = self._executed_proposal_summary(proposal)
                    final_path = f"sessions/{session_id}/artifacts/final_answer.md"
                    self.workspace.write_file(final_path, final_answer + "\n")
                    state["final_answer_path"] = final_path
                    self._append_transcript(
                        session_id,
                        {
                            "kind": "finish",
                            "timestamp": _now_iso(),
                            "run_id": run_id,
                            "reason": "The approved action already executed, so the session can conclude.",
                            "summary": final_answer,
                        },
                    )
                    stop_reason = "finished"
                    finish_summary = final_answer
                    state["status"] = "finished"
                    break

            try:
                action = await self._next_action(session_id=session_id, state=state, run_state=run_state)
            except Exception as exc:  # noqa: BLE001
                detail = f"{type(exc).__name__}: {exc}"
                state["status"] = "failed"
                state["error"] = detail
                self._append_transcript(
                    session_id,
                    {
                        "kind": "error",
                        "timestamp": _now_iso(),
                        "run_id": run_id,
                        "detail": detail,
                    },
                )
                stop_reason = "failed"
                finish_summary = detail
                break
            if action.tool == "finish":
                final_answer = str(action.params.get("summary", ""))
                final_path = f"sessions/{session_id}/artifacts/final_answer.md"
                self.workspace.write_file(final_path, final_answer + "\n")
                state["final_answer_path"] = final_path
                self._append_transcript(
                    session_id,
                    {
                        "kind": "finish",
                        "timestamp": _now_iso(),
                        "run_id": run_id,
                        "reason": action.reason,
                        "summary": final_answer,
                    },
                )
                stop_reason = "finished"
                finish_summary = final_answer
                state["status"] = "finished"
                break

            self._append_transcript(
                session_id,
                {
                    "kind": "model_action",
                    "timestamp": _now_iso(),
                    "run_id": run_id,
                    "tool": action.tool,
                    "params": action.params,
                    "reason": action.reason,
                },
            )
            try:
                result = await self._execute_session_action(session_id, action, run_state, step_index)
            except Exception as exc:  # noqa: BLE001
                detail = f"{type(exc).__name__}: {exc}"
                state["status"] = "failed"
                state["error"] = detail
                self._append_transcript(
                    session_id,
                    {
                        "kind": "error",
                        "timestamp": _now_iso(),
                        "run_id": run_id,
                        "tool": action.tool,
                        "reason": action.reason,
                        "detail": detail,
                    },
                )
                await self._report(
                    run_id=run_id,
                    event_kind="step",
                    step_index=step_index,
                    tool_name=action.tool,
                    summary={
                        "reported_origin": "untrusted_session",
                        "step_kind": action.tool,
                        "detail": detail,
                    },
                )
                stop_reason = "failed"
                finish_summary = detail
                break

            self._append_transcript(
                session_id,
                {
                    "kind": "tool_result",
                    "timestamp": _now_iso(),
                    "run_id": run_id,
                    "tool": action.tool,
                    "reason": action.reason,
                    "result": result,
                },
            )
            await self._report(
                run_id=run_id,
                event_kind="step",
                step_index=step_index,
                tool_name=action.tool,
                summary={
                    "reported_origin": "untrusted_session",
                    "step_kind": action.tool,
                    "result": self._reportable_result(action.tool, result),
                    "session_id": session_id,
                },
            )

            if action.tool == "bridge_status":
                if result.get("budget_exhausted"):
                    stop_reason = "budget_exhausted"
                    finish_summary = "budget exhausted"
                    state["status"] = "failed"
                    break
                state["last_bridge_status"] = result
            if action.tool == "bridge_create_proposal":
                state["last_proposal"] = {
                    **result,
                    "action_payload": dict(action.params.get("action_payload", {})),
                }
                state["browser_execution_consumed"] = False
                stop_reason = "waiting_for_approval"
                finish_summary = "waiting for approval"
                state["status"] = "waiting_for_approval"
                break
            if action.tool == "bridge_browser_submit_proposal":
                state["last_proposal"] = {
                    **result,
                    "action_payload": {
                        "session_id": action.params.get("session_id", ""),
                        "snapshot_id": action.params.get("snapshot_id", ""),
                        "submit_element_id": action.params.get("element_id", ""),
                        "target_url": result.get("target_url", ""),
                        "method": result.get("method", ""),
                        "field_preview": result.get("field_preview", []),
                    },
                }
                state["browser_execution_consumed"] = False
                stop_reason = "waiting_for_approval"
                finish_summary = "waiting for approval"
                state["status"] = "waiting_for_approval"
                break
            if action.tool in {"bridge_browser_render", "bridge_browser_follow_href"}:
                artifact_path = str(result.get("artifact_path", "")).strip()
                if artifact_path:
                    state["current_screenshot_path"] = artifact_path
            if action.tool.startswith("bridge_browser_session_"):
                artifact_path = str(result.get("artifact_path", "")).strip()
                if artifact_path:
                    state["current_screenshot_path"] = artifact_path
                state["browser_session"] = self._browser_session_state_from_result(result)
                if result.get("outcome") == "proposal_required" and isinstance(result.get("proposal"), dict):
                    state["last_proposal"] = result["proposal"]
                    state["browser_execution_consumed"] = False
                    stop_reason = "waiting_for_approval"
                    finish_summary = "waiting for approval"
                    state["status"] = "waiting_for_approval"
                    break
        else:
            state["status"] = "running"

        payload = {
            "run_id": run_id,
            "session_id": session_id,
            "task": run_state.task,
            "input_url": run_state.input_url,
            "proposal_target_url": run_state.proposal_target_url,
            "success": stop_reason == "finished",
            "finished_reason": stop_reason,
            "finish_summary": finish_summary,
            "steps_executed": self._count_transcript_steps(session_id, run_id),
            "workspace_dir": str(self.workspace.workspace_dir),
            "runtime_code_dir": str(self.runtime_code_dir),
        }
        summary_path = self.workspace.write_file(
            f"sessions/{session_id}/artifacts/{run_id}_summary.json",
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )["path"]

        state["updated_at"] = _now_iso()
        state["last_run_id"] = run_id
        state["current_run_id"] = run_id
        state["summary_path"] = summary_path
        if stop_reason == "max_turns_reached":
            state["status"] = "running"
        self._write_state(session_id, state)

        await self._report(
            run_id=run_id,
            event_kind="run_end",
            step_index=payload["steps_executed"],
            tool_name=None,
            summary={
                "reported_origin": "untrusted_session",
                "success": stop_reason == "finished",
                "finished_reason": stop_reason,
                "finish_summary": finish_summary,
                "summary_path": summary_path,
                "session_id": session_id,
            },
        )
        return SessionRunResult(
            session_id=session_id,
            run_id=run_id,
            stop_reason=stop_reason,
            summary_path=summary_path,
            steps_executed=payload["steps_executed"],
            error=str(state.get("error", "")),
        )

    async def _next_action(self, *, session_id: str, state: dict[str, Any], run_state: RunState) -> SessionToolAction:
        response = await self.bridge_client.chat(
            model=str(state.get("model", self.model) or self.model),
            message=self._build_llm_message(session_id=session_id, state=state, run_state=run_state),
        )
        content = response.choices[0].message.content or ""
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            if content.startswith("stage1 deterministic reply:"):
                raise ValueError(
                    "model returned mock plain text instead of session-action JSON; "
                    "provider sessions require RSI_LITELLM_RESPONSE_MODE=provider_passthrough"
                ) from exc
            raise ValueError(f"model did not return valid JSON: {exc}") from exc
        payload = self._coerce_model_payload(payload, state)
        return validate_session_action(payload)

    def _build_llm_message(self, *, session_id: str, state: dict[str, Any], run_state: RunState) -> str:
        transcript_tail = self._read_transcript_tail(session_id, max_items=8)
        last_browser = run_state.last_browser_follow or run_state.last_browser_render or {}
        browser_session = state.get("browser_session", {}) if isinstance(state.get("browser_session"), dict) else {}
        last_proposal = state.get("last_proposal", {})
        proposal_target_url = state.get("proposal_target_url", "")
        capability_profile = str(state.get("capability_profile", "bounded_packet") or "bounded_packet")
        public_workflow = capability_profile == "workflow_browser_public"
        instructions = [
            "Return exactly one JSON object with keys tool, reason, params.",
            "Use only the allowed tools.",
            "Do not use run_command, shell, or unsupported tools.",
            "Use finish when the session can conclude with a plain-language answer.",
            "If a browser page should be read first, use bridge_browser_render.",
            "If the task requires interactive browsing, use bridge_browser_session_open first, then use only the returned session_id, snapshot_id, and interactable element_id values.",
            "Do not invent element IDs, snapshot IDs, or tab IDs.",
            "If a trusted browser session exists but the interactable list is empty or stale, refresh it with bridge_browser_session_snapshot.",
            "If human approval is needed outside the trusted browser request intercept flow, use bridge_create_proposal.",
            "When using bridge_create_proposal, always include params.action_type and params.action_payload.",
            "If proposal_target_url is present, use it as action_payload.url.",
            "Example proposal action: {\"tool\":\"bridge_create_proposal\",\"reason\":\"Need approval before posting the summary.\",\"params\":{\"action_type\":\"http_post\",\"action_payload\":{\"url\":\"%s\",\"body\":{\"summary\":\"one short sentence\"}}}}" % proposal_target_url,
        ]
        if public_workflow:
            instructions.extend(
                [
                    "This session is in workflow_browser_public mode. Stay inside the trusted browser session for the workflow.",
                    "Use bridge_browser_session_click, bridge_browser_session_fill, bridge_browser_session_select, bridge_browser_session_set_checked, bridge_browser_session_press, bridge_browser_session_hover, bridge_browser_session_wait_for, bridge_browser_session_navigate, and tab actions as needed.",
                    "When a real mutating browser request is paused for approval, the tool result will include proposal_required and a proposal preview. Stop and wait for approval instead of inventing another proposal.",
                    "Prefer bridge_browser_session_click for links and buttons, bridge_browser_session_fill for text fields, and bridge_browser_session_navigate for direct URL navigation.",
                ]
            )
        else:
            instructions.extend(
                [
                    "Use bridge_browser_session_click only for links and non-submit buttons.",
                    "Use bridge_browser_submit_proposal when a submit element is ready and approval is needed before submitting the form.",
                    "For the current approval flow, action_type should be http_post.",
                ]
            )
        prompt = {
            "task": state.get("task", ""),
            "session_id": session_id,
            "resume_count": state.get("resume_count", 0),
            "input_url": state.get("input_url", ""),
            "proposal_target_url": proposal_target_url,
            "capability_profile": capability_profile,
            "last_browser_packet": {
                "final_url": last_browser.get("final_url", ""),
                "page_title": last_browser.get("page_title", ""),
                "text_preview": last_browser.get("text_preview", ""),
                "followable_links": (last_browser.get("followable_links") or [])[:5],
            },
            "browser_session_packet": {
                "session_id": browser_session.get("session_id", ""),
                "snapshot_id": browser_session.get("snapshot_id", ""),
                "current_url": browser_session.get("current_url", ""),
                "page_title": browser_session.get("page_title", ""),
                "http_status": browser_session.get("http_status"),
                "interactable_elements": (browser_session.get("interactable_elements") or [])[:24],
                "field_preview": browser_session.get("field_preview", []),
                "active_tab_id": browser_session.get("active_tab_id", ""),
                "tabs": (browser_session.get("tabs") or [])[:8],
                "pending_request_preview": browser_session.get("pending_request_preview", {}),
            },
            "last_proposal": last_proposal,
            "allowed_tools": sorted(ALLOWED_SESSION_TOOLS),
            "instructions": instructions,
            "recent_transcript": transcript_tail,
        }
        return json.dumps(prompt, indent=2, sort_keys=True)

    def _coerce_model_payload(self, payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return payload
        tool = str(payload.get("tool", "")).strip()
        browser_session = state.get("browser_session", {})
        if tool == "bridge_browser_session_open":
            params = payload.get("params", {})
            if not isinstance(params, dict):
                params = {}
            if not str(params.get("url", "")).strip() and str(state.get("input_url", "")).strip():
                params["url"] = state.get("input_url", "")
            if not str(params.get("capability_profile", "")).strip():
                params["capability_profile"] = state.get("capability_profile", "bounded_packet")
            return {**payload, "params": params}
        if tool in {
            "bridge_browser_session_navigate",
            "bridge_browser_session_snapshot",
            "bridge_browser_session_click",
            "bridge_browser_session_fill",
            "bridge_browser_session_type",
            "bridge_browser_session_select",
            "bridge_browser_session_set_checked",
            "bridge_browser_session_press",
            "bridge_browser_session_hover",
            "bridge_browser_session_wait_for",
            "bridge_browser_session_back",
            "bridge_browser_session_forward",
            "bridge_browser_session_new_tab",
            "bridge_browser_session_switch_tab",
            "bridge_browser_session_close_tab",
            "bridge_browser_submit_proposal",
        }:
            params = payload.get("params", {})
            if not isinstance(params, dict):
                params = {}
            if isinstance(browser_session, dict):
                if tool != "bridge_browser_session_open" and not str(params.get("session_id", "")).strip():
                    params["session_id"] = browser_session.get("session_id", "")
                if tool not in {"bridge_browser_session_snapshot", "bridge_browser_session_new_tab"} and not str(params.get("snapshot_id", "")).strip():
                    params["snapshot_id"] = browser_session.get("snapshot_id", "")
            return {**payload, "params": params}
        if tool != "bridge_create_proposal":
            return payload
        params = payload.get("params", {})
        if not isinstance(params, dict):
            params = {}
        proposal_target_url = str(state.get("proposal_target_url", "")).strip()
        if not str(params.get("action_type", "")).strip():
            params["action_type"] = "http_post"
        action_payload = params.get("action_payload", {})
        if not isinstance(action_payload, dict):
            action_payload = {}
        if proposal_target_url and not str(action_payload.get("url", "")).strip():
            action_payload["url"] = proposal_target_url
        params["action_payload"] = action_payload
        return {**payload, "params": params}

    async def _execute_session_action(
        self,
        session_id: str,
        action: SessionToolAction,
        run_state: RunState,
        step_index: int,
    ) -> dict[str, Any]:
        if action.tool.startswith("bridge_browser_session_") or action.tool == "bridge_browser_submit_proposal":
            return await self._execute_browser_session_action(session_id, action, step_index)
        plan_action = PlanAction(kind=action.tool, params=action.params)
        result = await self._execute_action(plan_action, run_state)
        if action.tool == "bridge_create_proposal":
            state = self._load_state(session_id) or {}
            state["last_proposal"] = {
                **(state.get("last_proposal") or {}),
                **result,
                "action_payload": dict(action.params.get("action_payload", {})),
            }
            self._write_state(session_id, state)
        if action.tool in {"bridge_browser_render", "bridge_browser_follow_href"}:
            screenshot_base64 = ""
            if action.tool == "bridge_browser_render":
                screenshot_base64 = (run_state.last_browser_render or {}).get("screenshot_png_base64", "")
            else:
                screenshot_base64 = (run_state.last_browser_follow or {}).get("screenshot_png_base64", "")
            if screenshot_base64:
                artifact_path = f"sessions/{session_id}/artifacts/turn_{step_index + 1:03d}_browser.png"
                self.workspace.write_binary_base64(artifact_path, screenshot_base64)
                state = self._load_state(session_id) or {}
                state["current_screenshot_path"] = artifact_path
                self._write_state(session_id, state)
                result = {**result, "artifact_path": artifact_path}
        return result

    async def _execute_browser_session_action(
        self,
        session_id: str,
        action: SessionToolAction,
        step_index: int,
    ) -> dict[str, Any]:
        params = dict(action.params)
        state = self._load_state(session_id) or {}
        capability_profile = str(state.get("capability_profile", "bounded_packet") or "bounded_packet")
        public_workflow = capability_profile == "workflow_browser_public"
        if action.tool == "bridge_browser_session_open":
            response = await self.bridge_client.browser_session_open(
                url=str(params["url"]),
                capability_profile=str(params.get("capability_profile", capability_profile)),
            )
            return await self._session_snapshot_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_navigate":
            response = await self.bridge_client.browser_session_navigate(
                session_id=str(params["session_id"]),
                snapshot_id=str(params.get("snapshot_id", "")),
                url=str(params["url"]),
            )
            return await self._session_action_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_snapshot":
            response = await self.bridge_client.browser_session_snapshot(
                session_id=str(params["session_id"]),
            )
            return await self._session_snapshot_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_click":
            if public_workflow:
                response = await self.bridge_client.browser_session_click_action(
                    session_id=str(params["session_id"]),
                    snapshot_id=str(params["snapshot_id"]),
                    element_id=str(params["element_id"]),
                )
                return await self._session_action_result(session_id, response, step_index=step_index)
            response = await self.bridge_client.browser_session_click(
                session_id=str(params["session_id"]),
                snapshot_id=str(params["snapshot_id"]),
                element_id=str(params["element_id"]),
            )
            return await self._session_snapshot_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_fill":
            response = await self.bridge_client.browser_session_fill(
                session_id=str(params["session_id"]),
                snapshot_id=str(params["snapshot_id"]),
                element_id=str(params["element_id"]),
                text=str(params["text"]),
            )
            return await self._session_action_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_type":
            if public_workflow:
                response = await self.bridge_client.browser_session_fill(
                    session_id=str(params["session_id"]),
                    snapshot_id=str(params["snapshot_id"]),
                    element_id=str(params["element_id"]),
                    text=str(params["text"]),
                )
                return await self._session_action_result(session_id, response, step_index=step_index)
            response = await self.bridge_client.browser_session_type(
                session_id=str(params["session_id"]),
                snapshot_id=str(params["snapshot_id"]),
                element_id=str(params["element_id"]),
                text=str(params["text"]),
            )
            return await self._session_snapshot_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_select":
            if public_workflow:
                response = await self.bridge_client.browser_session_select_action(
                    session_id=str(params["session_id"]),
                    snapshot_id=str(params["snapshot_id"]),
                    element_id=str(params["element_id"]),
                    value=str(params["value"]),
                )
                return await self._session_action_result(session_id, response, step_index=step_index)
            response = await self.bridge_client.browser_session_select(
                session_id=str(params["session_id"]),
                snapshot_id=str(params["snapshot_id"]),
                element_id=str(params["element_id"]),
                value=str(params["value"]),
            )
            return await self._session_snapshot_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_set_checked":
            if public_workflow:
                response = await self.bridge_client.browser_session_set_checked_action(
                    session_id=str(params["session_id"]),
                    snapshot_id=str(params["snapshot_id"]),
                    element_id=str(params["element_id"]),
                    checked=bool(params["checked"]),
                )
                return await self._session_action_result(session_id, response, step_index=step_index)
            response = await self.bridge_client.browser_session_set_checked(
                session_id=str(params["session_id"]),
                snapshot_id=str(params["snapshot_id"]),
                element_id=str(params["element_id"]),
                checked=bool(params["checked"]),
            )
            return await self._session_snapshot_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_press":
            response = await self.bridge_client.browser_session_press(
                session_id=str(params["session_id"]),
                snapshot_id=str(params.get("snapshot_id", "")),
                key=str(params["key"]),
                element_id=str(params.get("element_id", "")),
            )
            return await self._session_action_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_hover":
            response = await self.bridge_client.browser_session_hover(
                session_id=str(params["session_id"]),
                snapshot_id=str(params["snapshot_id"]),
                element_id=str(params["element_id"]),
            )
            return await self._session_action_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_wait_for":
            response = await self.bridge_client.browser_session_wait_for(
                session_id=str(params["session_id"]),
                snapshot_id=str(params.get("snapshot_id", "")),
                text=str(params.get("text", "")),
                time_seconds=float(params.get("time_seconds", 0.0)),
            )
            return await self._session_action_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_back":
            response = await self.bridge_client.browser_session_back(
                session_id=str(params["session_id"]),
                snapshot_id=str(params.get("snapshot_id", "")),
            )
            return await self._session_action_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_forward":
            response = await self.bridge_client.browser_session_forward(
                session_id=str(params["session_id"]),
                snapshot_id=str(params.get("snapshot_id", "")),
            )
            return await self._session_action_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_new_tab":
            response = await self.bridge_client.browser_session_new_tab(
                session_id=str(params["session_id"]),
                url=str(params.get("url", "")),
            )
            return await self._session_action_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_switch_tab":
            response = await self.bridge_client.browser_session_switch_tab(
                session_id=str(params["session_id"]),
                snapshot_id=str(params.get("snapshot_id", "")),
                tab_id=str(params["tab_id"]),
            )
            return await self._session_action_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_session_close_tab":
            response = await self.bridge_client.browser_session_close_tab(
                session_id=str(params["session_id"]),
                snapshot_id=str(params.get("snapshot_id", "")),
                tab_id=str(params.get("tab_id", "")),
            )
            return await self._session_action_result(session_id, response, step_index=step_index)
        if action.tool == "bridge_browser_submit_proposal":
            proposal = await self.bridge_client.browser_submit_proposal(
                session_id=str(params["session_id"]),
                snapshot_id=str(params["snapshot_id"]),
                element_id=str(params["element_id"]),
            )
            return {
                "proposal_id": proposal.proposal_id,
                "status": proposal.status,
                "action_type": proposal.action_type,
                "target_url": proposal.action_payload.get("target_url", ""),
                "method": proposal.action_payload.get("method", ""),
                "field_preview": proposal.action_payload.get("field_preview", []),
            }
        raise ValueError(f"unsupported browser session tool: {action.tool}")

    async def _session_snapshot_result(
        self,
        session_id: str,
        response,
        *,
        step_index: int,
    ) -> dict[str, Any]:
        artifact_path = f"sessions/{session_id}/artifacts/turn_{step_index + 1:03d}_browser.png"
        self.workspace.write_binary_base64(artifact_path, response.screenshot_png_base64)
        state = self._load_state(session_id) or {}
        state["current_screenshot_path"] = artifact_path
        self._write_state(session_id, state)
        return {
            "outcome": "snapshot",
            **self._browser_snapshot_result_dict(response, artifact_path=artifact_path),
        }

    async def _session_action_result(
        self,
        session_id: str,
        response,
        *,
        step_index: int,
    ) -> dict[str, Any]:
        snapshot = response.snapshot
        artifact_path = ""
        if snapshot is not None and snapshot.screenshot_png_base64:
            artifact_path = f"sessions/{session_id}/artifacts/turn_{step_index + 1:03d}_browser.png"
            self.workspace.write_binary_base64(artifact_path, snapshot.screenshot_png_base64)
            state = self._load_state(session_id) or {}
            state["current_screenshot_path"] = artifact_path
            self._write_state(session_id, state)
        result = {
            "outcome": response.outcome,
            "proposal_preview": response.proposal_preview.model_dump() if response.proposal_preview else {},
            "proposal": response.proposal.model_dump() if response.proposal else {},
            "artifact_path": artifact_path,
        }
        if snapshot is not None:
            result.update(self._browser_snapshot_result_dict(snapshot, artifact_path=artifact_path))
        return result

    def _browser_snapshot_result_dict(self, response, *, artifact_path: str) -> dict[str, Any]:
        return {
            "session_id": response.session_id,
            "snapshot_id": response.snapshot_id,
            "current_url": response.current_url,
            "http_status": response.http_status,
            "page_title": response.page_title,
            "meta_description": response.meta_description,
            "text_bytes": response.text_bytes,
            "text_truncated": response.text_truncated,
            "content_preview": response.rendered_text[:200],
            "screenshot_sha256": response.screenshot_sha256,
            "screenshot_bytes": response.screenshot_bytes,
            "interactable_elements": [item.model_dump() for item in response.interactable_elements],
            "active_tab_id": getattr(response, "active_tab_id", ""),
            "tabs": [item.model_dump() for item in getattr(response, "tabs", [])],
            "pending_request_preview": (
                response.pending_request_preview.model_dump()
                if getattr(response, "pending_request_preview", None) is not None
                else {}
            ),
            "capability_profile": getattr(response, "capability_profile", "bounded_packet"),
            "artifact_path": artifact_path,
        }

    def _browser_session_state_from_result(self, result: dict[str, Any]) -> dict[str, Any]:
        pending_request_preview = result.get("pending_request_preview", {})
        if not pending_request_preview and isinstance(result.get("proposal_preview"), dict):
            pending_request_preview = result.get("proposal_preview", {})
        return {
            "session_id": result.get("session_id", ""),
            "snapshot_id": result.get("snapshot_id", ""),
            "current_url": result.get("current_url", ""),
            "page_title": result.get("page_title", ""),
            "http_status": result.get("http_status"),
            "interactable_elements": result.get("interactable_elements", []),
            "active_tab_id": result.get("active_tab_id", ""),
            "tabs": result.get("tabs", []),
            "pending_request_preview": pending_request_preview,
            "capability_profile": result.get("capability_profile", "bounded_packet"),
            "field_preview": result.get("field_preview", []),
        }

    def _count_transcript_steps(self, session_id: str, run_id: str) -> int:
        return sum(
            1
            for item in self._read_transcript_tail(session_id, max_items=200)
            if item.get("run_id") == run_id and item.get("kind") in {"tool_result", "error", "finish"}
        )

    def _executed_proposal_summary(self, proposal: dict[str, Any]) -> str:
        action_payload = proposal.get("action_payload", {})
        execution_result = proposal.get("execution_result", {})
        target_url = ""
        summary_text = ""
        http_status = ""
        if isinstance(action_payload, dict):
            target_url = str(action_payload.get("url", "") or action_payload.get("target_url", "")).strip()
            body = action_payload.get("body", {})
            if isinstance(body, dict):
                summary_text = str(body.get("summary", "")).strip()
        if isinstance(execution_result, dict):
            raw_status = execution_result.get("http_status")
            http_status = str(raw_status) if raw_status is not None else ""
        parts = []
        if summary_text:
            parts.append(f"{summary_text} This approved action already executed.")
        else:
            parts.append("The approved action already executed.")
        if target_url:
            parts.append(f"Posted to {target_url}.")
        if http_status:
            parts.append(f"HTTP {http_status}.")
        return " ".join(parts)

    def _budget_exhausted(self, last_status: dict[str, Any] | None) -> bool:
        if not isinstance(last_status, dict):
            return False
        return bool(last_status.get("budget_exhausted"))

    def _session_dir(self, session_id: str) -> Path:
        return self.workspace.resolve_path(f"sessions/{session_id}")

    def _state_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "state.json"

    def _transcript_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "transcript.jsonl"

    def _load_state(self, session_id: str) -> dict[str, Any] | None:
        path = self._state_path(session_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_state(self, session_id: str, state: dict[str, Any]) -> None:
        state.setdefault("updated_at", _now_iso())
        self.workspace.write_file(
            f"sessions/{session_id}/state.json",
            json.dumps(state, indent=2, sort_keys=True) + "\n",
        )

    def _append_transcript(self, session_id: str, payload: dict[str, Any]) -> None:
        path = self._transcript_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def _read_transcript_tail(self, session_id: str, *, max_items: int) -> list[dict[str, Any]]:
        path = self._transcript_path(session_id)
        if not path.exists():
            return []
        items: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                items.append(payload)
        return items[-max_items:]


async def run_once(args) -> SessionRunResult:
    settings = agent_settings()
    runner = SessionRunner(
        workspace_dir=settings.workspace_dir,
        runtime_code_dir=settings.runtime_code_dir,
        bridge_client=BridgeClient(args.bridge_url, agent_token=settings.agent_token),
        model=args.model,
        max_turns_per_resume=args.max_turns_per_resume,
    )
    if args.resume:
        state_path = settings.workspace_dir / "sessions" / args.session_id / "state.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        return await runner.run_session(
            session_id=args.session_id,
            task=payload.get("task", ""),
            input_url=payload.get("input_url", ""),
            proposal_target_url=payload.get("proposal_target_url", ""),
            launch_mode=payload.get("launch_mode", "default"),
            model=payload.get("model", args.model),
            capability_profile=payload.get("capability_profile", args.capability_profile),
            resume=True,
        )
    return await runner.run_session(
        session_id=args.session_id,
        task=args.task,
        input_url=args.input_url,
        proposal_target_url=args.proposal_target_url,
        launch_mode=args.launch_mode,
        model=args.model,
        capability_profile=args.capability_profile,
        resume=False,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-url", default="http://bridge:8000")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--task", default="")
    parser.add_argument("--input-url", default="")
    parser.add_argument("--proposal-target-url", default="")
    parser.add_argument("--launch-mode", default="default")
    parser.add_argument("--capability-profile", default="bounded_packet")
    parser.add_argument("--model", default="stage1-deterministic")
    parser.add_argument("--max-turns-per-resume", type=int, default=4)
    parser.add_argument("--resume", action="store_true")
    return parser


def main():
    args = build_parser().parse_args()
    if not args.resume and not args.task.strip():
        raise SystemExit("--task is required unless --resume is set")
    result = asyncio.run(run_once(args))
    print(json.dumps(asdict(result), indent=2, sort_keys=True))


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    main()
