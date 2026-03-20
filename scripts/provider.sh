#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${RSI_PROVIDER_ENV_FILE:-$ROOT_DIR/.env.provider.local}"
TEMPLATE_FILE="${RSI_PROVIDER_TEMPLATE_FILE:-$ROOT_DIR/.env.provider.local.example}"
WORKSPACE_DIR="${RSI_PROVIDER_WORKSPACE_DIR:-$ROOT_DIR/untrusted/agent_workspace}"
ANSWER_PACKET_PLAN="${RSI_PROVIDER_ANSWER_PACKET_PLAN:-$WORKSPACE_DIR/.seed_plans/stage6_answer_packet_provider.json}"
FOLLOW_ANSWER_PACKET_PLAN="${RSI_PROVIDER_FOLLOW_ANSWER_PACKET_PLAN:-$WORKSPACE_DIR/.seed_plans/stage6_follow_answer_packet.json}"
SENTINEL_PROVIDER_KEY="stage1-sentinel-provider-key"

resolve_python_bin() {
    if [[ -n "${PYTHON:-}" ]]; then
        printf '%s\n' "$PYTHON"
        return
    fi
    if command -v python3 >/dev/null 2>&1; then
        printf '%s\n' "python3"
        return
    fi
    if command -v python >/dev/null 2>&1; then
        printf '%s\n' "python"
        return
    fi
    echo "python3 or python is required" >&2
    exit 1
}

PYTHON_BIN="$(resolve_python_bin)"

usage() {
    cat <<EOF
Usage:
  ./scripts/provider.sh init
  ./scripts/provider.sh up
  ./scripts/provider.sh smoke [--model MODEL] [--message MESSAGE]
  ./scripts/provider.sh seed-run --script SCRIPT --task TASK [--input-url URL] [--follow-target-url URL] [--proposal-target-url URL] [--model MODEL] [--max-steps N]
  ./scripts/provider.sh answer-packet --task TASK --input-url URL [--model MODEL] [--max-steps N]
  ./scripts/provider.sh follow-answer-packet --task TASK --input-url URL --follow-target-url URL [--model MODEL] [--max-steps N]

Defaults:
  env file: ${ENV_FILE}
  smoke model: RSI_PROVIDER_SMOKE_MODEL or gpt-4.1-mini
  seed-run model: RSI_PROVIDER_ANSWER_MODEL or gpt-4.1-mini
  answer-packet plan: ${ANSWER_PACKET_PLAN}
  follow-answer-packet plan: ${FOLLOW_ANSWER_PACKET_PLAN}

Create $ENV_FILE from $TEMPLATE_FILE and set OPENAI_API_KEY before using provider mode.
EOF
}

init_provider_env() {
    if [[ -f "$ENV_FILE" ]]; then
        echo "provider env file already exists: $ENV_FILE" >&2
        exit 1
    fi

    cp "$TEMPLATE_FILE" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "created $ENV_FILE from $TEMPLATE_FILE"
}

load_provider_env() {
    if [[ ! -f "$ENV_FILE" ]]; then
        echo "missing provider env file: $ENV_FILE" >&2
        echo "run ./scripts/provider.sh init and set OPENAI_API_KEY" >&2
        exit 1
    fi

    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
}

require_provider_key() {
    if [[ -z "${OPENAI_API_KEY:-}" ]]; then
        echo "OPENAI_API_KEY must be set in $ENV_FILE" >&2
        exit 1
    fi

    if [[ "${OPENAI_API_KEY}" == "$SENTINEL_PROVIDER_KEY" ]]; then
        echo "OPENAI_API_KEY in $ENV_FILE must be a real provider key" >&2
        exit 1
    fi
}

provider_seed_run_default_model() {
    printf '%s' "${RSI_PROVIDER_ANSWER_MODEL:-gpt-4.1-mini}"
}

