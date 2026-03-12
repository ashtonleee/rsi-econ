from dataclasses import dataclass
from pathlib import Path


def _relative_string(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    value = rel.as_posix()
    return value if value else "."


@dataclass
class WorkspaceTools:
    workspace_dir: Path

    def __post_init__(self):
        self.workspace_dir = self.workspace_dir.resolve()
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

    def resolve_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.workspace_dir / candidate).resolve()
        if not resolved.is_relative_to(self.workspace_dir):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved

    def list_tree(self, path: str = ".", *, max_depth: int = 4) -> list[dict]:
        root = self.resolve_path(path)
        entries = []
        for entry in sorted(root.rglob("*")):
            relative = entry.relative_to(root)
            depth = len(relative.parts)
            if depth > max_depth:
                continue
            entries.append(
                {
                    "path": _relative_string(entry, self.workspace_dir),
                    "kind": "dir" if entry.is_dir() else "file",
                }
            )
        return entries

    def list_files(self, path: str = ".") -> list[str]:
        root = self.resolve_path(path)
        return [
            _relative_string(entry, self.workspace_dir)
            for entry in sorted(root.rglob("*"))
            if entry.is_file()
        ]

    def read_file(self, path: str) -> str:
        resolved = self.resolve_path(path)
        return resolved.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> dict:
        resolved = self.resolve_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return {
            "path": _relative_string(resolved, self.workspace_dir),
            "bytes_written": len(content.encode("utf-8")),
        }
