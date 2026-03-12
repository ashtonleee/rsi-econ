import httpx

from shared.schemas import EgressProbeResult


async def attempt_egress(
    *,
    probe_kind: str,
    target_url: str,
) -> EgressProbeResult:
    try:
        async with httpx.AsyncClient(timeout=3.0, follow_redirects=False) as client:
            response = await client.get(target_url)
        return EgressProbeResult(
            probe_kind=probe_kind,
            target_url=target_url,
            reachable=True,
            error=f"unexpected status={response.status_code}",
        )
    except Exception as exc:  # noqa: BLE001
        return EgressProbeResult(
            probe_kind=probe_kind,
            target_url=target_url,
            reachable=False,
            error=f"{type(exc).__name__}: {exc}",
        )
