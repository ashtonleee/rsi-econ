import base64
import asyncio
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from shared.config import DEFAULT_LLM_BUDGET_TOKEN_CAP, agent_settings
from shared.mock_llm import deterministic_usage
from shared.schemas import (
    AgentRunEventReceipt,
    BrowserFollowHrefResponse,
    BrowserFollowLink,
    BrowserRenderResponse,
    BrowserState,
    BridgeStatusReport,
    BudgetState,
    ChatChoice,
    ChatCompletionResponse,
    ChatMessage,
    ChatUsage,
    ConnectionStatus,
    RecentRequest,
    RecoveryState,
    WebFetchResponse,
    WebState,
)
from untrusted.agent.command_runner import BoundedCommandRunner
from untrusted.agent.seed_runner import PlanAction, ScriptedPlanner, SeedRunner
from untrusted.agent.workspace_tools import WorkspaceTools
from untrusted.agent.app import app as agent_app


class FakeBridgeClient:
    def __init__(self):
        self.status_calls = 0
        self.chat_calls = 0
        self.chat_requests: list[dict[str, str]] = []
        self.fetch_calls = 0
        self.browser_render_calls = 0
        self.browser_follow_href_calls = 0
        self.reported_events: list[dict] = []

    async def status(self) -> BridgeStatusReport:
        self.status_calls += 1
        return BridgeStatusReport(
            service="bridge",
            stage="stage6_read_only_browser",
            trusted_state_dir="/var/lib/rsi/trusted_state",
            log_path="/var/lib/rsi/trusted_state/logs/bridge_events.jsonl",
            operational_state_path="/var/lib/rsi/trusted_state/state/operational_state.json",
            connections={
                "litellm": ConnectionStatus(
                    url="http://litellm:4000",
                    reachable=True,
                    detail=None,
                    checked_at="2026-03-12T00:00:00+00:00",
                ),
                "fetcher": ConnectionStatus(
                    url="http://fetcher:8082",
                    reachable=True,
                    detail=None,
                    checked_at="2026-03-12T00:00:00+00:00",
                ),
                "browser": ConnectionStatus(
                    url="http://browser:8083",
                    reachable=True,
                    detail=None,
                    checked_at="2026-03-12T00:00:00+00:00",
                ),
            },
            budget=BudgetState(
                unit="mock_tokens",
                total=100,
                spent=9,
                remaining=91,
                exhausted=False,
                minimum_call_cost=5,
                approximation="deterministic_token_usage_from_stage2_mock_litellm",
                total_prompt_tokens=3,
                total_completion_tokens=6,
                total_tokens=9,
            ),
            recovery=RecoveryState(
                checkpoint_dir="/var/lib/rsi/trusted_state/checkpoints",
                baseline_id="seed-123456789abc",
                baseline_source_dir="/app/trusted/recovery/seed_workspace_baseline",
                baseline_archive_path="/var/lib/rsi/trusted_state/checkpoints/baselines/seed_workspace_baseline.tar.gz",
                available_checkpoints=[],
                latest_checkpoint_id=None,
                latest_action=None,
                current_workspace_status="seed_baseline",
            ),
            web=WebState(
                fetcher=ConnectionStatus(
                    url="http://fetcher:8082",
                    reachable=True,
                    detail=None,
                    checked_at="2026-03-12T00:00:00+00:00",
                ),
                allowlist_hosts=["example.com"],
                private_test_hosts=[],
                allowed_content_types=["text/plain", "text/html"],
                caps={
                    "max_redirects": 3,
                    "max_response_bytes": 32768,
                    "max_preview_chars": 1024,
                    "timeout_seconds": 5.0,
                },
                counters={
                    "web_fetch_total": 0,
                    "web_fetch_success": 0,
                    "web_fetch_denied": 0,
                    "web_fetch_errors": 0,
                },
                recent_fetches=[],
            ),
            browser=BrowserState(
                service=ConnectionStatus(
                    url="http://browser:8083",
                    reachable=True,
                    detail=None,
                    checked_at="2026-03-12T00:00:00+00:00",
                ),
                caps={
                    "viewport_width": 1280,
                    "viewport_height": 720,
                    "timeout_seconds": 10.0,
                    "settle_time_ms": 500,
                    "max_rendered_text_bytes": 16384,
                    "max_screenshot_bytes": 1048576,
                    "max_follow_hops": 1,
                    "max_followable_links": 20,
                },
                counters={
                    "browser_render_total": 0,
                    "browser_render_success": 0,
                    "browser_render_denied": 0,
                    "browser_render_errors": 0,
                    "browser_follow_href_total": 0,
                    "browser_follow_href_success": 0,
                    "browser_follow_href_denied": 0,
                    "browser_follow_href_errors": 0,
                },
                recent_renders=[],
                recent_follows=[],
            ),
            counters={"status_queries": 1, "llm_calls_total": 1},
            recent_requests=[
                RecentRequest(
                    timestamp="2026-03-12T00:00:00+00:00",
                    event_type="status_query",
                    request_id="req-status",
                    trace_id="trace-status",
                    actor="agent",
                    source_service="bridge",
                    outcome="success",
                )
            ],
            surfaces={
                "seed_agent": "local_only_stage3_substrate",
                "browser": "trusted_browser_stage6a_read_only_render",
                "browser_follow_href": "trusted_browser_stage6b_safe_follow_href",
            },
        )

    async def chat(self, *, model: str, message: str) -> ChatCompletionResponse:
        self.chat_calls += 1
        self.chat_requests.append({"model": model, "message": message})
        reply = ChatMessage(role="assistant", content=f"scripted reply: {message}")
        return ChatCompletionResponse(
            id="chatcmpl-scripted",
            object="chat.completion",
            created=1,
            model=model,
            choices=[ChatChoice(index=0, message=reply, finish_reason="stop")],
            usage=ChatUsage(prompt_tokens=4, completion_tokens=4, total_tokens=8),
        )

    async def report_agent_event(
        self,
        *,
        run_id: str,
        event_kind: str,
        step_index: int | None,
        tool_name: str | None,
        summary: dict,
    ) -> AgentRunEventReceipt:
        self.reported_events.append(
            {
                "run_id": run_id,
                "event_kind": event_kind,
                "step_index": step_index,
                "tool_name": tool_name,
                "summary": summary,
            }
        )
        return AgentRunEventReceipt(
            request_id=f"req-{len(self.reported_events)}",
            trace_id=f"trace-{len(self.reported_events)}",
            outcome="recorded",
        )

    async def fetch(self, *, url: str) -> WebFetchResponse:
        self.fetch_calls += 1
        return WebFetchResponse(
            request_id="fetch-req-1",
            trace_id="fetch-trace-1",
            normalized_url=url,
            final_url=url,
            scheme="https",
            host="example.com",
            port=443,
            http_status=200,
            content_type="text/html",
            byte_count=24,
            truncated=False,
            redirect_chain=[],
            resolved_ips=["93.184.216.34"],
            used_ip="93.184.216.34",
            content_sha256="hash",
            text="example preview text",
        )

    async def browser_render(self, *, url: str) -> BrowserRenderResponse:
        self.browser_render_calls += 1
        return BrowserRenderResponse(
            request_id="browser-req-1",
            trace_id="browser-trace-1",
            normalized_url=url,
            final_url=url,
            http_status=200,
            page_title="Fixture Browser Title",
            meta_description="Fixture browser description",
            rendered_text="Rendered browser text preview",
            rendered_text_sha256="text-hash",
            text_bytes=28,
            text_truncated=False,
            screenshot_png_base64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO2Wb6wAAAAASUVORK5CYII=",
            screenshot_sha256="image-hash",
            screenshot_bytes=67,
            redirect_chain=[],
            observed_hosts=["example.com"],
            resolved_ips=["93.184.216.34"],
            followable_links=[
                BrowserFollowLink(
                    text="Follow same origin target",
                    target_url="https://example.com/follow-target",
                    same_origin=True,
                )
            ],
        )

    async def browser_follow_href(
        self,
        *,
        source_url: str,
        target_url: str,
    ) -> BrowserFollowHrefResponse:
        self.browser_follow_href_calls += 1
        return BrowserFollowHrefResponse(
            request_id="browser-follow-req-1",
            trace_id="browser-follow-trace-1",
            source_url=source_url,
            source_final_url=source_url,
            requested_target_url=target_url,
            matched_link_text="Follow same origin target",
            follow_hop_count=1,
            navigation_history=[source_url, target_url],
            normalized_url=target_url,
            final_url=target_url,
            http_status=200,
            page_title="Fixture Browser Follow Title",
            meta_description="Fixture browser follow description",
            rendered_text="Rendered followed browser text preview",
            rendered_text_sha256="follow-text-hash",
            text_bytes=36,
            text_truncated=False,
            screenshot_png_base64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO2Wb6wAAAAASUVORK5CYII=",
            screenshot_sha256="follow-image-hash",
            screenshot_bytes=67,
            redirect_chain=[],
            observed_hosts=["example.com"],
            resolved_ips=["93.184.216.34"],
        )


