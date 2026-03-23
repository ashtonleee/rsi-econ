from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse
from uuid import uuid4

from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import Response

try:
    from notifier import notify, process_event_files
except ImportError:
    # Fallback for testing outside Docker
    def notify(event_type: str, message: str, data: dict | None = None) -> bool:
        return False
    def process_event_files() -> None:
        pass


EVENT_POLL_INTERVAL = int(os.getenv("EVENT_POLL_INTERVAL", "10"))
_event_poller_stop = threading.Event()


def _event_poller() -> None:
    """Background thread that drains supervisor event files and sends notifications."""
    print("[bridge] event poller thread started", flush=True)
    while not _event_poller_stop.is_set():
        try:
            process_event_files()
        except Exception as exc:
            print(f"[bridge] event poller error: {exc}", flush=True)
        _event_poller_stop.wait(EVENT_POLL_INTERVAL)


LOGGER = logging.getLogger(__name__)
MODEL_CACHE_TTL_SECONDS = 60.0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _coerce_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field_name}") from exc


def _coerce_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field_name}") from exc


def _extract_domain(record: dict[str, Any]) -> str | None:
    domain = str(record.get("domain") or "").strip().lower()
    if domain:
        return domain
    url = str(record.get("url") or "").strip()
    if not url:
        return None
    return (urlparse(url).hostname or "").strip().lower() or None


def _proposal_path(proposals_dir: Path, proposal_id: str) -> Path:
    return proposals_dir / f"{proposal_id}.json"


def _remaining_phase(remaining_ratio: float) -> str:
    if remaining_ratio > 0.5:
        return "full"
    if remaining_ratio > 0.2:
        return "moderate"
    if remaining_ratio >= 0.05:
        return "conserve"
    return "wrapup"


@dataclass
class SpendTotals:
    spent_usd: float = 0.0
    total_tokens: int = 0
    request_count: int = 0


class SpendTracker:
    def __init__(
        self,
        *,
        budget_usd: float,
        usage_log_path: str | os.PathLike[str],
        litellm_base_url: str,
    ) -> None:
        self.budget_usd = budget_usd
        self.usage_log_path = Path(usage_log_path)
        self.litellm_models_url = f"{litellm_base_url.rstrip('/')}/v1/models"
        self._lock = threading.Lock()
        self._totals = SpendTotals()
        self._cached_models: list[str] = []
        self._models_loaded_at = 0.0
        self._load_existing_usage()

    def _load_existing_usage(self) -> None:
        if not self.usage_log_path.exists():
            return
        totals = SpendTotals()
        for raw_line in self.usage_log_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
                totals.spent_usd += _coerce_float(record.get("cost_usd", 0.0), "cost_usd")
                totals.total_tokens += _coerce_int(record.get("total_tokens", 0), "total_tokens")
                totals.request_count += 1
            except (json.JSONDecodeError, ValueError) as exc:
                LOGGER.warning("Ignoring malformed usage log entry: %s", exc)
        self._totals = totals

    def _append_event(self, event: dict[str, Any]) -> None:
        self.usage_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True))
            handle.write("\n")

    def record_usage(self, payload: Any) -> bool:
        try:
            if isinstance(payload, list):
                if len(payload) != 1:
                    raise ValueError("invalid payload list")
                payload = payload[0]
            if not isinstance(payload, dict):
                raise ValueError("invalid payload")
            usage = payload["usage"]
            if isinstance(usage, dict):
                prompt_tokens = _coerce_int(usage.get("prompt_tokens"), "prompt_tokens")
                completion_tokens = _coerce_int(usage.get("completion_tokens"), "completion_tokens")
                total_tokens = usage.get("total_tokens")
                if total_tokens is None:
                    total_tokens = prompt_tokens + completion_tokens
                total_tokens = _coerce_int(total_tokens, "total_tokens")
            else:
                raise KeyError
            response_cost = _coerce_float(payload.get("response_cost"), "response_cost")
            model = str(payload["model"]).strip()
            if not model:
                raise ValueError("invalid model")
        except KeyError:
            try:
                prompt_tokens = _coerce_int(payload.get("prompt_tokens"), "prompt_tokens")
                completion_tokens = _coerce_int(payload.get("completion_tokens"), "completion_tokens")
                total_tokens = _coerce_int(payload.get("total_tokens"), "total_tokens")
                response_cost = _coerce_float(payload.get("response_cost"), "response_cost")
                model = str(payload.get("model") or "").strip()
                if not model:
                    raise ValueError("invalid model")
            except (AttributeError, TypeError, ValueError) as exc:
                LOGGER.warning("Ignoring malformed LiteLLM usage webhook: %s", exc)
                return False
        except (TypeError, ValueError) as exc:
            LOGGER.warning("Ignoring malformed LiteLLM usage webhook: %s", exc)
            return False

        event = {
            "timestamp": _utcnow(),
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cost_usd": response_cost,
        }
        with self._lock:
            self._append_event(event)
            self._totals.spent_usd += response_cost
            self._totals.total_tokens += total_tokens
            self._totals.request_count += 1
        return True

    def models_available(self) -> list[str]:
        now = time.monotonic()
        with self._lock:
            cached_models = list(self._cached_models)
            cache_age = now - self._models_loaded_at
        if cached_models and cache_age < MODEL_CACHE_TTL_SECONDS:
            return cached_models

        try:
            with urllib_request.urlopen(self.litellm_models_url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib_error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            LOGGER.warning("Unable to refresh LiteLLM model list: %s", exc)
            return cached_models

        data = payload.get("data")
        if not isinstance(data, list):
            LOGGER.warning("LiteLLM /v1/models returned an unexpected payload shape")
            return cached_models

        models: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("id") or item.get("model_name") or item.get("name") or "").strip()
            if name:
                models.append(name)

        with self._lock:
            self._cached_models = models
            self._models_loaded_at = now
        return list(models)

    def wallet_payload(self) -> dict[str, Any]:
        with self._lock:
            spent_usd = self._totals.spent_usd
            total_tokens = self._totals.total_tokens
            request_count = self._totals.request_count

        budget_usd = self.budget_usd
        remaining_usd = max(budget_usd - spent_usd, 0.0)
        remaining_ratio = 0.0 if budget_usd <= 0 else remaining_usd / budget_usd
        avg_cost = spent_usd / request_count if request_count else 0.0
        return {
            "budget_usd": round(budget_usd, 6),
            "spent_usd": round(spent_usd, 6),
            "remaining_usd": round(remaining_usd, 6),
            "phase": _remaining_phase(remaining_ratio),
            "total_tokens": total_tokens,
            "total_requests": request_count,
            "avg_cost_per_request": round(avg_cost, 6),
            "models_available": self.models_available(),
            "note": "Budget is managed by trusted infrastructure. You cannot modify it.",
        }


