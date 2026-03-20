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

    async def run_session(
        self,
        *,
        session_id: str,
        task: str = "",
        input_url: str = "",
        proposal_target_url: str = "",
        launch_mode: str = "default",
        model: str = "",
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
                "resume_count": 0,
                "current_run_id": "",
                "last_run_id": "",
                "summary_path": "",
                "last_proposal": {},
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
                stop_reason = "waiting_for_approval"
                finish_summary = "waiting for approval"
                state["status"] = "waiting_for_approval"
                break
            if action.tool in {"bridge_browser_render", "bridge_browser_follow_href"}:
                artifact_path = str(result.get("artifact_path", "")).strip()
                if artifact_path:
                    state["current_screenshot_path"] = artifact_path
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
            raise ValueError(f"model did not return valid JSON: {exc}") from exc
        payload = self._coerce_model_payload(payload, state)
        return validate_session_action(payload)

    def _build_llm_message(self, *, session_id: str, state: dict[str, Any], run_state: RunState) -> str:
        transcript_tail = self._read_transcript_tail(session_id, max_items=8)
        last_browser = run_state.last_browser_follow or run_state.last_browser_render or {}
        last_proposal = state.get("last_proposal", {})
        proposal_target_url = state.get("proposal_target_url", "")
        prompt = {
            "task": state.get("task", ""),
            "session_id": session_id,
            "resume_count": state.get("resume_count", 0),
            "input_url": state.get("input_url", ""),
            "proposal_target_url": proposal_target_url,
            "last_browser_packet": {
                "final_url": last_browser.get("final_url", ""),
                "page_title": last_browser.get("page_title", ""),
                "text_preview": last_browser.get("text_preview", ""),
                "followable_links": (last_browser.get("followable_links") or [])[:5],
            },
            "last_proposal": last_proposal,
            "allowed_tools": sorted(ALLOWED_SESSION_TOOLS),
            "instructions": [
                "Return exactly one JSON object with keys tool, reason, params.",
                "Use only the allowed tools.",
                "Do not use run_command, shell, or unsupported tools.",
                "Use finish when the session can conclude with a plain-language answer.",
                "If a browser page should be read first, use bridge_browser_render.",
                "If human approval is needed, use bridge_create_proposal.",
                "When using bridge_create_proposal, always include params.action_type and params.action_payload.",
                "For the current approval flow, action_type should be http_post.",
                "If proposal_target_url is present, use it as action_payload.url.",
                "Example proposal action: {\"tool\":\"bridge_create_proposal\",\"reason\":\"Need approval before posting the summary.\",\"params\":{\"action_type\":\"http_post\",\"action_payload\":{\"url\":\"%s\",\"body\":{\"summary\":\"one short sentence\"}}}}" % proposal_target_url,
            ],
            "recent_transcript": transcript_tail,
        }
        return json.dumps(prompt, indent=2, sort_keys=True)

    def _coerce_model_payload(self, payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return payload
        if str(payload.get("tool", "")).strip() != "bridge_create_proposal":
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
            target_url = str(action_payload.get("url", "")).strip()
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
            resume=True,
        )
    return await runner.run_session(
        session_id=args.session_id,
        task=args.task,
        input_url=args.input_url,
        proposal_target_url=args.proposal_target_url,
        launch_mode=args.launch_mode,
        model=args.model,
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