def make_local_task_workspace(workspace: Path):
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a - b\n",
        encoding="ascii",
    )
    (workspace / "tests").mkdir(exist_ok=True)
    (workspace / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="ascii",
    )


def test_agent_settings_make_workspace_target_explicit(monkeypatch, tmp_path):
    workspace_dir = tmp_path / "workspace"
    runtime_code_dir = tmp_path / "runtime_code"
    monkeypatch.setenv("RSI_AGENT_WORKSPACE_DIR", str(workspace_dir))
    monkeypatch.setenv("RSI_AGENT_RUNTIME_CODE_DIR", str(runtime_code_dir))

    settings = agent_settings()

    assert settings.workspace_dir == workspace_dir
    assert settings.runtime_code_dir == runtime_code_dir
    assert settings.workspace_dir != settings.runtime_code_dir


def test_workspace_tools_cannot_escape_mutable_workspace(tmp_path):
    workspace = WorkspaceTools(tmp_path)
    workspace.write_file("notes/summary.txt", "seed agent\n")

    assert workspace.read_file("notes/summary.txt") == "seed agent\n"
    assert workspace.list_files() == ["notes/summary.txt"]
    assert any(entry["path"] == "notes" for entry in workspace.list_tree())

    with pytest.raises(ValueError):
        workspace.read_file("../outside.txt")
    with pytest.raises(ValueError):
        workspace.write_file("/tmp/outside.txt", "nope\n")
    with pytest.raises(ValueError):
        workspace.list_files("../../")


