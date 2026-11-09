"""Print the project tree with rich, skipping caches and generated folders."""

from pathlib import Path

from rich.console import Console
from rich.tree import Tree

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    "reports",
    "data",
}
SKIP_FILES = {".coverage", ".env"}


def add_children(node: Tree, directory: Path) -> None:
    """Add directories first, then files, both in alphabetical order."""
    entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    for entry in entries:
        if entry.is_dir():
            if entry.name in SKIP_DIRS or entry.name.endswith(".egg-info"):
                continue
            add_children(node.add(f"[bold cyan]{entry.name}/[/]"), entry)
        elif entry.name not in SKIP_FILES:
            node.add(entry.name)


def main() -> None:
    tree = Tree("[bold]asw-bot/[/]")
    add_children(tree, ROOT)
    Console().print(tree)


if __name__ == "__main__":
    main()