def _append_allowlist_domain(allowlist_path: Path, domain: str) -> bool:
    normalized = domain.strip().lower()
    if not normalized:
        return False
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[str] = []
    if allowlist_path.exists():
        existing = [line.strip().lower() for line in allowlist_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if normalized in existing:
        return False
    with allowlist_path.open("a", encoding="utf-8") as handle:
        if allowlist_path.stat().st_size > 0:
            handle.write("\n")
        handle.write(normalized)
    return True


def _load_proposal_or_404(proposals_dir: Path, proposal_id: str) -> tuple[Path, dict[str, Any]]:
    path = _proposal_path(proposals_dir, proposal_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="proposal not found")
    return path, _read_json(path)


class GitManager:
    """Manages the trusted git repository for the agent workspace."""

    def __init__(
        self,
        repo_dir: Path,
        workspace_dir: Path,
        seed_dir: Path,
        stats_dir: Path | None = None,
    ) -> None:
        self.repo_dir = repo_dir
        self.workspace_dir = workspace_dir
        self.seed_dir = seed_dir
        self.stats_dir = stats_dir
        if self.stats_dir:
            self.stats_dir.mkdir(parents=True, exist_ok=True)

    def _run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "GIT_DIR": str(self.repo_dir / ".git"), "GIT_WORK_TREE": str(self.workspace_dir)}
        return subprocess.run(
            ["git", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def is_initialized(self) -> bool:
        return (self.repo_dir / ".git").exists()

    def init_repo(self) -> dict[str, Any]:
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        if self.is_initialized():
            head = self._run_git("rev-parse", "HEAD")
            return {"status": "already_initialized", "hash": head.stdout.strip()}
        # Copy seed files into workspace
        if self.seed_dir.exists():
            for item in self.seed_dir.iterdir():
                if item.is_file() and item.name != ".git":
                    shutil.copy2(str(item), str(self.workspace_dir / item.name))
        # Init repo with GIT_DIR pointing to repo_dir
        subprocess.run(["git", "init", str(self.repo_dir)], capture_output=True, check=False)
        self._run_git("add", "-A")
        self._run_git("commit", "-m", "seed")
        head = self._run_git("rev-parse", "HEAD")
        return {"status": "initialized", "hash": head.stdout.strip()}

    # --- Core git operations ---

    def commit(
        self,
        message: str,
        wallet_state: dict[str, Any] | None = None,
        source: str = "agent_edit",
    ) -> dict[str, Any]:
        self._run_git("add", "-A")
        status = self._run_git("status", "--porcelain")
        if not status.stdout.strip():
            return {"changed": False, "hash": self._run_git("rev-parse", "HEAD").stdout.strip()}
        self._run_git("commit", "-m", message)
        head = self._run_git("rev-parse", "HEAD")
        commit_hash = head.stdout.strip()
        try:
            self._write_commit_stats(commit_hash, message, wallet_state, source)
            self._increment_branch_commit_count()
        except Exception as exc:
            LOGGER.warning("Failed to write commit stats for %s: %s", commit_hash[:7], exc)
        return {"changed": True, "hash": commit_hash}

    def revert(
        self,
        ref: str,
        wallet_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = self._run_git("checkout", ref, "--", ".")
        if result.returncode != 0:
            return {"error": result.stderr.strip() or "revert failed"}
        self._run_git("add", "-A")
        msg = f"reverted to {ref}"
        self._run_git("commit", "-m", msg)
        head = self._run_git("rev-parse", "HEAD")
        commit_hash = head.stdout.strip()
        try:
            self._write_commit_stats(commit_hash, msg, wallet_state, "supervisor_revert")
        except Exception as exc:
            LOGGER.warning("Failed to write revert stats for %s: %s", commit_hash[:7], exc)
        return {"hash": commit_hash, "ref": ref}

    def log(self, count: int = 20) -> list[dict[str, str]]:
        result = self._run_git("log", f"-{count}", "--format=%H|%s|%aI")
        if result.returncode != 0:
            return []
        entries = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("|", 2)
            if len(parts) == 3:
                entries.append({"hash": parts[0], "message": parts[1], "date": parts[2]})
        return entries

    def show(self, ref: str, path: str) -> str | None:
        result = self._run_git("show", f"{ref}:{path}")
        if result.returncode != 0:
            return None
        return result.stdout

    def diff(self, ref1: str, ref2: str) -> str:
        result = self._run_git("diff", ref1, ref2)
        return result.stdout

    def diff_stat(self, ref: str = "HEAD~1") -> str:
        result = self._run_git("diff", "--stat", ref)
        return result.stdout.strip()

    def fsck(self) -> bool:
        result = self._run_git("fsck", "--no-dangling")
        if result.returncode == 0:
            return True
        self._run_git("gc", "--auto")
        return self._run_git("fsck", "--no-dangling").returncode == 0

    def push(self) -> dict[str, Any]:
        result = self._run_git("remote", "get-url", "origin")
        if result.returncode != 0:
            return {"status": "no_remote"}
        push = self._run_git("push", "origin", "HEAD")
        if push.returncode == 0:
            return {"status": "pushed"}
        return {"status": "push_failed", "error": push.stderr.strip()[:200]}

    # --- Branch / checkout / merge ---

    def current_branch(self) -> str | None:
        result = self._run_git("branch", "--show-current")
        branch = result.stdout.strip()
        return branch if branch else None

    def _session_root(self) -> str | None:
        """Return the session/{name} root of the current branch, or None.

        Handles both session branches (session/foo) and experiment branches
        (session/foo--exp/bar) by splitting on '--exp/' first.
        """
        branch = self.current_branch()
        if not branch:
            return None
        # If on an experiment branch, extract the session root before --exp/
        if "--exp/" in branch:
            session_part = branch.split("--exp/")[0]
            parts = session_part.split("/")
            if len(parts) >= 2 and parts[0] == "session":
                return session_part
            return None
        parts = branch.split("/")
        if len(parts) >= 2 and parts[0] == "session":
            return f"{parts[0]}/{parts[1]}"
        return None

    def _validate_branch_namespace(self, target: str) -> str | None:
        """Return error message if target is outside current session namespace, or None if valid."""
        root = self._session_root()
        if root is None:
            return "not on a session branch"
        # Allow the session root itself, or experiment branches (root--exp/...)
        if target != root and not target.startswith(f"{root}--exp/"):
            return f"branch '{target}' is outside session namespace '{root}'"
        return None

    def create_branch(self, name: str) -> dict[str, Any]:
        root = self._session_root()
        if root is None:
            return {"error": "not on a session branch"}
        # Experiment branches use -- separator to avoid git ref path conflict:
        # session/foo (branch file) can coexist with session/foo--exp/bar (different prefix)
        if not name.startswith(f"{root}--exp/"):
            return {"error": f"branch name must start with '{root}--exp/'"}
        suffix = name[len(f"{root}--exp/"):]
        if not suffix or not re.match(r"^[a-zA-Z0-9_-]+$", suffix):
            return {"error": "experiment name must be non-empty and contain only [a-zA-Z0-9_-]"}
        result = self._run_git("checkout", "-b", name)
        if result.returncode != 0:
            return {"error": result.stderr.strip() or "branch creation failed"}
        try:
            parent_hash = self._run_git("rev-parse", "HEAD").stdout.strip()
            self._write_branch_metadata(name, root, parent_hash, "active")
        except Exception as exc:
            LOGGER.warning("Failed to write branch metadata for %s: %s", name, exc)
        return {"branch": name, "status": "created"}

    def checkout(self, ref: str) -> dict[str, Any]:
        err = self._validate_branch_namespace(ref)
        if err:
            return {"error": err}
        verify = self._run_git("rev-parse", "--verify", ref)
        if verify.returncode != 0:
            return {"error": f"ref '{ref}' does not exist"}
        # Commit any pending changes before switching (stats files, etc.)
        self._run_git("add", "-A")
        status = self._run_git("status", "--porcelain")
        if status.stdout.strip():
            self._run_git("commit", "-m", f"auto-commit before checkout to {ref}")
        result = self._run_git("checkout", ref)
        if result.returncode != 0:
            return {"error": result.stderr.strip() or "checkout failed"}
        # Write restart marker so supervisor restarts the agent with new code
        restart_marker = self.workspace_dir / ".restart_requested"
        restart_marker.write_text("checkout", encoding="utf-8")
        return {"branch": ref, "status": "checked_out", "restart": True}

    def merge(self, branch: str) -> dict[str, Any]:
        err = self._validate_branch_namespace(branch)
        if err:
            return {"error": err}
        verify = self._run_git("rev-parse", "--verify", branch)
        if verify.returncode != 0:
            return {"error": f"branch '{branch}' does not exist"}
        result = self._run_git("merge", branch, "--no-edit")
        if result.returncode != 0:
            # Check for merge conflict
            if "CONFLICT" in result.stdout or "Automatic merge failed" in result.stdout:
                self._run_git("merge", "--abort")
                return {"status": "conflict", "message": result.stdout.strip()[:500]}
            return {"error": result.stderr.strip() or "merge failed"}
        head = self._run_git("rev-parse", "HEAD")
        commit_hash = head.stdout.strip()
        try:
            self._update_branch_metadata(branch, "merged")
        except Exception as exc:
            LOGGER.warning("Failed to update branch metadata for %s: %s", branch, exc)
        return {"status": "merged", "hash": commit_hash}

    def list_branches(self) -> dict[str, Any]:
        result = self._run_git("branch", "--format=%(refname:short)")
        branches = [b.strip() for b in result.stdout.strip().split("\n") if b.strip()]
        return {"current": self.current_branch(), "branches": branches}

    def delete_branch(self, name: str) -> dict[str, Any]:
        current = self.current_branch()
        if name == current:
            return {"error": "cannot delete the current branch"}
        root = self._session_root()
        if root and name == root:
            return {"error": "cannot delete the session root branch"}
        result = self._run_git("branch", "-D", name)
        if result.returncode != 0:
            return {"error": result.stderr.strip() or "delete failed"}
        try:
            self._update_branch_metadata(name, "abandoned")
        except Exception as exc:
            LOGGER.warning("Failed to update branch metadata for %s: %s", name, exc)
        return {"branch": name, "status": "deleted"}

    # --- Tags ---

    def tag(self, name: str, ref: str = "HEAD") -> dict[str, Any]:
        resolved = self._run_git("rev-parse", ref)
        if resolved.returncode != 0:
            return {"error": f"ref '{ref}' does not exist"}
        result = self._run_git("tag", name, ref)
        if result.returncode != 0:
            return {"error": result.stderr.strip() or "tag creation failed"}
        return {"tag": name, "ref": resolved.stdout.strip()}

    def list_tags(self) -> list[dict[str, str]]:
        result = self._run_git("tag", "-l", "--sort=-creatordate",
                               "--format=%(refname:short)|%(objectname:short)")
        if result.returncode != 0:
            return []
        tags = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("|", 1)
            if len(parts) == 2:
                tags.append({"name": parts[0], "hash": parts[1]})
        return tags

    # --- Per-commit stats ---

    def _parse_diff_stat(self, raw: str) -> dict[str, int]:
        """Parse 'git diff --stat' summary line into structured data."""
        result = {"files_changed": 0, "insertions": 0, "deletions": 0}
        if not raw.strip():
            return result
        lines = [line for line in raw.strip().split("\n") if line.strip()]
        if not lines:
            return result
        summary = lines[-1]
        m_files = re.search(r"(\d+) files? changed", summary)
        m_ins = re.search(r"(\d+) insertions?\(\+\)", summary)
        m_del = re.search(r"(\d+) deletions?\(-\)", summary)
        if m_files:
            result["files_changed"] = int(m_files.group(1))
        if m_ins:
            result["insertions"] = int(m_ins.group(1))
        if m_del:
            result["deletions"] = int(m_del.group(1))
        return result

    def _write_commit_stats(
        self,
        commit_hash: str,
        message: str,
        wallet_state: dict[str, Any] | None,
        source: str,
    ) -> None:
        """Write per-commit stats to both workspace and bridge-only directories."""
        branch = self.current_branch() or "detached"
        raw_stat = self._run_git("diff", "--stat", "HEAD~1", "HEAD")
        diff_stat = self._parse_diff_stat(raw_stat.stdout)

        stats = {
            "hash": commit_hash,
            "short_hash": commit_hash[:7],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "branch": branch,
            "message": message,
            "source": source,
            "wallet": {
                "budget_usd": wallet_state.get("budget_usd", 0),
                "spent_usd": wallet_state.get("spent_usd", 0),
                "remaining_usd": wallet_state.get("remaining_usd", 0),
                "total_requests": wallet_state.get("total_requests", 0),
                "avg_cost_per_request": wallet_state.get("avg_cost_per_request", 0),
            } if wallet_state else None,
            "diff_stat": diff_stat,
        }

        stats_json = json.dumps(stats, indent=2, sort_keys=True)
        filename = f"{commit_hash}.json"

        # Write to workspace (agent-discoverable)
        ws_dir = self.workspace_dir / ".git-stats"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / filename).write_text(stats_json, encoding="utf-8")

        # Write to bridge-only trusted directory
        if self.stats_dir:
            (self.stats_dir / filename).write_text(stats_json, encoding="utf-8")

    # --- Branch metadata sidecar ---

    @staticmethod
    def _sanitize_branch_name(name: str) -> str:
        """Convert branch name to safe filename: session/foo/exp/bar -> session_foo_exp_bar"""
        return name.replace("/", "_")

    def _write_branch_metadata(
        self,
        branch: str,
        parent_branch: str,
        created_from_commit: str,
        status: str,
    ) -> None:
        """Write branch metadata sidecar to workspace and bridge-only dirs."""
        metadata = {
            "branch": branch,
            "parent_branch": parent_branch,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_from_commit": created_from_commit,
            "status": status,
            "merged_at": None,
            "abandoned_at": None,
            "commits": 0,
        }
        self._save_branch_metadata(branch, metadata)

    def _update_branch_metadata(self, branch: str, new_status: str) -> None:
        """Update an existing branch metadata sidecar status."""
        metadata = self._load_branch_metadata(branch)
        if metadata is None:
            # No metadata file exists; create a minimal one
            metadata = {
                "branch": branch,
                "parent_branch": "unknown",
                "created_at": "unknown",
                "created_from_commit": "unknown",
                "status": new_status,
                "merged_at": None,
                "abandoned_at": None,
                "commits": 0,
            }
        metadata["status"] = new_status
        now = datetime.now(timezone.utc).isoformat()
        if new_status == "merged":
            metadata["merged_at"] = now
        elif new_status == "abandoned":
            metadata["abandoned_at"] = now
        self._save_branch_metadata(branch, metadata)

    def _increment_branch_commit_count(self) -> None:
        """Increment the commit count in the current branch's metadata."""
        branch = self.current_branch()
        if not branch:
            return
        metadata = self._load_branch_metadata(branch)
        if metadata is None:
            return
        metadata["commits"] = metadata.get("commits", 0) + 1
        self._save_branch_metadata(branch, metadata)

    def _save_branch_metadata(self, branch: str, metadata: dict[str, Any]) -> None:
        """Write branch metadata to both workspace and bridge-only dirs."""
        safe_name = self._sanitize_branch_name(branch)
        filename = f"{safe_name}.json"
        content = json.dumps(metadata, indent=2, sort_keys=True)

        ws_branches = self.workspace_dir / ".git-stats" / "branches"
        ws_branches.mkdir(parents=True, exist_ok=True)
        (ws_branches / filename).write_text(content, encoding="utf-8")

        if self.stats_dir:
            bridge_branches = self.stats_dir / "branches"
            bridge_branches.mkdir(parents=True, exist_ok=True)
            (bridge_branches / filename).write_text(content, encoding="utf-8")

    def _load_branch_metadata(self, branch: str) -> dict[str, Any] | None:
        """Load branch metadata, preferring bridge-only copy."""
        safe_name = self._sanitize_branch_name(branch)
        filename = f"{safe_name}.json"
        # Try bridge-only dir first (trusted)
        if self.stats_dir:
            path = self.stats_dir / "branches" / filename
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        # Fall back to workspace
        path = self.workspace_dir / ".git-stats" / "branches" / filename
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None


def create_app(
    proposals_dir: str | os.PathLike[str] | None = None,
    *,
    usage_log_path: str | os.PathLike[str] | None = None,
    allowlist_path: str | os.PathLike[str] | None = None,
    litellm_base_url: str | None = None,
    budget_usd: float | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _event_poller_stop.clear()
        poller = threading.Thread(target=_event_poller, daemon=True)
        poller.start()
        LOGGER.info("Event poller thread started (interval=%ds)", EVENT_POLL_INTERVAL)
        yield
        _event_poller_stop.set()

    app = FastAPI(lifespan=lifespan)
    default_proposals_dir = os.getenv("PROPOSALS_DIR", "./state/proposals")
    default_usage_log_path = os.getenv("LLM_USAGE_LOG_PATH", "/var/log/rsi/llm_usage.jsonl")
    default_allowlist_path = os.getenv("PROXY_ALLOWLIST_PATH", "/etc/rsi/proxy_allowlist.txt")
    default_litellm_base_url = os.getenv("LITELLM_URL", "http://litellm:4000")
    default_budget_usd = _coerce_float(os.getenv("RSI_BUDGET_USD", "5.00"), "RSI_BUDGET_USD")

    app.state.proposals_dir = Path(proposals_dir or default_proposals_dir)
    app.state.proposals_dir.mkdir(parents=True, exist_ok=True)
    app.state.allowlist_path = Path(allowlist_path or default_allowlist_path)
    app.state.spend_tracker = SpendTracker(
        budget_usd=budget_usd if budget_usd is not None else default_budget_usd,
        usage_log_path=usage_log_path or default_usage_log_path,
        litellm_base_url=litellm_base_url or default_litellm_base_url,
    )

    app.state.git_manager = GitManager(
        repo_dir=Path(os.getenv("GIT_REPO_DIR", "/var/lib/rsi/git-repo")),
        workspace_dir=Path(os.getenv("GIT_WORKSPACE_DIR", "/var/lib/rsi/workspace")),
        seed_dir=Path(os.getenv("SEED_DIR", "/opt/seed")),
        stats_dir=Path(os.getenv("GIT_STATS_DIR", "/var/lib/rsi/git-stats")),
    )
    app.state.litellm_base_url = litellm_base_url or default_litellm_base_url
    app.state.operator_messages_dir = Path(os.getenv("OPERATOR_MESSAGES_DIR", "/var/lib/rsi/operator_messages"))
    app.state.operator_messages_dir.mkdir(parents=True, exist_ok=True)
    app.state.provider_proposals_dir = Path(os.getenv("PROVIDER_PROPOSALS_DIR", "/var/lib/rsi/provider_proposals"))
    app.state.provider_proposals_dir.mkdir(parents=True, exist_ok=True)
    app.state._budget_warned_25 = False
    app.state._budget_warned_10 = False

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        process_event_files()
        return {"status": "ok"}

    @app.post("/llm_usage")
    def llm_usage(payload: Any = Body(...)) -> dict[str, str]:
        app.state.spend_tracker.record_usage(payload)
        # Check budget thresholds for notifications
        w = app.state.spend_tracker.wallet_payload()
        budget = w.get("budget_usd", 0)
        remaining = w.get("remaining_usd", 0)
        if budget > 0:
            pct = (remaining / budget) * 100
            thresholds = [
                (10, "_budget_warned_10", "budget_critical", "🔴 CRITICAL"),
                (25, "_budget_warned_25", "budget_25", "🟠 Low"),
                (50, "_budget_warned_50", "budget_50", "🟡 Half spent"),
                (75, "_budget_warned_75", "budget_75", "📊 Update"),
            ]
            for threshold, flag, event, label in thresholds:
                if pct <= threshold and not getattr(app.state, flag, False):
                    setattr(app.state, flag, True)
                    notify(event, f"{label} — ${remaining:.2f} left of ${budget:.2f} ({pct:.0f}%)")
        return {"status": "ok"}

    @app.get("/wallet")
    def wallet() -> dict[str, Any]:
        return app.state.spend_tracker.wallet_payload()

    @app.post("/proposals")
    def create_proposal(payload: dict[str, Any]) -> dict[str, str]:
        proposal_id = str(uuid4())
        record = {
            **payload,
            "proposal_id": proposal_id,
            "status": "pending",
            "created_at": _utcnow(),
        }
        domain = _extract_domain(record)
        if domain and "domain" not in record:
            record["domain"] = domain
        path = _proposal_path(app.state.proposals_dir, proposal_id)
        _write_json(path, record)
        notify("proposal_submitted", f"Proposal: {domain or 'unknown'} ({payload.get('method', '?')} {payload.get('url', '?')[:80]})")
        return {"proposal_id": proposal_id}

    @app.get("/proposals")
    def list_proposals() -> list[dict[str, Any]]:
        proposals = [_read_json(path) for path in sorted(app.state.proposals_dir.glob("*.json"))]
        return sorted(proposals, key=lambda item: item.get("created_at", ""))

    @app.get("/proposals/{proposal_id}")
    def get_proposal(proposal_id: str) -> dict[str, Any]:
        _path, record = _load_proposal_or_404(app.state.proposals_dir, proposal_id)
        return record

    @app.post("/proposals/{proposal_id}/approve")
    def approve_proposal(proposal_id: str) -> dict[str, Any]:
        path, record = _load_proposal_or_404(app.state.proposals_dir, proposal_id)
        if record.get("status") != "pending":
            raise HTTPException(status_code=409, detail="proposal already decided")
        record["status"] = "approved"
        record["approved_at"] = _utcnow()
        domain = _extract_domain(record)
        if domain:
            _append_allowlist_domain(app.state.allowlist_path, domain)
        _write_json(path, record)
        notify("proposal_approved", f"Approved: {domain or proposal_id[:8]}")
        return record

    @app.post("/proposals/{proposal_id}/reject")
    def reject_proposal(proposal_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        path, record = _load_proposal_or_404(app.state.proposals_dir, proposal_id)
        if record.get("status") != "pending":
            raise HTTPException(status_code=409, detail="proposal already decided")
        record["status"] = "rejected"
        record["rejected_at"] = _utcnow()
        reason = None if payload is None else payload.get("reason")
        if reason is not None:
            record["reason"] = str(reason)
        _write_json(path, record)
        return record

    @app.get("/proposals/{proposal_id}/status")
    def proposal_status(proposal_id: str) -> dict[str, Any]:
        _path, record = _load_proposal_or_404(app.state.proposals_dir, proposal_id)
        return {"status": record.get("status", "pending")}

    @app.post("/operator/inject")
    def operator_inject(payload: dict[str, Any]) -> dict[str, str]:
        message = str(payload.get("message", "")).strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        entry = {
            "timestamp": _utcnow(),
            "message": message,
            "model_override": payload.get("model_override"),
        }
        pending_path = app.state.operator_messages_dir / "pending.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        with pending_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        notify("operator_injection", f"Operator: {message[:100]}")
        return {"status": "queued"}

    # --- Provider hot-add endpoints ---

    @app.post("/providers/propose")
    def propose_provider(payload: dict[str, Any]) -> dict[str, Any]:
        """Agent proposes a new LLM provider for operator to add."""
        name = str(payload.get("name", "")).strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")

        # Check for existing proposal with same name
        for path in app.state.provider_proposals_dir.glob("*.json"):
            try:
                existing = _read_json(path)
                if existing.get("name") == name:
                    return {
                        "proposal_id": existing.get("proposal_id", path.stem),
                        "status": existing.get("status", "pending_operator"),
                        "note": "duplicate — proposal already exists",
                    }
            except Exception:
                continue

        proposal_id = str(uuid4())
        record = {
            "proposal_id": proposal_id,
            "name": name,
            "provider": str(payload.get("provider", name)).strip(),
            "model_id": str(payload.get("model_id", "")).strip(),
            "signup_url": str(payload.get("signup_url", "")).strip(),
            "free_tier": str(payload.get("free_tier", "")).strip(),
            "needs_api_key": bool(payload.get("needs_api_key", True)),
            "notes": str(payload.get("notes", "")).strip(),
            "status": "pending_operator",
            "created_at": _utcnow(),
        }
        _write_json(app.state.provider_proposals_dir / f"{proposal_id}.json", record)
        notify("provider_proposed", f"New provider: {name} ({record['model_id'] or '?'})")
        return {"proposal_id": proposal_id, "status": "pending_operator"}

    @app.get("/providers")
    def list_providers() -> dict[str, Any]:
        """List active and proposed providers."""
        # Active: derive from LiteLLM models
        active = []
        models = app.state.spend_tracker.models_available()
        if models:
            active.append({
                "name": "openrouter",
                "models": models,
                "key_configured": True,
            })

        # Proposed: read proposal files
        proposed = []
        for path in sorted(app.state.provider_proposals_dir.glob("*.json")):
            try:
                record = _read_json(path)
                proposed.append({
                    "proposal_id": record.get("proposal_id", path.stem),
                    "name": record.get("name", ""),
                    "provider": record.get("provider", ""),
                    "model_id": record.get("model_id", ""),
                    "status": record.get("status", "pending_operator"),
                    "signup_url": record.get("signup_url", ""),
                    "free_tier": record.get("free_tier", ""),
                })
            except Exception:
                continue
        return {"active": active, "proposed": proposed}

    @app.get("/providers/proposals/{proposal_id}")
    def get_provider_proposal(proposal_id: str) -> dict[str, Any]:
        path = app.state.provider_proposals_dir / f"{proposal_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="provider proposal not found")
        return _read_json(path)

    @app.post("/providers/proposals/{proposal_id}/activate")
    def activate_provider(proposal_id: str) -> dict[str, Any]:
        """Mark a provider proposal as active (called by cli/providers.py after key setup)."""
        path = app.state.provider_proposals_dir / f"{proposal_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="provider proposal not found")
        record = _read_json(path)
        record["status"] = "active"
        record["activated_at"] = _utcnow()
        _write_json(path, record)
        notify("provider_activated", f"Provider added: {record.get('name', '?')} — {record.get('model_id', '?')}")
        return record

    # --- Git API endpoints (trusted git repo management) ---

    @app.post("/git/init")
    def git_init() -> dict[str, Any]:
        return app.state.git_manager.init_repo()

    @app.post("/git/commit")
    def git_commit(payload: dict[str, Any]) -> dict[str, Any]:
        message = str(payload.get("message", "")).strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        source = str(payload.get("source", "agent_edit"))
        wallet_state = app.state.spend_tracker.wallet_payload()
        return app.state.git_manager.commit(message, wallet_state=wallet_state, source=source)

    @app.post("/git/revert/{ref:path}")
    def git_revert(ref: str) -> dict[str, Any]:
        wallet_state = app.state.spend_tracker.wallet_payload()
        result = app.state.git_manager.revert(ref, wallet_state=wallet_state)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.get("/git/log")
    def git_log(count: int = 20) -> list[dict[str, str]]:
        return app.state.git_manager.log(count)

    @app.get("/git/show/{ref:path}")
    def git_show(ref: str, path: str = "main.py") -> dict[str, Any]:
        content = app.state.git_manager.show(ref, path)
        if content is None:
            raise HTTPException(status_code=404, detail=f"{path} not found at {ref}")
        return {"ref": ref, "path": path, "content": content}

    @app.get("/git/diff")
    def git_diff(ref1: str = "HEAD~1", ref2: str = "HEAD") -> dict[str, str]:
        return {"ref1": ref1, "ref2": ref2, "diff": app.state.git_manager.diff(ref1, ref2)}

    @app.get("/git/fsck")
    def git_fsck() -> dict[str, bool]:
        return {"ok": app.state.git_manager.fsck()}

    @app.post("/git/push")
    def git_push() -> dict[str, Any]:
        return app.state.git_manager.push()

    # --- Git evolution endpoints (branch/checkout/merge/tag) ---

    @app.post("/git/branch")
    def git_branch(payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name", "")).strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        result = app.state.git_manager.create_branch(name)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.post("/git/checkout")
    def git_checkout(payload: dict[str, Any]) -> dict[str, Any]:
        ref = str(payload.get("ref", "")).strip()
        if not ref:
            raise HTTPException(status_code=400, detail="ref is required")
        result = app.state.git_manager.checkout(ref)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.post("/git/merge")
    def git_merge(payload: dict[str, Any]) -> dict[str, Any]:
        branch = str(payload.get("branch", "")).strip()
        if not branch:
            raise HTTPException(status_code=400, detail="branch is required")
        result = app.state.git_manager.merge(branch)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.get("/git/branches")
    def git_branches() -> dict[str, Any]:
        return app.state.git_manager.list_branches()

    @app.delete("/git/branch/{name:path}")
    def git_delete_branch(name: str) -> dict[str, Any]:
        result = app.state.git_manager.delete_branch(name)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.post("/git/tag")
    def git_tag(payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name", "")).strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        ref = str(payload.get("ref", "HEAD")).strip()
        result = app.state.git_manager.tag(name, ref)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.get("/git/tags")
    def git_tags() -> list[dict[str, str]]:
        return app.state.git_manager.list_tags()

    # --- Operator tooling endpoints ---

    @app.post("/summarize")
    def summarize(payload: dict[str, Any]) -> dict[str, Any]:
        """LLM-powered summary of agent activity. Cost tracked separately from agent budget."""
        text = str(payload.get("text", ""))[:4000]
        max_tokens = int(payload.get("max_tokens", 150))
        if not text.strip():
            return {"summary": "(no content to summarize)"}
        body = json.dumps({
            "model": "default",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Summarize what this AI agent is doing in 2-3 concise sentences. "
                        "Focus on actions taken and discoveries made. "
                        "Be specific about provider names and findings."
                    ),
                },
                {"role": "user", "content": text},
            ],
            "max_tokens": max_tokens,
        }).encode("utf-8")
        try:
            req = urllib_request.Request(
                f"{app.state.litellm_base_url}/v1/chat/completions",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib_request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            summary = result["choices"][0]["message"]["content"]
            return {"summary": summary}
        except Exception as exc:
            return {"summary": f"(summarization failed: {exc})"}

    @app.get("/agent/screenshot")
    def agent_screenshot() -> Response:
        """Serve the latest browser screenshot from the agent workspace."""
        ws = Path(os.getenv("GIT_WORKSPACE_DIR", "/var/lib/rsi/workspace"))
        path = ws / "latest_screenshot.png"
        if not path.exists():
            raise HTTPException(status_code=404, detail="no screenshot available")
        age = time.time() - path.stat().st_mtime
        if age > 300:
            raise HTTPException(status_code=404, detail="screenshot stale (>5 min)")
        return Response(content=path.read_bytes(), media_type="image/png")

    @app.get("/agent/status")
    def agent_status() -> dict[str, Any]:
        """Serve agent context info from the workspace volume."""
        ws = Path(os.getenv("GIT_WORKSPACE_DIR", "/var/lib/rsi/workspace"))
        result: dict[str, Any] = {}
        kpath = ws / "knowledge.json"
        if kpath.exists():
            try:
                result["knowledge"] = json.loads(kpath.read_text("utf-8"))
            except Exception:
                pass
        spath = ws / "agent_status.json"
        if spath.exists():
            try:
                result["agent_status"] = json.loads(spath.read_text("utf-8"))
            except Exception:
                pass
        result["paused"] = (ws / ".paused").exists()
        return result

    @app.get("/agent/reasoning")
    def agent_reasoning(lines: int = 5) -> dict[str, Any]:
        """Return the last N entries from reasoning.jsonl."""
        ws = Path(os.getenv("GIT_WORKSPACE_DIR", "/var/lib/rsi/workspace"))
        rpath = ws / "reasoning.jsonl"
        if not rpath.exists():
            return {"entries": []}
        try:
            all_lines = rpath.read_text("utf-8").strip().split("\n")
            tail = all_lines[-lines:] if lines > 0 else []
            entries = []
            for raw in tail:
                if not raw.strip():
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
            return {"entries": entries}
        except Exception:
            return {"entries": []}

    # --- Exa.ai search endpoint ---

    @app.post("/search")
    def search(payload: dict[str, Any]) -> dict[str, Any]:
        """Proxy search queries to Exa.ai and return structured results."""
        query = str(payload.get("query", "")).strip()
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        num_results = min(int(payload.get("num_results", 5)), 20)

        exa_body = json.dumps({
            "query": query,
            "numResults": num_results,
            "type": "auto",
            "contents": {"text": {"maxCharacters": 1000}},
        }).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        exa_api_key = os.getenv("EXA_API_KEY", "").strip()
        if exa_api_key:
            headers["x-api-key"] = exa_api_key

        search_log_path = Path("/var/log/rsi/search.jsonl")
        try:
            req = urllib_request.Request(
                "https://api.exa.ai/search",
                data=exa_body,
                headers=headers,
                method="POST",
            )
            with urllib_request.urlopen(req, timeout=15) as resp:
                exa_response = json.loads(resp.read().decode("utf-8"))

            results = []
            for item in exa_response.get("results", []):
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "text": item.get("text", ""),
                })

            # Log the search
            try:
                search_log_path.parent.mkdir(parents=True, exist_ok=True)
                with search_log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "timestamp": _utcnow(),
                        "query": query,
                        "num_results": len(results),
                        "success": True,
                    }, sort_keys=True) + "\n")
            except Exception:
                pass

            return {"results": results}

        except Exception as exc:
            # Log the failed search
            try:
                search_log_path.parent.mkdir(parents=True, exist_ok=True)
                with search_log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "timestamp": _utcnow(),
                        "query": query,
                        "num_results": 0,
                        "success": False,
                        "error": str(exc)[:200],
                    }, sort_keys=True) + "\n")
            except Exception:
                pass

            return {"results": [], "error": str(exc)[:200]}

    return app


app = create_app()