@pytest.mark.fast
def test_workspace_tools_reject_symlink_escapes(tmp_path):
    workspace_dir = tmp_path / "workspace"
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    workspace_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret\n", encoding="ascii")
    (outside_dir / "nested").mkdir()

    try:
        (workspace_dir / "linked_secret.txt").symlink_to(outside_dir / "secret.txt")
        (workspace_dir / "linked_dir").symlink_to(
            outside_dir / "nested",
            target_is_directory=True,
        )
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in test environment: {exc}")

    workspace = WorkspaceTools(workspace_dir)

    with pytest.raises(ValueError):
        workspace.read_file("linked_secret.txt")
    with pytest.raises(ValueError):
        workspace.write_file("linked_dir/new.txt", "escape\n")
    with pytest.raises(ValueError):
        workspace.list_files("linked_dir")


def test_bounded_command_runner_enforces_cwd_timeout_and_output_limit(tmp_path):
    runner = BoundedCommandRunner(tmp_path, default_timeout_seconds=1.0, output_limit_bytes=64)
    env = runner._env()
    assert env["HOME"] == "/tmp"
    assert env["TMPDIR"] == "/tmp"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"

    cwd_result = runner.run(["python", "-c", "from pathlib import Path; print(Path.cwd().name)"])
    assert cwd_result.returncode == 0
    assert cwd_result.stdout.strip() == tmp_path.name
    assert cwd_result.cwd == str(tmp_path)

    with pytest.raises(ValueError):
        runner.run(["bash", "-lc", "pwd"])

    timeout_result = runner.run(
        ["python", "-c", "import time; time.sleep(2)"],
        timeout_seconds=0.1,
    )
    assert timeout_result.timed_out is True

    output_result = runner.run(
        ["python", "-c", "print('x' * 400)"],
        output_limit_bytes=40,
    )
    assert output_result.stdout_truncated is True
    assert len(output_result.stdout) <= 40


@pytest.mark.fast
def test_bounded_command_runner_scrubs_host_secret_and_proxy_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "top-secret")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8443")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.test:1080")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")

    runner = BoundedCommandRunner(tmp_path)
    result = runner.run(
        [
            "python",
            "-c",
            (
                "import json, os\n"
                "keys = ['OPENAI_API_KEY', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY']\n"
                "print(json.dumps({key: os.environ.get(key) for key in keys}, sort_keys=True))\n"
            ),
        ]
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "ALL_PROXY": None,
        "HTTPS_PROXY": None,
        "HTTP_PROXY": None,
        "NO_PROXY": None,
        "OPENAI_API_KEY": None,
    }


def test_agent_health_reports_workspace_writable_and_runtime_code_read_only(monkeypatch, tmp_path):
    workspace_dir = tmp_path / "workspace"
    runtime_code_dir = tmp_path / "runtime_code"
    runtime_code_dir.mkdir(parents=True)
    workspace_dir.mkdir(parents=True)
    os.chmod(runtime_code_dir, 0o555)
    monkeypatch.setenv("RSI_AGENT_WORKSPACE_DIR", str(workspace_dir))
    monkeypatch.setenv("RSI_AGENT_RUNTIME_CODE_DIR", str(runtime_code_dir))

    try:
        with TestClient(agent_app) as client:
            response = client.get("/healthz")
    finally:
        os.chmod(runtime_code_dir, 0o755)

    assert response.status_code == 200
    body = response.json()
    assert body["details"]["workspace_writable"] is True
    assert body["details"]["runtime_code_writable"] is False


