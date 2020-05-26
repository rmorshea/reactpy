import sys
import argparse
from functools import wraps
from typing import TypeVar, Callable, Any

from idom.client.manage import install, delete_web_modules


_Func = TypeVar("_Func", bound=Callable[..., Any])


def _exit_gracefully(function: _Func) -> _Func:
    @wraps(function)
    def wrapper(*args, print=print, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exit as error:
            print(f"ERROR: {error}")
            sys.exit(1)

    return wrapper


class Exit(Exception):
    """Raise to exit console gracefully in error"""


@_exit_gracefully
def main(*args: str):
    cli = argparse.ArgumentParser()
    cli.add_argument("command", choices=["install", "uninstall"])
    cli.add_argument(
        "dependencies", nargs="*", type=str,
    )
    cli.add_argument(
        "--exports", nargs="*", type=str, default=[],
    )
    cli.add_argument("--force", action="store_true")

    parsed = cli.parse_args(args or sys.argv[1:])

    if parsed.command == "uninstall":
        if parsed.exports:
            raise Exit("uninstall does not support the '--exports' option")
        if parsed.force:
            raise Exit("uninstall does not support the '--force' option")

    if parsed.command == "install":
        install(parsed.dependencies, parsed.exports, parsed.force)
    else:
        delete_web_modules(parsed.dependencies)


if __name__ == "__main__":  # pragma: no cover
    main()