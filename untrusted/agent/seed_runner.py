import argparse
import asyncio
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from shared.config import agent_settings
from untrusted.agent.bridge_client import BridgeClient
from untrusted.agent.command_runner import BoundedCommandRunner
from untrusted.agent.workspace_tools import WorkspaceTools


@dataclass
class PlanAction:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlanAction":
        params = dict(payload)
        kind = params.pop("kind")
        return cls(kind=kind, params=params)


class ScriptedPlanner:
    def __init__(self, actions: list[PlanAction]):
        self.actions = list(actions)
        self.index = 0

    @classmethod
    def from_file(cls, path: Path) -> "ScriptedPlanner":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls([PlanAction.from_dict(item) for item in payload])

    def next_action(self, *, state: "RunState") -> PlanAction:
        if self.index >= len(self.actions):
            return PlanAction(kind="finish", params={"summary": "script complete"})
        action = self.actions[self.index]
        self.index += 1
        return action


class DefaultSeedPlanner(ScriptedPlanner):
    def __init__(self):
        super().__init__(
            [
                PlanAction(kind="bridge_status"),
                PlanAction(kind="list_files"),
                PlanAction(
                    kind="bridge_chat",
                    params={"message": "summarize this local-only task: {task}"},
                ),
                PlanAction(
                    kind="write_file",
                    params={
                        "path": "run_outputs/default_seed_report.txt",
                        "content_template": (
                            "task: {task}\n"
                            "bridge_stage: {last_bridge_stage}\n"
                            "budget_remaining: {last_bridge_budget_remaining}\n"
                            "llm_summary: {last_bridge_chat}\n"
                        ),
                    },
                ),
                PlanAction(
                    kind="run_command",
                    params={"argv": ["python", "-m", "pytest", "-q"]},
                ),
                PlanAction(kind="finish", params={"summary": "default seed loop complete"}),
            ]
        )


@dataclass
class RunState:
    task: str
    run_id: str
    workspace_dir: Path
    runtime_code_dir: Path
    input_url: str = ""
    follow_target_url: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    last_bridge_status: dict[str, Any] | None = None
    last_bridge_chat: dict[str, Any] | None = None
    last_web_fetch: dict[str, Any] | None = None
    last_browser_render: dict[str, Any] | None = None
    last_browser_follow: dict[str, Any] | None = None

    def template_context(self) -> dict[str, Any]:
        status = self.last_bridge_status or {}
        chat = self.last_bridge_chat or {}
        fetch = self.last_web_fetch or {}
        browser = self.last_browser_render or {}
        browser_follow = self.last_browser_follow or {}
        followable_links = browser.get("followable_links", [])
        first_followable = followable_links[0] if followable_links else {}
        return {
            "task": self.task,
            "run_id": self.run_id,
            "workspace_dir": str(self.workspace_dir),
            "runtime_code_dir": str(self.runtime_code_dir),
            "input_url": self.input_url,
            "follow_target_url": self.follow_target_url,
            "last_bridge_stage": status.get("stage", ""),
            "last_bridge_budget_remaining": status.get("budget_remaining", ""),
            "last_bridge_chat": chat.get("message", ""),
            "last_bridge_chat_model": chat.get("model", ""),
            "last_web_fetch_url": fetch.get("url", ""),
            "last_web_fetch_request_id": fetch.get("request_id", ""),
            "last_web_fetch_trace_id": fetch.get("trace_id", ""),
            "last_web_fetch_preview": fetch.get("preview", ""),
            "last_browser_request_id": browser.get("request_id", ""),
            "last_browser_trace_id": browser.get("trace_id", ""),
            "last_browser_normalized_url": browser.get("normalized_url", ""),
            "last_browser_final_url": browser.get("final_url", ""),
            "last_browser_title": browser.get("page_title", ""),
            "last_browser_meta_description": browser.get("meta_description", ""),
            "last_browser_rendered_text": browser.get("rendered_text", ""),
            "last_browser_text_preview": browser.get("text_preview", ""),
            "last_browser_text_bytes": browser.get("text_bytes", 0),
            "last_browser_text_truncated": browser.get("text_truncated", False),
            "last_browser_screenshot_base64": browser.get("screenshot_png_base64", ""),
            "last_browser_first_followable_target_url": first_followable.get("target_url", ""),
            "last_browser_first_followable_text": first_followable.get("text", ""),
            "last_browser_follow_request_id": browser_follow.get("request_id", ""),
            "last_browser_follow_trace_id": browser_follow.get("trace_id", ""),
            "last_browser_follow_source_url": browser_follow.get("source_url", ""),
            "last_browser_follow_source_final_url": browser_follow.get("source_final_url", ""),
            "last_browser_follow_requested_target_url": browser_follow.get("requested_target_url", ""),
            "last_browser_follow_matched_link_text": browser_follow.get("matched_link_text", ""),
            "last_browser_follow_normalized_url": browser_follow.get("normalized_url", ""),
            "last_browser_follow_final_url": browser_follow.get("final_url", ""),
            "last_browser_follow_title": browser_follow.get("page_title", ""),
            "last_browser_follow_meta_description": browser_follow.get("meta_description", ""),
            "last_browser_follow_rendered_text": browser_follow.get("rendered_text", ""),
            "last_browser_follow_text_preview": browser_follow.get("text_preview", ""),
            "last_browser_follow_text_bytes": browser_follow.get("text_bytes", 0),
            "last_browser_follow_text_truncated": browser_follow.get("text_truncated", False),
            "last_browser_follow_screenshot_base64": browser_follow.get("screenshot_png_base64", ""),
        }