def test_scripted_planner_completes_local_task_end_to_end(tmp_path):
    workspace_dir = tmp_path / "workspace"
    make_local_task_workspace(workspace_dir)

    bridge = FakeBridgeClient()
    planner = ScriptedPlanner(
        [
            PlanAction(kind="bridge_status"),
            PlanAction(kind="bridge_chat", params={"message": "summarize {task}"}),
            PlanAction(kind="read_file", params={"path": "calc.py"}),
            PlanAction(
                kind="write_file",
                params={
                    "path": "calc.py",
                    "content": "def add(a, b):\n    return a + b\n",
                },
            ),
            PlanAction(
                kind="run_command",
                params={"argv": ["python", "-m", "pytest", "-q"]},
            ),
            PlanAction(kind="finish", params={"summary": "local task complete"}),
        ]
    )
    runner = SeedRunner(
        workspace_dir=workspace_dir,
        bridge_client=bridge,
        planner=planner,
        max_steps=8,
    )

    result = asyncio.run(runner.run("fix the local add helper"))

    assert result.success is True
    assert result.finished_reason == "planner_finished"
    assert bridge.status_calls == 1
    assert bridge.chat_calls == 1
    assert len(bridge.reported_events) >= 3
    assert bridge.reported_events[0]["event_kind"] == "run_start"
    assert bridge.reported_events[-1]["event_kind"] == "run_end"
    assert "return a + b" in workspace_dir.joinpath("calc.py").read_text(encoding="ascii")

    latest_summary = workspace_dir / "run_outputs" / "latest_seed_run.json"
    assert latest_summary.exists()
    payload = json.loads(latest_summary.read_text(encoding="ascii"))
    assert payload["task"] == "fix the local add helper"
    assert any(step["kind"] == "bridge_status" for step in payload["steps"])
    assert any(step["kind"] == "bridge_chat" for step in payload["steps"])


def test_scripted_planner_can_fetch_via_bridge_and_write_report(tmp_path):
    workspace_dir = tmp_path / "workspace"
    make_local_task_workspace(workspace_dir)

    bridge = FakeBridgeClient()
    planner = ScriptedPlanner(
        [
            PlanAction(kind="bridge_fetch", params={"url": "https://example.com/"}),
            PlanAction(
                kind="write_file",
                params={
                    "path": "reports/web_fetch.txt",
                    "content_template": (
                        "url={last_web_fetch_url}\n"
                        "request_id={last_web_fetch_request_id}\n"
                        "trace_id={last_web_fetch_trace_id}\n"
                        "preview={last_web_fetch_preview}\n"
                    ),
                },
            ),
            PlanAction(kind="finish", params={"summary": "fetch complete"}),
        ]
    )
    runner = SeedRunner(
        workspace_dir=workspace_dir,
        bridge_client=bridge,
        planner=planner,
        max_steps=4,
    )

    result = asyncio.run(runner.run("fetch one page"))

    assert result.success is True
    assert bridge.fetch_calls == 1
    report = workspace_dir / "reports" / "web_fetch.txt"
    assert report.exists()
    payload = report.read_text(encoding="ascii")
    assert "https://example.com/" in payload
    assert "fetch-req-1" in payload
    assert "example preview text" in payload


def test_scripted_planner_can_render_browser_via_bridge_and_write_artifacts(tmp_path):
    workspace_dir = tmp_path / "workspace"
    make_local_task_workspace(workspace_dir)

    bridge = FakeBridgeClient()
    planner = ScriptedPlanner(
        [
            PlanAction(kind="bridge_browser_render", params={"url": "https://example.com/"}),
            PlanAction(
                kind="write_file",
                params={
                    "path": "reports/browser_report.md",
                    "content_template": (
                        "url={last_browser_final_url}\n"
                        "title={last_browser_title}\n"
                        "request_id={last_browser_request_id}\n"
                        "trace_id={last_browser_trace_id}\n"
                        "preview={last_browser_text_preview}\n"
                    ),
                },
            ),
            PlanAction(
                kind="write_binary_base64",
                params={
                    "path": "reports/browser.png",
                    "base64_template": "{last_browser_screenshot_base64}",
                },
            ),
            PlanAction(kind="finish", params={"summary": "browser render complete"}),
        ]
    )
    runner = SeedRunner(
        workspace_dir=workspace_dir,
        bridge_client=bridge,
        planner=planner,
        max_steps=5,
    )

    result = asyncio.run(runner.run("render one page"))

    assert result.success is True
    assert bridge.browser_render_calls == 1
    report = workspace_dir / "reports" / "browser_report.md"
    screenshot = workspace_dir / "reports" / "browser.png"
    assert report.exists()
    assert screenshot.exists()
    assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    payload = report.read_text(encoding="ascii")
    assert "Fixture Browser Title" in payload
    assert "browser-req-1" in payload
    assert "Rendered browser text preview" in payload


