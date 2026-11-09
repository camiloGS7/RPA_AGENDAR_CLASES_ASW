"""Command line entry point. Commands (check, observe, run, report) arrive in phase 6."""

from asw_bot import __version__


def main() -> int:
    """Print the version and exit; placeholder until the real commands exist."""
    print(f"asw-bot {__version__}")
    return 0
