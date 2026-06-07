from __future__ import annotations  # PEP 604 (X | None) on Python 3.9

import rich_click as click
from rich.console import Console
from rich.table import Table

from pat_sig.config import setup_django

console = Console()

# status -> rich colour for the Status column.
_STATUS_STYLE = {
    "pending": "yellow",
    "downloaded": "cyan",
    "playing": "green",
    "skiping": "dim",
    "completed": "blue",
    "failed": "red",
}


def _fmt_schedule(task) -> str:
    """Render the start -> end window compactly, '-' when unset."""
    start = " ".join(
        str(v) for v in (task.date_started_at, task.time_started_at) if v
    )
    end = " ".join(str(v) for v in (task.date_end_at, task.time_end_at) if v)
    if not start and not end:
        return "-"
    return f"{start or '-'} → {end or '-'}"


@click.command(name="list")
@click.option("--status", "status_filter", help="Filter by task status.")
@click.option("--type", "type_filter", help="Filter by task type.")
@click.option(
    "-n", "--limit", type=int, default=50, help="Max rows to show (default 50)."
)
def list_tasks(status_filter: str | None, type_filter: str | None, limit: int):
    """List DSM tasks from the local database as a table."""
    setup_django()
    from home.models import DSMTask

    qs = DSMTask.objects.all()
    if status_filter:
        qs = qs.filter(status=status_filter)
    if type_filter:
        qs = qs.filter(task_type=type_filter)

    total = qs.count()
    tasks = list(qs[:limit])

    if not tasks:
        console.print("[yellow]No tasks found.[/yellow]")
        return

    table = Table(
        title=f"DSM Tasks ({len(tasks)} of {total})",
        header_style="bold cyan",
        show_lines=False,
    )
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Media")
    table.add_column("Status", no_wrap=True)
    table.add_column("Schedule", no_wrap=True)
    table.add_column("Created", style="dim", no_wrap=True)

    for task in tasks:
        style = _STATUS_STYLE.get(task.status, "white")
        created = (
            task.created_at.strftime("%Y-%m-%d %H:%M") if task.created_at else "-"
        )
        table.add_row(
            task.dsm_task_id[:8],
            task.name,
            task.get_task_type_display(),
            task.get_media_type_display(),
            f"[{style}]{task.status}[/{style}]",
            _fmt_schedule(task),
            created,
        )

    console.print(table)
