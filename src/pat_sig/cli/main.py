import os
import subprocess
from importlib.metadata import version
from pathlib import Path

import rich_click as click

# Cofiguration of Textual Framwork
click.rich_click.USE_RICH_MARKUP = True
click.rich_click.COLOR_SYSTEM = "truecolor"

click.rich_click.STYLE_OPTIONS_TABLE_LEADING = 1
click.rich_click.STYLE_OPTIONS_TABLE_BOX = "SIMPLE"

click.rich_click.SHOW_METAVARS_COLUMN = True
click.rich_click.ERRORS_SUGGESTION = "Try 'pat-sig --help' to view available options."


def _version_option() -> str:
    pat_smart_version = version("pat-sig")
    return f"Pattaya Smart, version {pat_smart_version}"


def _load_config():
    return None


@click.group(
    invoke_without_command=True,
    help="PAT Smart Digital Signage",
)
@click.version_option(
    package_name="pat-sig",
    message=_version_option(),
)
@click.pass_context
def cli(ctx: click.Context):
    """
    Run the CLI application.
    """
    if ctx.invoked_subcommand is None:
        config = _load_config()
        if config is None:
            click.echo(
                "Error: Configuration error. Run 'pat-sig init' to create a default .env file."
            )
            return


def run() -> None:
    cli()