@dataclass
class SeedRunResult:
    run_id: str
    task: str
    input_url: str
    follow_target_url: str
    success: bool
    finished_reason: str
    steps_executed: int
    summary_path: str
    steps: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SeedRunner:
    def __init__(
        self,
        *,
        workspace_dir: Path,
        bridge_client,
        planner,
        runtime_code_dir: Path | None = None,
        max_steps: int = 8,
        command_runner: BoundedCommandRunner | None = None,
    ):
        self.workspace = WorkspaceTools(workspace_dir)
        self.bridge_client = bridge_client
        self.planner = planner
        self.max_steps = max_steps
        self.runtime_code_dir = (runtime_code_dir or workspace_dir.parent).resolve()
        self.command_runner = command_runner or BoundedCommandRunner(self.workspace.workspace_dir)

    def _resolve_text(self, template: str, state: RunState) -> str:
        return template.format(**state.template_context())

    async def _report(
        self,
        *,
        run_id: str,
        event_kind: str,
        step_index: int | None,
        tool_name: str | None,
        summary: dict[str, Any],
    ):
        await self.bridge_client.report_agent_event(
            run_id=run_id,
            event_kind=event_kind,
            step_index=step_index,
            tool_name=tool_name,
            summary=summary,
        )

    def _reportable_result(self, action_kind: str, result: dict[str, Any]) -> dict[str, Any]:
        if action_kind == "bridge_fetch":
            return {
                "request_id": result["request_id"],
                "trace_id": result["trace_id"],
                "normalized_url": result["normalized_url"],
                "final_url": result["final_url"],
                "http_status": result["http_status"],
                "content_type": result["content_type"],
                "byte_count": result["byte_count"],
                "truncated": result["truncated"],
            }
        if action_kind == "bridge_browser_render":
            return {
                "request_id": result["request_id"],
                "trace_id": result["trace_id"],
                "normalized_url": result["normalized_url"],
                "final_url": result["final_url"],
                "http_status": result["http_status"],
                "page_title": result["page_title"],
                "meta_description": result["meta_description"],
                "text_bytes": result["text_bytes"],
                "text_truncated": result["text_truncated"],
                "screenshot_sha256": result["screenshot_sha256"],
                "screenshot_bytes": result["screenshot_bytes"],
            }
        if action_kind == "bridge_browser_follow_href":
            return {
                "request_id": result["request_id"],
                "trace_id": result["trace_id"],
                "source_url": result["source_url"],
                "source_final_url": result["source_final_url"],
                "requested_target_url": result["requested_target_url"],
                "matched_link_text": result["matched_link_text"],
                "normalized_url": result["normalized_url"],
                "final_url": result["final_url"],
                "http_status": result["http_status"],
                "page_title": result["page_title"],
                "meta_description": result["meta_description"],
                "text_bytes": result["text_bytes"],
                "text_truncated": result["text_truncated"],
                "screenshot_sha256": result["screenshot_sha256"],
                "screenshot_bytes": result["screenshot_bytes"],
            }
        return result

    async def _execute_action(self, action: PlanAction, state: RunState) -> dict[str, Any]:
        if action.kind == "bridge_status":
            status = await self.bridge_client.status()
            state.last_bridge_status = {
                "stage": status.stage,
                "budget_remaining": status.budget.remaining,
            }
            return {
                "stage": status.stage,
                "budget_remaining": status.budget.remaining,
                "budget_exhausted": status.budget.exhausted,
            }

        if action.kind == "bridge_chat":
            message = self._resolve_text(action.params["message"], state)
            model = action.params.get("model", "stage1-deterministic")
            response = await self.bridge_client.chat(model=model, message=message)
            content = response.choices[0].message.content
            state.last_bridge_chat = {"message": content, "model": response.model}
            return {
                "message": content,
                "model": response.model,
                "usage": response.usage.model_dump(),
            }

        if action.kind == "bridge_fetch":
            response = await self.bridge_client.fetch(url=action.params["url"])
            state.last_web_fetch = {
                "url": response.final_url,
                "request_id": response.request_id,
                "trace_id": response.trace_id,
                "preview": response.text[:200],
            }
            return {
                "request_id": response.request_id,
                "trace_id": response.trace_id,
                "normalized_url": response.normalized_url,
                "final_url": response.final_url,
                "http_status": response.http_status,
                "content_type": response.content_type,
                "byte_count": response.byte_count,
                "truncated": response.truncated,
                "content_preview": response.text[:200],
            }

        if action.kind == "bridge_browser_render":
            url = self._resolve_text(action.params["url"], state)
            response = await self.bridge_client.browser_render(url=url)
            state.last_browser_render = {
                "request_id": response.request_id,
                "trace_id": response.trace_id,
                "normalized_url": response.normalized_url,
                "final_url": response.final_url,
                "page_title": response.page_title,
                "meta_description": response.meta_description,
                "rendered_text": response.rendered_text,
                "text_preview": response.rendered_text[:200],
                "text_bytes": response.text_bytes,
                "text_truncated": response.text_truncated,
                "screenshot_png_base64": response.screenshot_png_base64,
                "followable_links": [link.model_dump() for link in response.followable_links],
            }
            return {
                "request_id": response.request_id,
                "trace_id": response.trace_id,
                "normalized_url": response.normalized_url,
                "final_url": response.final_url,
                "http_status": response.http_status,
                "page_title": response.page_title,
                "meta_description": response.meta_description,
                "text_bytes": response.text_bytes,
                "text_truncated": response.text_truncated,
                "content_preview": response.rendered_text[:200],
                "screenshot_sha256": response.screenshot_sha256,
                "screenshot_bytes": response.screenshot_bytes,
                "followable_links": [link.model_dump() for link in response.followable_links],
            }

        if action.kind == "bridge_browser_follow_href":
            source_url = self._resolve_text(action.params["source_url"], state)
            target_url = self._resolve_text(action.params["target_url"], state)
            response = await self.bridge_client.browser_follow_href(
                source_url=source_url,
                target_url=target_url,
            )
            state.last_browser_follow = {
                "request_id": response.request_id,
                "trace_id": response.trace_id,
                "source_url": response.source_url,
                "source_final_url": response.source_final_url,
                "requested_target_url": response.requested_target_url,
                "matched_link_text": response.matched_link_text,
                "normalized_url": response.normalized_url,
                "final_url": response.final_url,
                "page_title": response.page_title,
                "meta_description": response.meta_description,
                "rendered_text": response.rendered_text,
                "text_preview": response.rendered_text[:200],
                "text_bytes": response.text_bytes,
                "text_truncated": response.text_truncated,
                "screenshot_png_base64": response.screenshot_png_base64,
            }
            return {
                "request_id": response.request_id,
                "trace_id": response.trace_id,
                "source_url": response.source_url,
                "source_final_url": response.source_final_url,
                "requested_target_url": response.requested_target_url,
                "matched_link_text": response.matched_link_text,
                "follow_hop_count": response.follow_hop_count,
                "navigation_history": list(response.navigation_history),
                "normalized_url": response.normalized_url,
                "final_url": response.final_url,
                "http_status": response.http_status,
                "page_title": response.page_title,
                "meta_description": response.meta_description,
                "text_bytes": response.text_bytes,
                "text_truncated": response.text_truncated,
                "content_preview": response.rendered_text[:200],
                "screenshot_sha256": response.screenshot_sha256,
                "screenshot_bytes": response.screenshot_bytes,
            }

        if action.kind == "list_files":
            path = action.params.get("path", ".")
            files = self.workspace.list_files(path)
            return {"path": path, "count": len(files), "files": files[:40]}

        if action.kind == "read_file":
            path = action.params["path"]
            content = self.workspace.read_file(path)
            return {
                "path": path,
                "bytes": len(content.encode("utf-8")),
                "content_preview": content[:200],
            }

        if action.kind == "write_file":
            path = action.params["path"]
            if "content" in action.params:
                content = action.params["content"]
            else:
                content = self._resolve_text(action.params["content_template"], state)
            return self.workspace.write_file(path, content)

        if action.kind == "write_binary_base64":
            path = action.params["path"]
            base64_data = self._resolve_text(action.params["base64_template"], state)
            return self.workspace.write_binary_base64(path, base64_data)

        if action.kind == "run_command":
            argv = [self._resolve_text(part, state) for part in action.params["argv"]]
            result = self.command_runner.run(
                argv,
                timeout_seconds=action.params.get("timeout_seconds"),
                output_limit_bytes=action.params.get("output_limit_bytes"),
            )
            return asdict(result)

        raise ValueError(f"unsupported action kind: {action.kind}")

    def _write_summary_files(self, payload: dict[str, Any]) -> str:
        per_run = self.workspace.write_file(
            f"run_outputs/{payload['run_id']}.json",
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )
        self.workspace.write_file(
            "run_outputs/latest_seed_run.json",
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )
        return per_run["path"]

    async def run(
        self,
        task: str,
        *,
        input_url: str = "",
        follow_target_url: str = "",
    ) -> SeedRunResult:
        run_id = uuid4().hex
        state = RunState(
            task=task,
            run_id=run_id,
            workspace_dir=self.workspace.workspace_dir,
            runtime_code_dir=self.runtime_code_dir,
            input_url=input_url,
            follow_target_url=follow_target_url,
        )
        await self._report(
            run_id=run_id,
            event_kind="run_start",
            step_index=None,
            tool_name=None,
            summary={
                "task": task,
                "workspace_dir": str(self.workspace.workspace_dir),
                "runtime_code_dir": str(self.runtime_code_dir),
                "input_url": input_url,
                "follow_target_url": follow_target_url,
                "reported_origin": "untrusted_agent",
            },
        )

        success = False
        finished_reason = "step_limit_reached"
        finish_summary = ""

        try:
            for step_index in range(self.max_steps):
                action = self.planner.next_action(state=state)
                if action.kind == "finish":
                    success = True
                    finished_reason = "planner_finished"
                    finish_summary = action.params.get("summary", "")
                    break

                result = await self._execute_action(action, state)
                record = {
                    "step_index": step_index,
                    "kind": action.kind,
                    "params": action.params,
                    "result": result,
                }
                state.steps.append(record)
                await self._report(
                    run_id=run_id,
                    event_kind="step",
                    step_index=step_index,
                    tool_name=action.kind,
                    summary={
                        "reported_origin": "untrusted_agent",
                        "step_kind": action.kind,
                        "result": self._reportable_result(action.kind, result),
                    },
                )
                if action.kind == "run_command":
                    if result["timed_out"]:
                        raise RuntimeError("local command timed out")
                    if result["returncode"] != 0:
                        raise RuntimeError(f"local command failed: returncode={result['returncode']}")
            else:
                finish_summary = "step limit reached"
        except Exception as exc:  # noqa: BLE001
            finished_reason = "error"
            finish_summary = f"{type(exc).__name__}: {exc}"
            state.steps.append(
                {
                    "step_index": len(state.steps),
                    "kind": "error",
                    "params": {},
                    "result": {"detail": finish_summary},
                }
            )

        payload = {
            "run_id": run_id,
            "task": task,
            "input_url": state.input_url,
            "follow_target_url": state.follow_target_url,
            "success": success,
            "finished_reason": finished_reason,
            "finish_summary": finish_summary,
            "steps_executed": len(state.steps),
            "steps": state.steps,
            "workspace_dir": str(self.workspace.workspace_dir),
            "runtime_code_dir": str(self.runtime_code_dir),
        }
        summary_path = self._write_summary_files(payload)

        await self._report(
            run_id=run_id,
            event_kind="run_end",
            step_index=len(state.steps),
            tool_name=None,
            summary={
                "reported_origin": "untrusted_agent",
                "success": success,
                "finished_reason": finished_reason,
                "finish_summary": finish_summary,
                "summary_path": summary_path,
            },
        )
        return SeedRunResult(
            run_id=run_id,
            task=task,
            input_url=state.input_url,
            follow_target_url=state.follow_target_url,
            success=success,
            finished_reason=finished_reason,
            steps_executed=len(state.steps),
            summary_path=summary_path,
            steps=state.steps,
        )