def test_scripted_planner_can_capture_single_url_browser_packet(tmp_path):
    workspace_dir = tmp_path / "workspace"
    make_local_task_workspace(workspace_dir)

    class CaptureBridgeClient(FakeBridgeClient):
        async def browser_render(self, *, url: str) -> BrowserRenderResponse:
            self.browser_render_calls += 1
            rendered_text = "Packet heading\n" + ("detail line\n" * 32)
            screenshot_base64 = (
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO2Wb6wAAAAASUVORK5CYII="
            )
            return BrowserRenderResponse(
                request_id="browser-capture-req-1",
                trace_id="browser-capture-trace-1",
                normalized_url=url,
                final_url=url,
                http_status=200,
                page_title="Capture Packet Title",
                meta_description="Capture packet description",
                rendered_text=rendered_text,
                rendered_text_sha256="capture-text-hash",
                text_bytes=len(rendered_text.encode("utf-8")),
                text_truncated=False,
                screenshot_png_base64=screenshot_base64,
                screenshot_sha256="capture-image-hash",
                screenshot_bytes=len(base64.b64decode(screenshot_base64)),
                redirect_chain=[],
                observed_hosts=["example.com"],
                resolved_ips=["93.184.216.34"],
                followable_links=[],
            )

    bridge = CaptureBridgeClient()
    planner = ScriptedPlanner(
        [
            PlanAction(kind="bridge_status"),
            PlanAction(kind="bridge_browser_render", params={"url": "{input_url}"}),
            PlanAction(
                kind="write_file",
                params={
                    "path": "research/current_capture.md",
                    "content_template": (
                        "input_url={input_url}\n"
                        "final_url={last_browser_final_url}\n"
                        "request_id={last_browser_request_id}\n"
                        "trace_id={last_browser_trace_id}\n"
                        "text_bytes={last_browser_text_bytes}\n"
                        "text_truncated={last_browser_text_truncated}\n"
                    ),
                },
            ),
            PlanAction(
                kind="write_file",
                params={
                    "path": "research/current_rendered_text.txt",
                    "content_template": "{last_browser_rendered_text}",
                },
            ),
            PlanAction(
                kind="write_binary_base64",
                params={
                    "path": "research/current_screenshot.png",
                    "base64_template": "{last_browser_screenshot_base64}",
                },
            ),
            PlanAction(kind="finish", params={"summary": "capture packet complete"}),
        ]
    )
    runner = SeedRunner(
        workspace_dir=workspace_dir,
        bridge_client=bridge,
        planner=planner,
        max_steps=6,
    )

    input_url = "https://example.com/reference"
    result = asyncio.run(
        runner.run("capture one allowlisted page", input_url=input_url)
    )

    assert result.success is True
    assert bridge.browser_render_calls == 1
    summary = json.loads(
        (workspace_dir / "run_outputs" / "latest_seed_run.json").read_text(encoding="ascii")
    )
    assert summary["input_url"] == input_url

    report = (workspace_dir / "research" / "current_capture.md").read_text(encoding="utf-8")
    captured_text = (workspace_dir / "research" / "current_rendered_text.txt").read_text(
        encoding="utf-8"
    )
    screenshot = workspace_dir / "research" / "current_screenshot.png"

    assert f"input_url={input_url}" in report
    assert "request_id=browser-capture-req-1" in report
    assert "trace_id=browser-capture-trace-1" in report
    assert "text_truncated=False" in report
    assert captured_text.startswith("Packet heading\n")
    assert captured_text.endswith("detail line\n")
    assert len(captured_text) > 200
    assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_scripted_planner_can_follow_browser_href_via_bridge_and_write_artifacts(tmp_path):
    workspace_dir = tmp_path / "workspace"
    make_local_task_workspace(workspace_dir)

    bridge = FakeBridgeClient()
    planner = ScriptedPlanner(
        [
            PlanAction(kind="bridge_browser_render", params={"url": "https://example.com/source"}),
            PlanAction(
                kind="bridge_browser_follow_href",
                params={
                    "source_url": "https://example.com/source",
                    "target_url": "{last_browser_first_followable_target_url}",
                },
            ),
            PlanAction(
                kind="write_file",
                params={
                    "path": "reports/browser_follow_report.md",
                    "content_template": (
                        "source={last_browser_follow_source_url}\n"
                        "target={last_browser_follow_requested_target_url}\n"
                        "final={last_browser_follow_final_url}\n"
                        "title={last_browser_follow_title}\n"
                        "request_id={last_browser_follow_request_id}\n"
                        "trace_id={last_browser_follow_trace_id}\n"
                        "preview={last_browser_follow_text_preview}\n"
                    ),
                },
            ),
            PlanAction(
                kind="write_binary_base64",
                params={
                    "path": "reports/browser_follow.png",
                    "base64_template": "{last_browser_follow_screenshot_base64}",
                },
            ),
            PlanAction(kind="finish", params={"summary": "browser follow complete"}),
        ]
    )
    runner = SeedRunner(
        workspace_dir=workspace_dir,
        bridge_client=bridge,
        planner=planner,
        max_steps=6,
    )

    result = asyncio.run(runner.run("follow one safe href"))

    assert result.success is True
    assert bridge.browser_render_calls == 1
    assert bridge.browser_follow_href_calls == 1
    report = workspace_dir / "reports" / "browser_follow_report.md"
    screenshot = workspace_dir / "reports" / "browser_follow.png"
    assert report.exists()
    assert screenshot.exists()
    assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    payload = report.read_text(encoding="ascii")
    assert "https://example.com/source" in payload
    assert "https://example.com/follow-target" in payload
    assert "Fixture Browser Follow Title" in payload
    assert "browser-follow-req-1" in payload


