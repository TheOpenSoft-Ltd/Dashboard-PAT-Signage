import rich_click as click

from pat_sig.cli.commands.init import init
from pat_sig.cli.commands.run import run as run_cmd
from pat_sig.cli.commands.install import install
from pat_sig.cli.commands.list import list_tasks
from pat_sig.cli.commands.service import (
    start,
    stop,
    restart,
    status,
    logs,
    uninstall,
)

click.rich_click.USE_RICH_MARKUP = True
click.rich_click.ERRORS_SUGGESTION = (
    "Try 'pat-sig --help' to view available options."
)


@click.group(help="PAT Signage (DSM) - Digital Signage Display System")
@click.version_option(package_name="pat-sig")
def cli():
    """PAT Signage (DSM) CLI."""
    pass


cli.add_command(init)
cli.add_command(run_cmd)
cli.add_command(install)
cli.add_command(list_tasks)
cli.add_command(start)
cli.add_command(stop)
cli.add_command(restart)
cli.add_command(status)
cli.add_command(logs)
cli.add_command(uninstall)


def run() -> None:
    """Entry point referenced by pyproject (pat_sig.__main__:run)."""
    cli()


if __name__ == "__main__":
    run()
