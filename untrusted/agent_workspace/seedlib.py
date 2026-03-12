def render_seed_status(task_name: str) -> str:
    task_name = task_name.strip()
    assert task_name
    return f"seed agent ready: {task_name}"
