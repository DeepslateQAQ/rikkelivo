"""GitHub CLI-style console output on top of Rich."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterable

from rich.console import Console
from rich.table import Table

console = Console(highlight=False)
_error_console = Console(highlight=False, stderr=True)

_BANNER = """\
┏━┓╻╻┏ ╻┏ ┏━╸╻  ╻╻ ╻┏━┓
┣┳┛┃┣┻┓┣┻┓┣╸ ┃  ┃┃┏┛┃ ┃
╹┗╸╹╹ ╹╹ ╹┗━╸┗━╸╹┗┛ ┗━┛"""


def banner() -> None:
    console.print(_BANNER, style="bold cyan")


def comment(msg: str) -> None:
    """Dim '#'-prefixed annotation line, like `gh` comments."""
    console.print(f"[dim]# {msg}[/dim]")


def ok(msg: str) -> None:
    console.print(f"[bold green]✓[/bold green] {msg}")


def warn(msg: str) -> None:
    console.print(f"[bold yellow]![/bold yellow] {msg}")


def err(msg: str) -> None:
    _error_console.print(f"[bold red]✗[/bold red] {msg}")


def info(msg: str) -> None:
    console.print(f"[bold cyan]>[/bold cyan] {msg}")


@contextmanager
def spinner(msg: str):
    with console.status(f"[bold]{msg}[/bold]", spinner="dots"):
        yield


def summary_box(title: str, rows: Iterable[tuple[str, str]]) -> None:
    """Compact bordered summary, like `gh`'s table output."""
    table = Table(
        title=title,
        title_justify="left",
        box=None,
        show_header=False,
        pad_edge=False,
        border_style="dim",
        title_style="bold",
    )
    table.add_column("key", style="dim", no_wrap=True)
    table.add_column("value", style="bold")
    for key, value in rows:
        table.add_row(key, value)
    console.print(table)


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