def build_planner(*, planner_name: str, script_path: Path | None):
    if planner_name == "scripted":
        if script_path is None:
            raise ValueError("--script is required for scripted planner")
        return ScriptedPlanner.from_file(script_path)
    if planner_name == "default":
        return DefaultSeedPlanner()
    raise ValueError(f"unsupported planner: {planner_name}")


async def run_once(args) -> SeedRunResult:
    settings = agent_settings()
    script_path = None
    if args.script:
        script_path = WorkspaceTools(settings.workspace_dir).resolve_path(args.script)
    planner = build_planner(planner_name=args.planner, script_path=script_path)
    runner = SeedRunner(
        workspace_dir=settings.workspace_dir,
        runtime_code_dir=settings.runtime_code_dir,
        bridge_client=BridgeClient(args.bridge_url),
        planner=planner,
        max_steps=args.max_steps,
    )
    return await runner.run(
        args.task,
        input_url=args.input_url,
        follow_target_url=args.follow_target_url,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--planner", choices=["default", "scripted"], default="default")
    parser.add_argument("--script")
    parser.add_argument("--bridge-url", default=agent_settings().bridge_url)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--input-url", default="")
    parser.add_argument("--follow-target-url", default="")

    result = asyncio.run(run_once(parser.parse_args()))
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
