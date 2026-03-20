import json
from pathlib import Path
import subprocess
from urllib.parse import urlencode, urlparse

import httpx

from shared.schemas import BridgeStatusReport, ProposalListResponse, ProposalRecord


ROOT = Path(__file__).resolve().parent.parent


class BridgeAPIError(RuntimeError):
    """Raised when the operator console cannot read a bridge-backed surface."""


class BridgeUnavailableError(BridgeAPIError):
    """Raised when the bridge cannot be contacted at all."""


class BridgeNotFoundError(BridgeAPIError):
    """Raised when a specific bridge-backed record does not exist."""


class BridgeAPI:
    def __init__(
        self,
        *,
        base_url: str,
        operator_token: str | None,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        command_runner=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.operator_token = operator_token
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.command_runner = command_runner or subprocess.run

    async def get_status(self) -> BridgeStatusReport:
        payload = await self._request_json("GET", "/status")
        return BridgeStatusReport.model_validate(payload)

    async def list_proposals(self, *, status: str | None = None) -> list[ProposalRecord]:
        params = {"status": status} if status else None
        payload = await self._request_json("GET", "/proposals", params=params)
        response = ProposalListResponse.model_validate(payload)
        return sorted(response.proposals, key=lambda record: record.created_at, reverse=True)

    async def get_proposal(self, proposal_id: str) -> ProposalRecord:
        payload = await self._request_json("GET", f"/proposals/{proposal_id}")
        return ProposalRecord.model_validate(payload)

    async def decide_proposal(
        self,
        proposal_id: str,
        *,
        decision: str,
        reason: str,
    ) -> ProposalRecord:
        payload = await self._request_json(
            "POST",
            f"/proposals/{proposal_id}/decide",
            payload={"decision": decision, "reason": reason},
        )
        return ProposalRecord.model_validate(payload)

    async def execute_proposal(self, proposal_id: str) -> ProposalRecord:
        payload = await self._request_json(
            "POST",
            f"/proposals/{proposal_id}/execute",
        )
        return ProposalRecord.model_validate(payload)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict | None = None,
    ) -> dict:
        if not self.operator_token:
            raise BridgeUnavailableError("RSI_OPERATOR_TOKEN is not set")

        try:
            return await self._http_request_json(method, path, params=params, payload=payload)
        except BridgeUnavailableError as exc:
            if self.transport is not None or not _may_use_compose_exec(self.base_url):
                raise exc
            try:
                return self._compose_exec_request_json(method, path, params=params, payload=payload)
            except BridgeUnavailableError as fallback_exc:
                raise BridgeUnavailableError(
                    f"{exc}; docker compose bridge fallback failed: {fallback_exc}"
                ) from fallback_exc

    async def _http_request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict | None = None,
    ) -> dict:
        headers = {"Authorization": f"Bearer {self.operator_token}"}
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            try:
                response = await client.request(method, path, params=params, json=payload)
            except httpx.RequestError as exc:
                raise BridgeUnavailableError(f"bridge unavailable: {exc}") from exc

        if response.status_code == 404:
            detail = _error_detail(response)
            raise BridgeNotFoundError(detail or "record not found")
        if response.status_code in {401, 403}:
            detail = _error_detail(response)
            raise BridgeAPIError(detail or f"bridge rejected operator request ({response.status_code})")
        if response.is_error:
            detail = _error_detail(response)
            raise BridgeAPIError(detail or f"bridge returned HTTP {response.status_code}")

        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise BridgeAPIError("bridge returned invalid JSON") from exc

    def _compose_exec_request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict | None = None,
    ) -> dict:
        full_url = f"{self.base_url}{path}"
        if params:
            full_url = f"{full_url}?{urlencode(params)}"

        code = (
            "import json\n"
            "import httpx\n"
            f"method = {method!r}\n"
            f"url = {full_url!r}\n"
            f"headers = {json.dumps({'Authorization': f'Bearer {self.operator_token}'})!r}\n"
            f"payload = {json.dumps(payload or {})!r}\n"
            f"timeout = {self.timeout_seconds!r}\n"
            "with httpx.Client(timeout=timeout) as client:\n"
            "    response = client.request(method, url, headers=json.loads(headers), json=json.loads(payload) if payload != '{}' else None)\n"
            "try:\n"
            "    body = response.json()\n"
            "except Exception:\n"
            "    body = {'detail': response.text}\n"
            "print(json.dumps({'status_code': response.status_code, 'body': body}))\n"
        )
        result = self.command_runner(
            ["docker", "compose", "exec", "-T", "bridge", "python", "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            raise BridgeUnavailableError(stderr or "docker compose exec bridge failed")

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BridgeAPIError("docker bridge fallback returned invalid JSON") from exc

        status_code = int(payload.get("status_code", 500))
        body = payload.get("body", {})
        if not isinstance(body, dict):
            body = {"detail": str(body)}
        if status_code == 404:
            raise BridgeNotFoundError(str(body.get("detail", "record not found")))
        if status_code in {401, 403}:
            raise BridgeAPIError(str(body.get("detail", f"bridge rejected operator request ({status_code})")))
        if status_code >= 400:
            raise BridgeAPIError(str(body.get("detail", f"bridge returned HTTP {status_code}")))
        return body


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        text = response.text.strip()
        return text

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
    return ""


def _may_use_compose_exec(base_url: str) -> bool:
    parsed = urlparse(base_url)
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