def test_scripted_planner_can_build_single_source_answer_packet(tmp_path):
    workspace_dir = tmp_path / "workspace"
    make_local_task_workspace(workspace_dir)

    class ReturnedModelBridgeClient(FakeBridgeClient):
        async def chat(self, *, model: str, message: str) -> ChatCompletionResponse:
            response = await super().chat(model=model, message=message)
            return response.model_copy(update={"model": f"{model}-resolved"})

    bridge = ReturnedModelBridgeClient()
    planner = ScriptedPlanner(
        [
            PlanAction(kind="bridge_status"),
            PlanAction(kind="bridge_browser_render", params={"url": "{input_url}"}),
            PlanAction(
                kind="bridge_chat",
                params={
                    "model": "stage1-deterministic",
                    "message": (
                        "Q: {task}\n"
                        "Title: {last_browser_title}\n"
                        "Text:\n{last_browser_rendered_text}"
                    ),
                },
            ),
            PlanAction(
                kind="write_file",
                params={
                    "path": "research/current_answer.md",
                    "content_template": (
                        "question={task}\n"
                        "input_url={input_url}\n"
                        "final_url={last_browser_final_url}\n"
                        "title={last_browser_title}\n"
                        "request_id={last_browser_request_id}\n"
                        "trace_id={last_browser_trace_id}\n"
                        "text_bytes={last_browser_text_bytes}\n"
                        "text_truncated={last_browser_text_truncated}\n"
                        "llm_model={last_bridge_chat_model}\n"
                        "answer={last_bridge_chat}\n"
                    ),
                },
            ),
            PlanAction(
                kind="write_file",
                params={
                    "path": "research/current_capture.md",
                    "content_template": (
                        "input_url={input_url}\n"
                        "final_url={last_browser_final_url}\n"
                        "request_id={last_browser_request_id}\n"
                        "trace_id={last_browser_trace_id}\n"
                    ),
                },
            ),
            PlanAction(
                kind="write_file",
                params={
                    "path": "research/current_rendered_text.txt",
                    "content_template": "{last_browser_rendered_text}",
                },
            ),
            PlanAction(
                kind="write_binary_base64",
                params={
                    "path": "research/current_screenshot.png",
                    "base64_template": "{last_browser_screenshot_base64}",
                },
            ),
            PlanAction(kind="finish", params={"summary": "answer packet complete"}),
        ]
    )
    runner = SeedRunner(
        workspace_dir=workspace_dir,
        bridge_client=bridge,
        planner=planner,
        max_steps=8,
    )

    input_url = "https://example.com/reference"
    question = "What does this page say?"
    result = asyncio.run(runner.run(question, input_url=input_url))

    assert result.success is True
    assert bridge.browser_render_calls == 1
    assert bridge.chat_calls == 1
    assert bridge.chat_requests[-1]["model"] == "stage1-deterministic"
    assert f"Q: {question}" in bridge.chat_requests[-1]["message"]
    assert "Title: Fixture Browser Title" in bridge.chat_requests[-1]["message"]
    assert "Rendered browser text preview" in bridge.chat_requests[-1]["message"]
    assert (
        deterministic_usage(
            [ChatMessage(role="user", content=bridge.chat_requests[-1]["message"])]
        ).total_tokens
        <= DEFAULT_LLM_BUDGET_TOKEN_CAP
    )

    summary = json.loads(
        (workspace_dir / "run_outputs" / "latest_seed_run.json").read_text(encoding="ascii")
    )
    assert summary["input_url"] == input_url
    assert any(step["kind"] == "bridge_browser_render" for step in summary["steps"])
    assert any(step["kind"] == "bridge_chat" for step in summary["steps"])
    assert any(
        step["kind"] == "bridge_chat"
        and step["result"]["model"] == "stage1-deterministic-resolved"
        for step in summary["steps"]
    )

    answer = (workspace_dir / "research" / "current_answer.md").read_text(encoding="utf-8")
    capture = (workspace_dir / "research" / "current_capture.md").read_text(encoding="utf-8")
    captured_text = (workspace_dir / "research" / "current_rendered_text.txt").read_text(
        encoding="utf-8"
    )
    screenshot = workspace_dir / "research" / "current_screenshot.png"

    assert f"question={question}" in answer
    assert f"input_url={input_url}" in answer
    assert "request_id=browser-req-1" in answer
    assert "trace_id=browser-trace-1" in answer
    assert "llm_model=stage1-deterministic-resolved" in answer
    assert "answer=scripted reply:" in answer
    assert f"input_url={input_url}" in capture
    assert captured_text == "Rendered browser text preview"
    assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_scripted_planner_can_build_follow_answer_packet(tmp_path):
    workspace_dir = tmp_path / "workspace"
    make_local_task_workspace(workspace_dir)

    class FollowAnswerBridgeClient(FakeBridgeClient):
        async def browser_render(self, *, url: str) -> BrowserRenderResponse:
            self.browser_render_calls += 1
            screenshot_base64 = (
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO2Wb6wAAAAASUVORK5CYII="
            )
            return BrowserRenderResponse(
                request_id="browser-follow-source-req-1",
                trace_id="browser-follow-source-trace-1",
                normalized_url=url,
                final_url=url,
                http_status=200,
                page_title="Follow Source Title",
                meta_description="Follow source description",
                rendered_text="SOURCE PAGE ONLY",
                rendered_text_sha256="follow-source-text-hash",
                text_bytes=len("SOURCE PAGE ONLY".encode("utf-8")),
                text_truncated=False,
                screenshot_png_base64=screenshot_base64,
                screenshot_sha256="follow-source-image-hash",
                screenshot_bytes=len(base64.b64decode(screenshot_base64)),
                redirect_chain=[],
                observed_hosts=["example.com"],
                resolved_ips=["93.184.216.34"],
                followable_links=[
                    BrowserFollowLink(
                        text="Follow same origin target",
                        target_url="https://example.com/follow-target",
                        same_origin=True,
                    )
                ],
            )

        async def browser_follow_href(
            self,
            *,
            source_url: str,
            target_url: str,
        ) -> BrowserFollowHrefResponse:
            self.browser_follow_href_calls += 1
            screenshot_base64 = (
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO2Wb6wAAAAASUVORK5CYII="
            )
            return BrowserFollowHrefResponse(
                request_id="browser-follow-answer-req-1",
                trace_id="browser-follow-answer-trace-1",
                source_url=source_url,
                source_final_url=source_url,
                requested_target_url=target_url,
                matched_link_text="Follow same origin target",
                follow_hop_count=1,
                navigation_history=[source_url, target_url],
                normalized_url=target_url,
                final_url=target_url,
                http_status=200,
                page_title="Follow Answer Target Title",
                meta_description="Follow answer target description",
                rendered_text="FOLLOWED PAGE ONLY",
                rendered_text_sha256="follow-answer-text-hash",
                text_bytes=len("FOLLOWED PAGE ONLY".encode("utf-8")),
                text_truncated=False,
                screenshot_png_base64=screenshot_base64,
                screenshot_sha256="follow-answer-image-hash",
                screenshot_bytes=len(base64.b64decode(screenshot_base64)),
                redirect_chain=[],
                observed_hosts=["example.com"],
                resolved_ips=["93.184.216.34"],
            )

        async def chat(self, *, model: str, message: str) -> ChatCompletionResponse:
            response = await super().chat(model=model, message=message)
            return response.model_copy(update={"model": f"{model}-resolved"})

    bridge = FollowAnswerBridgeClient()
    planner = ScriptedPlanner(
        [
            PlanAction(kind="bridge_status"),
            PlanAction(kind="bridge_browser_render", params={"url": "{input_url}"}),
            PlanAction(
                kind="bridge_browser_follow_href",
                params={
                    "source_url": "{input_url}",
                    "target_url": "{follow_target_url}",
                },
            ),
            PlanAction(
                kind="bridge_chat",
                params={
                    "model": "stage1-deterministic",
                    "message": (
                        "Q: {task}\n"
                        "Source input URL: {input_url}\n"
                        "Source final URL: {last_browser_follow_source_final_url}\n"
                        "Requested target URL: {follow_target_url}\n"
                        "Matched link text: {last_browser_follow_matched_link_text}\n"
                        "Followed final URL: {last_browser_follow_final_url}\n"
                        "Page title: {last_browser_follow_title}\n"
                        "Text:\n{last_browser_follow_rendered_text}"
                    ),
                },
            ),
            PlanAction(
                kind="write_file",
                params={
                    "path": "research/current_follow_answer.md",
                    "content_template": (
                        "question={task}\n"
                        "source_input_url={input_url}\n"
                        "source_final_url={last_browser_follow_source_final_url}\n"
                        "requested_target_url={follow_target_url}\n"
                        "matched_link_text={last_browser_follow_matched_link_text}\n"
                        "followed_final_url={last_browser_follow_final_url}\n"
                        "title={last_browser_follow_title}\n"
                        "request_id={last_browser_follow_request_id}\n"
                        "trace_id={last_browser_follow_trace_id}\n"
                        "text_bytes={last_browser_follow_text_bytes}\n"
                        "text_truncated={last_browser_follow_text_truncated}\n"
                        "llm_model={last_bridge_chat_model}\n"
                        "answer={last_bridge_chat}\n"
                    ),
                },
            ),
            PlanAction(
                kind="write_file",
                params={
                    "path": "research/current_follow_capture.md",
                    "content_template": (
                        "source_input_url={input_url}\n"
                        "source_final_url={last_browser_follow_source_final_url}\n"
                        "requested_target_url={follow_target_url}\n"
                        "matched_link_text={last_browser_follow_matched_link_text}\n"
                        "followed_final_url={last_browser_follow_final_url}\n"
                        "title={last_browser_follow_title}\n"
                        "request_id={last_browser_follow_request_id}\n"
                        "trace_id={last_browser_follow_trace_id}\n"
                        "text_bytes={last_browser_follow_text_bytes}\n"
                        "text_truncated={last_browser_follow_text_truncated}\n"
                    ),
                },
            ),
            PlanAction(
                kind="write_file",
                params={
                    "path": "research/current_follow_rendered_text.txt",
                    "content_template": "{last_browser_follow_rendered_text}",
                },
            ),
            PlanAction(
                kind="write_binary_base64",
                params={
                    "path": "research/current_follow_screenshot.png",
                    "base64_template": "{last_browser_follow_screenshot_base64}",
                },
            ),
            PlanAction(kind="finish", params={"summary": "follow answer packet complete"}),
        ]
    )
    runner = SeedRunner(
        workspace_dir=workspace_dir,
        bridge_client=bridge,
        planner=planner,
        max_steps=10,
    )

    input_url = "https://example.com/follow-source"
    follow_target_url = "https://example.com/follow-target"
    question = "What does the followed page say?"
    result = asyncio.run(
        runner.run(
            question,
            input_url=input_url,
            follow_target_url=follow_target_url,
        )
    )

    assert result.success is True
    assert bridge.browser_render_calls == 1
    assert bridge.browser_follow_href_calls == 1
    assert bridge.chat_calls == 1
    assert bridge.chat_requests[-1]["model"] == "stage1-deterministic"
    assert f"Q: {question}" in bridge.chat_requests[-1]["message"]
    assert "Matched link text: Follow same origin target" in bridge.chat_requests[-1]["message"]
    assert "Text:\nFOLLOWED PAGE ONLY" in bridge.chat_requests[-1]["message"]
    assert "SOURCE PAGE ONLY" not in bridge.chat_requests[-1]["message"]

    summary = json.loads(
        (workspace_dir / "run_outputs" / "latest_seed_run.json").read_text(encoding="ascii")
    )
    assert summary["input_url"] == input_url
    assert summary["follow_target_url"] == follow_target_url
    assert any(step["kind"] == "bridge_browser_follow_href" for step in summary["steps"])
    assert any(
        step["kind"] == "bridge_chat"
        and step["result"]["model"] == "stage1-deterministic-resolved"
        for step in summary["steps"]
    )

    answer = (workspace_dir / "research" / "current_follow_answer.md").read_text(
        encoding="utf-8"
    )
    capture = (workspace_dir / "research" / "current_follow_capture.md").read_text(
        encoding="utf-8"
    )
    captured_text = (
        workspace_dir / "research" / "current_follow_rendered_text.txt"
    ).read_text(encoding="utf-8")
    screenshot = workspace_dir / "research" / "current_follow_screenshot.png"

    assert f"question={question}" in answer
    assert f"source_input_url={input_url}" in answer
    assert f"requested_target_url={follow_target_url}" in answer
    assert "matched_link_text=Follow same origin target" in answer
    assert "title=Follow Answer Target Title" in answer
    assert "request_id=browser-follow-answer-req-1" in answer
    assert "trace_id=browser-follow-answer-trace-1" in answer
    assert "text_bytes=18" in answer
    assert "text_truncated=False" in answer
    assert "llm_model=stage1-deterministic-resolved" in answer
    assert "answer=scripted reply:" in answer
    assert f"source_input_url={input_url}" in capture
    assert f"requested_target_url={follow_target_url}" in capture
    assert "followed_final_url=https://example.com/follow-target" in capture
    assert captured_text == "FOLLOWED PAGE ONLY"
    assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
