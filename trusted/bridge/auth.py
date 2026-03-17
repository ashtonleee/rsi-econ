from fastapi import HTTPException


def resolve_identity(
    authorization_header: str | None,
    *,
    agent_token: str,
    operator_token: str,
) -> str:
    """Resolve caller identity from Authorization header.

    Returns "agent" or "operator". Raises HTTP 401 for missing,
    malformed, or unrecognized tokens.
    """
    if authorization_header is None:
        raise HTTPException(status_code=401, detail="missing authorization header")

    if not authorization_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="malformed authorization header")

    token = authorization_header[len("Bearer "):]

    if token == agent_token:
        return "agent"
    if token == operator_token:
        return "operator"

    raise HTTPException(status_code=401, detail="unrecognized token")
