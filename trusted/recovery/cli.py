import argparse
import json
import sys
from uuid import uuid4

from shared.config import agent_settings, bridge_settings
from trusted.bridge.app import build_surfaces, log_path_for, state_path_for, web_defaults_for
from trusted.recovery.store import WorkspaceRecoveryStore
from trusted.state.store import TrustedStateManager


def build_store() -> WorkspaceRecoveryStore:
    bridge = bridge_settings()
    agent = agent_settings()
    return WorkspaceRecoveryStore(
        workspace_dir=agent.workspace_dir,
        recovery_dir=bridge.checkpoint_dir,
        baseline_source_dir=bridge.seed_baseline_dir,
    )


def build_state_manager(store: WorkspaceRecoveryStore) -> TrustedStateManager:
    settings = bridge_settings()
    return TrustedStateManager(
        canonical_log_path=log_path_for(settings),
        operational_state_path=state_path_for(settings),
        budget_total=settings.llm_budget_token_cap,
        budget_unit=settings.budget_unit,
        stage=settings.stage,
        surfaces=build_surfaces(),
        recovery_defaults=store.current_recovery_summary(),
        web_defaults=web_defaults_for(settings),
    )


def append_recovery_event(
    manager: TrustedStateManager,
    *,
    event_type: str,
    outcome: str,
    summary: dict,
):
    manager.append_event(
        event_type=event_type,
        actor="operator",
        source_service="operator_recovery_cli",
        request_id=uuid4().hex,
        trace_id=uuid4().hex,
        outcome=outcome,
        summary=summary,
    )


def list_payload(store: WorkspaceRecoveryStore) -> dict:
    layout = store.ensure_layout()
    return {
        "checkpoint_dir": layout["checkpoint_dir"],
        "baseline": layout["baseline"],
        "checkpoints": store.list_checkpoints(),
    }


def command_create_checkpoint(args) -> int:
    store = build_store()
    manager = build_state_manager(store)
    checkpoint = store.create_checkpoint(label=args.label)
    append_recovery_event(
        manager,
        event_type="checkpoint_created",
        outcome="success",
        summary={
            "action": "create_checkpoint",
            "checkpoint": checkpoint,
            "recovery": store.current_recovery_summary(),
        },
    )
    print(json.dumps(checkpoint, sort_keys=True))
    return 0


def command_list_checkpoints(_args) -> int:
    store = build_store()
    print(json.dumps(list_payload(store), sort_keys=True))
    return 0


def command_restore_checkpoint(args) -> int:
    store = build_store()
    manager = build_state_manager(store)
    try:
        checkpoint = store.restore_checkpoint(args.checkpoint_id)
    except FileNotFoundError as exc:
        append_recovery_event(
            manager,
            event_type="recovery_error",
            outcome="error",
            summary={
                "action": "restore_checkpoint",
                "requested_checkpoint_id": args.checkpoint_id,
                "detail": str(exc),
                "recovery": store.current_recovery_summary(),
            },
        )
        print(str(exc), file=sys.stderr)
        return 1

    append_recovery_event(
        manager,
        event_type="checkpoint_restored",
        outcome="success",
        summary={
            "action": "restore_checkpoint",
            "checkpoint": checkpoint,
            "recovery": store.current_recovery_summary(),
        },
    )
    print(json.dumps(checkpoint, sort_keys=True))
    return 0


def command_reset_workspace(_args) -> int:
    store = build_store()
    manager = build_state_manager(store)
    baseline = store.reset_to_seed_baseline()
    append_recovery_event(
        manager,
        event_type="workspace_reset",
        outcome="success",
        summary={
            "action": "reset_workspace_to_seed_baseline",
            "baseline_id": baseline["baseline_id"],
            "baseline": baseline,
            "recovery": store.current_recovery_summary(),
        },
    )
    print(json.dumps(baseline, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trusted workspace recovery controls")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-checkpoint")
    create.add_argument("--label", default=None)
    create.set_defaults(func=command_create_checkpoint)

    listing = subparsers.add_parser("list-checkpoints")
    listing.set_defaults(func=command_list_checkpoints)

    restore = subparsers.add_parser("restore-checkpoint")
    restore.add_argument("checkpoint_id")
    restore.set_defaults(func=command_restore_checkpoint)

    reset = subparsers.add_parser("reset-workspace-to-seed-baseline")
    reset.set_defaults(func=command_reset_workspace)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