resolve_plan_path() {
    local script_path="$1"

    if [[ "$script_path" == /* ]]; then
        printf '%s\n' "$script_path"
        return
    fi

    printf '%s\n' "$WORKSPACE_DIR/$script_path"
}

render_seed_run_plan() {
    local source_plan="$1"
    local target_plan="$2"
    local model="$3"

    MODEL="$model" SOURCE_PLAN="$source_plan" TARGET_PLAN="$target_plan" "$PYTHON_BIN" - <<'PY'
import json
import os
import re
from pathlib import Path

source_plan = Path(os.environ["SOURCE_PLAN"])
target_plan = Path(os.environ["TARGET_PLAN"])
model = os.environ["MODEL"]

payload = json.loads(source_plan.read_text(encoding="utf-8"))
checked_answer_paths = {
    "research/current_answer.md",
    "research/current_follow_answer.md",
}

for action in payload:
    if action.get("kind") == "bridge_chat":
        action["model"] = model
    if action.get("kind") == "write_file" and action.get("path") in checked_answer_paths:
        template = action.get("content_template", "")
        if re.search(r"^llm_model=\{last_bridge_chat_model\}$", template, flags=re.MULTILINE) is None:
            raise SystemExit(
                f"{action['path']} must expose llm_model={{last_bridge_chat_model}}"
            )

target_plan.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
}

provider_seed_run() {
    load_provider_env
    require_provider_key
    export RSI_LITELLM_RESPONSE_MODE=provider_passthrough

    local script_path=""
    local task=""
    local input_url=""
    local follow_target_url=""
    local proposal_target_url=""
    local model
    local max_steps=""
    model="$(provider_seed_run_default_model)"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --script)
                script_path="$2"
                shift 2
                ;;
            --task)
                task="$2"
                shift 2
                ;;
            --input-url)
                input_url="$2"
                shift 2
                ;;
            --follow-target-url)
                follow_target_url="$2"
                shift 2
                ;;
            --proposal-target-url)
                proposal_target_url="$2"
                shift 2
                ;;
            --model)
                model="$2"
                shift 2
                ;;
            --max-steps)
                max_steps="$2"
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                echo "unsupported argument: $1" >&2
                usage >&2
                exit 1
                ;;
        esac
    done

    if [[ -z "$script_path" ]]; then
        echo "--script is required for 'seed-run'" >&2
        exit 1
    fi
    if [[ -z "$task" ]]; then
        echo "--task is required for 'seed-run'" >&2
        exit 1
    fi

    local source_plan
    source_plan="$(resolve_plan_path "$script_path")"
    if [[ ! -f "$source_plan" ]]; then
        echo "missing seed-run plan: $source_plan" >&2
        exit 1
    fi

    mkdir -p "$WORKSPACE_DIR/.seed_plans"
    local temp_plan_path
    temp_plan_path="$(mktemp "$WORKSPACE_DIR/.seed_plans/provider_seed_run.XXXXXX")"
    trap "rm -f '$temp_plan_path'" EXIT
    render_seed_run_plan "$source_plan" "$temp_plan_path" "$model"

    local cmd=(
        docker compose exec -T agent python -m untrusted.agent.seed_runner
        --task "$task"
        --planner scripted
        --script ".seed_plans/$(basename "$temp_plan_path")"
    )
    if [[ -n "$input_url" ]]; then
        cmd+=(--input-url "$input_url")
    fi
    if [[ -n "$follow_target_url" ]]; then
        cmd+=(--follow-target-url "$follow_target_url")
    fi
    if [[ -n "$proposal_target_url" ]]; then
        cmd+=(--proposal-target-url "$proposal_target_url")
    fi
    if [[ -n "$max_steps" ]]; then
        cmd+=(--max-steps "$max_steps")
    fi

    "${cmd[@]}"
}

provider_answer_packet() {
    local model="${RSI_PROVIDER_ANSWER_MODEL:-gpt-4.1-mini}"
    local task=""
    local input_url=""
    local max_steps=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --model)
                model="$2"
                shift 2
                ;;
            --task)
                task="$2"
                shift 2
                ;;
            --input-url)
                input_url="$2"
                shift 2
                ;;
            --max-steps)
                max_steps="$2"
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                echo "unsupported argument: $1" >&2
                usage >&2
                exit 1
                ;;
        esac
    done

    if [[ -z "$task" ]]; then
        echo "--task is required for 'answer-packet'" >&2
        exit 1
    fi
    if [[ -z "$input_url" ]]; then
        echo "--input-url is required for 'answer-packet'" >&2
        exit 1
    fi

    local cmd=(
        --script "$ANSWER_PACKET_PLAN"
        --task "$task"
        --input-url "$input_url"
        --model "$model"
    )
    if [[ -n "$max_steps" ]]; then
        cmd+=(--max-steps "$max_steps")
    fi

    provider_seed_run "${cmd[@]}"
}

provider_follow_answer_packet() {
    local model="${RSI_PROVIDER_ANSWER_MODEL:-gpt-4.1-mini}"
    local task=""
    local input_url=""
    local follow_target_url=""
    local max_steps=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --model)
                model="$2"
                shift 2
                ;;
            --task)
                task="$2"
                shift 2
                ;;
            --input-url)
                input_url="$2"
                shift 2
                ;;
            --follow-target-url)
                follow_target_url="$2"
                shift 2
                ;;
            --max-steps)
                max_steps="$2"
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                echo "unsupported argument: $1" >&2
                usage >&2
                exit 1
                ;;
        esac
    done

    if [[ -z "$task" ]]; then
        echo "--task is required for 'follow-answer-packet'" >&2
        exit 1
    fi
    if [[ -z "$input_url" ]]; then
        echo "--input-url is required for 'follow-answer-packet'" >&2
        exit 1
    fi
    if [[ -z "$follow_target_url" ]]; then
        echo "--follow-target-url is required for 'follow-answer-packet'" >&2
        exit 1
    fi

    local cmd=(
        --script "$FOLLOW_ANSWER_PACKET_PLAN"
        --task "$task"
        --input-url "$input_url"
        --follow-target-url "$follow_target_url"
        --model "$model"
    )
    if [[ -n "$max_steps" ]]; then
        cmd+=(--max-steps "$max_steps")
    fi

    provider_seed_run "${cmd[@]}"
}

command="${1:-help}"
case "$command" in
    init)
        shift
        if [[ $# -ne 0 ]]; then
            echo "unexpected arguments for 'init': $*" >&2
            usage >&2
            exit 1
        fi

        init_provider_env
        ;;
    up)
        shift
        if [[ $# -ne 0 ]]; then
            echo "unexpected arguments for 'up': $*" >&2
            usage >&2
            exit 1
        fi

        load_provider_env
        require_provider_key
        export RSI_LITELLM_RESPONSE_MODE=provider_passthrough
        "$ROOT_DIR/scripts/up.sh"
        ;;
    smoke)
        shift
        load_provider_env
        require_provider_key
        export RSI_LITELLM_RESPONSE_MODE=provider_passthrough

        model="${RSI_PROVIDER_SMOKE_MODEL:-gpt-4.1-mini}"
        message="Reply with the model identifier and the word ok."

        while [[ $# -gt 0 ]]; do
            case "$1" in
                --model)
                    model="$2"
                    shift 2
                    ;;
                --message)
                    message="$2"
                    shift 2
                    ;;
                -h|--help)
                    usage
                    exit 0
                    ;;
                *)
                    echo "unsupported argument: $1" >&2
                    usage >&2
                    exit 1
                    ;;
            esac
        done

        docker compose exec -T agent python -m untrusted.agent.bridge_client chat \
            --model "$model" \
            --message "$message"
        ;;
    seed-run)
        shift
        provider_seed_run "$@"
        ;;
    answer-packet)
        shift
        provider_answer_packet "$@"
        ;;
    follow-answer-packet)
        shift
        provider_follow_answer_packet "$@"
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        echo "unsupported command: $command" >&2
        usage >&2
        exit 1
        ;;
esac
