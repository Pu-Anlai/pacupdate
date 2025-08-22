import json
import os
from tempfile import mkdtemp
from typing import NoReturn

import aiohttp

TERMCOLORS = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "bold": "\033[1m",
    "default": "\033[0m",
}
BUILDDIR = mkdtemp()


def getenv_int(env: str) -> int | None:
    """Cast env to int if possible return None otherwise"""
    try:
        return int(os.getenv(env, ""))
    except ValueError:
        return None


async def make_aur_request(pkg: str, session: aiohttp.ClientSession) -> dict | None:
    try:
        async with session.get(
            f"https://aur.archlinux.org/rpc/v5/info?arg[]={pkg}"
        ) as resp:
            r = await resp.text()
            resp = json.loads(r)
    except (json.JSONDecodeError, aiohttp.ClientError):
        return None

    if int(resp["resultcount"]) > 1:
        raise TypeError(
            f"Queried the AUR for single package {pkg} but got multiple results. This should never happen."
        )
    elif int(resp["resultcount"]) == 0:
        return None
    else:
        return resp["results"][0]


def fancy_echo(
    msg: str,
    prefix: str = "::",
    prefix_color: str = TERMCOLORS["blue"],
    msg_color: str = TERMCOLORS["bold"],
):
    if prefix:
        print(f"{prefix_color}{prefix}{TERMCOLORS['default']} ", end="")
    print(f"{msg_color}{msg}{TERMCOLORS['default']}")


def y_or_n(prompt: str) -> bool:
    """Prompt the user with the msg PROMPT and return a bool representing the answer."""
    while True:
        response = input(f"{prompt} [Y/n] ")
        if response.lower() == "y" or response == "":
            return True
        elif response.lower() == "n":
            return False


def error_y_or_n(error: str, prompt: str = "Do you want to continue?") -> bool:
    """Prompt the user with the msg PROMPT and return a bool representing the answer."""
    fancy_echo(error, prefix_color=TERMCOLORS["red"])
    return y_or_n(prompt)


def headline_echo(msg: str, color: str = TERMCOLORS["green"], leading_nl: bool = True):
    prefix = "\n==>" if leading_nl else "==>"
    fancy_echo(msg, prefix=prefix, prefix_color=color)


def die(msg: str, exit_code: int = 0) -> NoReturn:
    """Print out msg and quit the program with exit code 1."""
    print()  # newline
    fancy_echo(msg, prefix_color=TERMCOLORS["red"])
    exit(exit_code)
