import asyncio
import json
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from operator_console.bridge_api import BridgeAPI, BridgeAPIError, BridgeNotFoundError
from operator_console.config import ConsoleSettings, console_settings
from operator_console.data import RepoData
from operator_console.launches import LaunchBusyError, LaunchManager, LaunchRequest
from operator_console.live_state import build_live_snapshot
from operator_console.plan_catalog import build_launch_plan_options, default_launch_plan_name


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
PROPOSAL_STATUSES = ["pending", "approved", "rejected", "executing", "executed", "failed"]


def create_app(
    *,
    settings: ConsoleSettings | None = None,
    bridge_api: BridgeAPI | None = None,
    repo_data: RepoData | None = None,
    launch_manager: LaunchManager | None = None,
) -> FastAPI:
    settings = settings or console_settings()
    bridge_api = bridge_api or BridgeAPI(
        base_url=settings.bridge_url,
        operator_token=settings.operator_token,
    )
    repo_data = repo_data or RepoData(settings)
    launch_manager = launch_manager or LaunchManager(settings, repo_data=repo_data)

    app = FastAPI(title="RSI Operator Console")
    app.state.settings = settings
    app.state.bridge_api = bridge_api
    app.state.repo_data = repo_data
    app.state.launch_manager = launch_manager
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["pretty_json"] = lambda value: json.dumps(value, indent=2, sort_keys=True)
    templates.env.filters["tone"] = status_tone
    app.state.templates = templates

    def render_page(
        request: Request,
        template_name: str,
        *,
        status_code: int = 200,
        **context,
    ) -> HTMLResponse:
        base_context = {
            "request": request,
            "bridge_url": settings.bridge_url,
            "workspace_dir": str(settings.workspace_dir),
            "trusted_state_dir": str(settings.trusted_state_dir),
            "proposal_statuses": PROPOSAL_STATUSES,
            "flash_message": request.query_params.get("flash", ""),
            "flash_level": request.query_params.get("flash_level", "ok"),
        }
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context={**base_context, **context},
            status_code=status_code,
        )

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        bridge_error = ""
        status = None
        latest_pending = None
        active_launch = launch_manager.get_active_launch()
        try:
            status = await bridge_api.get_status()
            pending = await bridge_api.list_proposals(status="pending")
            latest_pending = pending[0] if pending else None
        except BridgeAPIError as exc:
            bridge_error = str(exc)

        active_snapshot = None
        if active_launch is not None:
            raw_snapshot = launch_manager.get_snapshot(active_launch.launch_id)
            active_proposals, proposal_error = await load_snapshot_proposals(raw_snapshot)
            active_snapshot = build_live_snapshot(
                raw_snapshot,
                related_proposals=active_proposals,
                allowlist_hosts=status.web.allowlist_hosts if status else None,
                bridge_error=bridge_error or proposal_error,
            )

        runs = repo_data.list_run_summaries()
        latest_run = runs[0] if runs else None
        return render_page(
            request,
            "home.html",
            page_title="Operator Console",
            status=status,
            bridge_error=bridge_error,
            latest_run=latest_run,
            latest_pending=latest_pending,
            active_launch=active_launch,
            active_snapshot=active_snapshot,
            run_count=len(runs),
        )

    @app.get("/launches", response_class=HTMLResponse)
    async def launches(request: Request) -> HTMLResponse:
        plan_options = build_launch_plan_options(launch_manager.list_seed_plans())
        return render_page(
            request,
            "launches.html",
            page_title="Launches",
            launch_plans=plan_options,
            default_plan_name=default_launch_plan_name([option.name for option in plan_options]),
            launches=launch_manager.list_launches(),
            active_launch=launch_manager.get_active_launch(),
        )

    @app.post("/launches")
    async def create_launch(request: Request) -> RedirectResponse:
        form = await read_simple_form(request)
        try:
            launch_request = LaunchRequest(
                task=form.get("task", "").strip(),
                script=form.get("script", "").strip(),
                launch_mode=form.get("launch_mode", "default").strip(),  # type: ignore[arg-type]
                model=form.get("model", "").strip(),
                input_url=form.get("input_url", "").strip(),
                follow_target_url=form.get("follow_target_url", "").strip(),
                proposal_target_url=form.get("proposal_target_url", "").strip(),
                max_steps=max(1, int(form.get("max_steps", "8").strip() or "8")),
            )
            launch = launch_manager.create_launch(launch_request)
        except (AssertionError, FileNotFoundError, LaunchBusyError, ValueError) as exc:
            return redirect_with_flash("/launches", str(exc), level="error")

        return redirect_with_flash(
            f"/launches/{launch.launch_id}",
            "Launch started.",
            level="ok",
        )

    @app.get("/launches/{launch_id}", response_class=HTMLResponse)
    async def launch_detail(request: Request, launch_id: str) -> HTMLResponse:
        try:
            snapshot = await load_live_snapshot(launch_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="launch not found")
        return render_page(
            request,
            "launch_detail.html",
            page_title=f"Launch {launch_id}",
            snapshot=snapshot,
        )

    @app.get("/api/launches/{launch_id}")
    async def launch_snapshot(launch_id: str) -> JSONResponse:
        try:
            snapshot = await load_live_snapshot(launch_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="launch not found")
        return JSONResponse(snapshot)

    @app.get("/api/launches/{launch_id}/stream")
    async def launch_stream(request: Request, launch_id: str, once: int = 0) -> StreamingResponse:
        try:
            await load_live_snapshot(launch_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="launch not found")

        async def event_source():
            last_version = ""
            next_heartbeat = monotonic() + 15.0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    snapshot = await load_live_snapshot(launch_id)
                except FileNotFoundError:
                    break
                if snapshot["version_token"] != last_version:
                    last_version = snapshot["version_token"]
                    next_heartbeat = monotonic() + 15.0
                    yield sse_event("snapshot", snapshot)
                    if once:
                        break
                elif monotonic() >= next_heartbeat:
                    next_heartbeat = monotonic() + 15.0
                    yield sse_event("heartbeat", {"launch_id": launch_id})
                    if once:
                        break
                await asyncio.sleep(1.0)

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/runs", response_class=HTMLResponse)
    async def runs(request: Request) -> HTMLResponse:
        return render_page(
            request,
            "runs.html",
            page_title="Runs",
            runs=repo_data.list_run_summaries(),
        )

    @app.get("/runs/{run_name}", response_class=HTMLResponse)
    async def run_detail(request: Request, run_name: str) -> HTMLResponse:
        try:
            detail = repo_data.load_run_detail(run_name)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="run not found")
        return render_page(
            request,
            "run_detail.html",
            page_title=detail.summary.name,
            detail=detail,
        )

    @app.get("/proposals", response_class=HTMLResponse)
    async def proposals(request: Request, status: str | None = None) -> HTMLResponse:
        selected_status = status if status in PROPOSAL_STATUSES else None
        bridge_error = ""
        proposals = []
        try:
            proposals = await bridge_api.list_proposals(status=selected_status)
        except BridgeAPIError as exc:
            bridge_error = str(exc)
        return render_page(
            request,
            "proposals.html",
            page_title="Proposals",
            proposals=proposals,
            selected_status=selected_status,
            bridge_error=bridge_error,
        )

    @app.get("/proposals/{proposal_id}", response_class=HTMLResponse)
    async def proposal_detail(request: Request, proposal_id: str) -> HTMLResponse:
        bridge_error = ""
        proposal = None
        status_code = 200
        try:
            proposal = await bridge_api.get_proposal(proposal_id)
        except BridgeNotFoundError:
            status_code = 404
        except BridgeAPIError as exc:
            bridge_error = str(exc)
        return render_page(
            request,
            "proposal_detail.html",
            page_title=f"Proposal {proposal_id}",
            proposal_id=proposal_id,
            proposal=proposal,
            bridge_error=bridge_error,
            status_code=status_code,
        )

    @app.post("/proposals/{proposal_id}/approve")
    async def approve_proposal(request: Request, proposal_id: str) -> RedirectResponse:
        return await proposal_action_redirect(
            request,
            proposal_id,
            action="approve",
        )

    @app.post("/proposals/{proposal_id}/reject")
    async def reject_proposal(request: Request, proposal_id: str) -> RedirectResponse:
        return await proposal_action_redirect(
            request,
            proposal_id,
            action="reject",
        )

    @app.post("/proposals/{proposal_id}/execute")
    async def execute_proposal(request: Request, proposal_id: str) -> RedirectResponse:
        return await proposal_action_redirect(
            request,
            proposal_id,
            action="execute",
        )

    @app.get("/artifacts/{artifact_path:path}")
    async def artifact_view(request: Request, artifact_path: str):
        try:
            artifact = repo_data.load_artifact(artifact_path)
        except ValueError:
            raise HTTPException(status_code=404, detail="artifact path rejected")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="artifact not found")

        if artifact.kind == "image":
            assert artifact.path is not None
            return FileResponse(artifact.path)

        return render_page(
            request,
            "artifact.html",
            page_title=artifact.name,
            artifact=artifact,
        )

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    async def load_snapshot_proposals(snapshot: dict) -> tuple[list, str]:
        proposal_ids = snapshot.get("proposal_ids", [])
        if not proposal_ids:
            return [], ""

        proposals = []
        try:
            for proposal_id in proposal_ids:
                proposals.append(await bridge_api.get_proposal(str(proposal_id)))
        except BridgeAPIError as exc:
            return [], str(exc)
        return proposals, ""

    async def load_live_snapshot(launch_id: str) -> dict:
        raw_snapshot = launch_manager.get_snapshot(launch_id)
        proposals, proposal_error = await load_snapshot_proposals(raw_snapshot)

        status = None
        status_error = ""
        try:
            status = await bridge_api.get_status()
        except BridgeAPIError as exc:
            status_error = str(exc)

        return build_live_snapshot(
            raw_snapshot,
            related_proposals=proposals,
            allowlist_hosts=status.web.allowlist_hosts if status else None,
            bridge_error=status_error or proposal_error,
        )

    async def proposal_action_redirect(
        request: Request,
        proposal_id: str,
        *,
        action: str,
    ) -> RedirectResponse:
        form = await read_simple_form(request)
        redirect_to = form.get("redirect_to", "").strip() or f"/proposals/{proposal_id}"
        reason = form.get("reason", "").strip()
        try:
            if action == "execute":
                await bridge_api.execute_proposal(proposal_id)
                message = "Proposal executed."
            else:
                decision = "approve" if action == "approve" else "reject"
                await bridge_api.decide_proposal(proposal_id, decision=decision, reason=reason)
                message = f"Proposal {decision}d."
        except BridgeAPIError as exc:
            return redirect_with_flash(redirect_to, str(exc), level="error")
        return redirect_with_flash(redirect_to, message, level="ok")

    return app


def status_tone(reachable: bool) -> str:
    return "ok" if reachable else "bad"


async def read_simple_form(request: Request) -> dict[str, str]:
    payload = (await request.body()).decode("utf-8")
    parsed = parse_qs(payload, keep_blank_values=True)
    return {
        key: values[-1] if values else ""
        for key, values in parsed.items()
    }


def redirect_with_flash(path: str, message: str, *, level: str) -> RedirectResponse:
    query = urlencode({"flash": message, "flash_level": level})
    separator = "&" if "?" in path else "?"
    return RedirectResponse(url=f"{path}{separator}{query}", status_code=303)


def sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


app = create_app()
