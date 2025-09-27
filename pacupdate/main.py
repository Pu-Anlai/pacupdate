import asyncio
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
from calendar import timegm
from html.parser import HTMLParser
from time import time
from types import FrameType
from typing import Iterator, Literal, NoReturn, TypedDict
from urllib.error import URLError
from urllib.request import urlopen

import aiohttp
import feedparser
import pyalpm
from packaging.version import Version

from .shared import (
    BUILDDIR,
    TERMCOLORS,
    die,
    error_y_or_n,
    fancy_echo,
    headline_echo,
    y_or_n,
)
from .structs import AURPackage, Config, GitPackage, UpdateInfo


def exit_signal_handler(signal: int, frame: FrameType | None) -> NoReturn:
    die("Aborted by user.", exit_code=1)


signal.signal(signal.SIGINT, exit_signal_handler)


class MixedDeps(TypedDict):
    pm_deps: list[str]
    aur_deps: list[AURPackage]


class FeedPrinter(HTMLParser):
    """Cleans up HTML tags and does nothing else."""

    def __init__(self):
        self.text = ""
        super().__init__()

    def handle_data(self, data: str):
        self.text += data


def test_for_root() -> bool:
    return os.geteuid() == 0


def check_for_programs():
    """Checks whether sudo executable is present on the system."""
    for p in ["sudo", "git"]:
        if not shutil.which(p):
            die(f"{p} not found in path. Exiting...", exit_code=1)


def print_package_info(pkgs: list, source: str = ""):
    """Print info on number of available packages in PKGS."""
    source = source + " " if source else source
    updates = len(pkgs)
    if updates > 0:
        fancy_echo(f"{updates} {source}package update{'s'[:updates^1]} available.")
    else:
        fancy_echo("All packages up-to-date.")


def call_shell_cmd(cmd: str, stdout=None):
    """Run shell command CMD."""
    if subprocess.call(shlex.split(cmd), stdout=stdout) > 0:
        if not error_y_or_n(
            f"The following command failed:\n{cmd}\n",
            prompt="Would you like to continue? (This may lead to additional errors.)",
        ):
            quit()


def update_mirrorlist(conf: Config):
    """Replace current mirrorlist with one fetched from archlinux.org."""

    try:
        r = urlopen(conf.mirrorlist_url)
        mlist_raw = r.read().decode("utf-8").split("\n")
        r.close()
    except (ValueError, URLError):
        die(f"Error downloading from {conf.mirrorlist_url}", exit_code=1)

    # backup mirrorlist
    call_shell_cmd(
        f"sudo cp {conf.mirrorlist_path} {conf.mirrorlist_path}.backup",
        stdout=subprocess.DEVNULL,
    )

    tmpfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
    with tmpfile as f:
        # add newline to all lines
        lines = map(lambda x: f"{x}\n", mlist_raw)
        # remove first hashtag character from all lines that have one
        lines = map(lambda x: x[1:] if x.startswith("#") else x, lines)
        f.writelines(lines)

    try:
        call_shell_cmd(
            f"sudo cp {tmpfile.name} {conf.mirrorlist_path}", stdout=subprocess.DEVNULL
        )
    finally:
        os.unlink(tmpfile.name)


def mirrorlist_uptodate(conf: Config) -> bool:
    """Returns False if mirrorlist is outdated."""
    mlist_mtime = os.path.getmtime(os.path.join("/", "etc", "pacman.d", "mirrorlist"))
    days_since = (time() - mlist_mtime) / 60 / 60 / 24
    return days_since <= conf.mirrorlist_interval


def get_pmdb_update_time(db: pyalpm.DB) -> int:
    """Returns the epoch time of the most recently installed pacman package."""
    pkg = sorted(db.pkgcache, key=lambda x: x.installdate, reverse=True)[0]
    return pkg.installdate


def get_mailing_list_entries(conf: Config) -> list[feedparser.util.FeedParserDict]:
    """Return a list of any mailing list entries created after the most recent update."""
    rss = feedparser.parse(conf.rss_feed_url)
    db_update_time = get_pmdb_update_time(conf.pm_local_db)
    try:
        # retrieve all entries that were published after the last pacman update
        entries = filter(
            lambda x: timegm(x.published_parsed) > db_update_time, rss.entries
        )
        return list(entries)
    except Exception as e:
        if error_y_or_n(f"There was a an error parsing the Arch RSS feed:\n{e}."):
            return list()
        else:
            exit(1)


def ensure_str_from_feed(feed) -> str:
    if not isinstance(feed, str):
        raise TypeError(f"Expected string from RSS feed but got:\n{feed}")
    else:
        return feed


def print_feed_entries(entries: list[feedparser.util.FeedParserDict]):
    """Print out all entries in ENTRIES in a clean way."""
    f = FeedPrinter()
    for e in entries:
        f.feed(ensure_str_from_feed(e.title))
        fancy_echo(f.text)
        f.text = ""
        f.feed(ensure_str_from_feed(e.summary))
        print(f.text + "\n")
        f.text = ""
        if not y_or_n("Continue?"):
            exit()


def get_stdout_lines(cmd: str) -> list[str]:
    """Return a list of the lines printed to stdout by CMD."""
    sp = subprocess.run(shlex.split(cmd), capture_output=True)
    if sp.stdout is None:
        die(f'Error running command "{cmd}:\n{sp.stderr}".', exit_code=1)
    elif len(sp.stdout) == 0:
        return list()
    else:
        pkgs = map(str, sp.stdout.split(b"\n"))
        return list(pkgs)


def check_mirrorlist(conf: Config):
    """Update pacman mirrorlist if it is older than the configured threshold."""
    headline_echo(
        "Checking if mirrorlist is outdated...",
        color=TERMCOLORS["green"],
        leading_nl=False,
    )
    if not mirrorlist_uptodate(conf):
        fancy_echo("Mirrorlist out-of-date, fetching new one...")
        update_mirrorlist(conf)
    else:
        fancy_echo("Mirrorlist up-to-date.")


def check_mailinglist(conf: Config):
    """Check the Arch mailing list for any updates."""
    headline_echo("Checking for news in the Arch mailinglist since the last update...")

    new_entries = get_mailing_list_entries(conf)
    n_e_len = len(new_entries)

    fancy_echo(f"{n_e_len} news item{'s'[:n_e_len^1]}")
    if n_e_len == 0:
        return
    else:
        print_feed_entries(new_entries)


def get_foreign_packages(conf: Config) -> Iterator[pyalpm.Package]:
    """Get a list of all foreign packages. Equivalent of running `pacman -Qm`"""
    for p in conf.pm_local_db.pkgcache:
        for db in conf.pm_sync_dbs:
            if db.get_pkg(p.name) is not None:
                break
        else:
            yield p


def print_package_errors(pkgs: list[AURPackage], op: str):
    if len(pkgs) == 0:
        return

    pkg_errors = []
    for p in pkgs:
        pkg_errors.append(
            "{pkg}{msg}".format(
                pkg=p.name, msg=f"\n--> {p.error_msg}" if p.error_msg else ""
            )
        )
    if not error_y_or_n(
        f"While {op}, an error occured during the processing of the following packages:\n{"\n".join(pkg_errors)}"
    ):
        quit()


async def collect_aur_updates(
    updates: UpdateInfo, conf: Config, session: aiohttp.ClientSession
):
    aur_pkgs = []
    for pkg in get_foreign_packages(conf):
        if pkg.name.endswith("-git"):
            aur_pkgs.append(GitPackage(pkg.name, conf))
        else:
            aur_pkgs.append(AURPackage(pkg.name, conf))

    async with asyncio.TaskGroup() as tg:
        for pkg in aur_pkgs:
            tg.create_task(pkg.get_outdated(session, conf))

    errors = [x for x in aur_pkgs if await x.get_outdated(session, conf) is None]
    print_package_errors(errors, "checking for updates")

    updates["aur_updates"] = [
        pkg for pkg in aur_pkgs if await pkg.get_outdated(session, conf)
    ]

    # check if any remaining packages require a rebuilt
    aur_pkgs = list(set(aur_pkgs) - set(updates["aur_updates"]) - set(errors))
    async with asyncio.TaskGroup() as tg:
        for pkg in aur_pkgs:
            tg.create_task(pkg.get_rebuild_required(updates, conf, session))

    errors = [
        x
        for x in aur_pkgs
        if await x.get_rebuild_required(updates, conf, session) is None
    ]
    print_package_errors(errors, "checking if a rebuild is required")

    updates["aur_updates"].extend(
        [
            pkg
            for pkg in aur_pkgs
            if await pkg.get_rebuild_required(updates, conf, session)
        ]
    )


async def gather_update_info(
    updates: UpdateInfo, conf: Config, session: aiohttp.ClientSession
):
    """Checks for updates from available sources."""
    headline_echo("Checking for updates in the pacman repos...")
    call_shell_cmd("sudo pacman -Sy", stdout=subprocess.DEVNULL)
    updates["pm_updates"] = get_stdout_lines("pacman -Quq")
    print_package_info(updates["pm_updates"])

    headline_echo("Checking for updates in the AUR...")
    await collect_aur_updates(updates, conf, session)
    print_package_info(updates["aur_updates"], "AUR")


def run_pacman_update(updates: UpdateInfo):
    """Run `pacman -Syu`."""
    headline_echo("Installing updates from the pacman repositories...")
    if "archlinux-keyring" in updates["pm_updates"]:
        fancy_echo("Upgrading archlinux-keyring ahead of other packages...")
        call_shell_cmd("sudo pacman -Sy archlinux-keyring")
    call_shell_cmd("sudo pacman -Syu")


def get_all_deps_from_aurdeps(
    deptype: Literal["pm_deps", "aur_deps", "lost_deps"], pkgs: list[AURPackage]
) -> list[str]:
    """Return a list of all dependencies of type DEPTYPE found in the packages
    in PKGS."""
    deps = []
    for aurdep in (getattr(p, deptype) for p in pkgs):
        deps.extend(aurdep["depends"])
        deps.extend(aurdep["make_depends"])
        deps.extend(aurdep["check_depends"])
    return deps


def is_provided(dep: str, conf: Config) -> bool:
    """Return True if DEP is provided by any package installed on the system."""
    dep_split = dep.split("=")
    dep_ver = "0"
    if len(dep_split) > 1:
        dep = dep_split[0]
        dep_ver = dep_split[1]

    for prov in (name for pkg in conf.pm_local_db.pkgcache for name in pkg.provides):
        prov_split = prov.split("=")
        prov_ver = f"1{dep_ver}"
        if len(prov_split) > 1:
            prov = prov_split[0]
            prov_ver = prov_split[1]
        if not prov == dep:
            continue
        else:
            return Version(prov_ver) >= Version(dep_ver)
    else:
        return False


def install_pm_deps(updates: UpdateInfo, conf: Config) -> list[str]:
    """Install all dependencies in UPDATES that are available in the pacman repos
    or do nothing if there are none. Return a list of all dependencies that were found
    """
    deps: list[str] = get_all_deps_from_aurdeps("pm_deps", updates["aur_updates"])
    # get rid of all deps that are not in the repos
    deps = [dep for dep in deps if dep in conf.pm_sync_pkgcache_str]
    deps = clean_up_deps(deps, conf)

    if len(deps) == 0:
        return []
    fancy_echo("Installing dependencies from the pacman repos...")
    call_shell_cmd(f"sudo pacman -S --asdeps --needed {' '.join(deps)}")
    return deps


def remove_installed_dependencies(deps: list[str]):
    """Remove all packages in DEPS that are no longer required by any other package."""
    if len(deps) > 0:
        fancy_echo("Removing build dependencies that are no longer needed...")
        call_shell_cmd(f"sudo pacman -Rus {" ".join(deps)}")


def clean_up_deps(deps: list[str], conf: Config) -> list[str]:
    """Remove dep packages from DEPS that do not need to be build/updated."""
    clean_deps: list[str] = []
    for d in deps:
        # skip all deps that are already installed on the system
        if d in (pkg.name for pkg in conf.pm_local_db.pkgcache):
            continue
        # then skip all deps that are provided by other installed packages
        elif is_provided(d, conf):
            continue
        else:
            clean_deps.append(d)

    return clean_deps


async def install_aur_deps(
    updates: UpdateInfo, conf: Config, session: aiohttp.ClientSession
) -> list[AURPackage]:
    """Build and install all AUR dependencies in UPDATES or do nothing if there
    are none. Return a list of all the dependecies that were collected."""
    deps: list[str] = get_all_deps_from_aurdeps("aur_deps", updates["aur_updates"])
    deps = clean_up_deps(deps, conf)
    if len(deps) == 0:
        return []

    fancy_echo("Building dependencies from the AUR...")
    dep_pkgs: list[AURPackage] = [AURPackage(pkg, conf) for pkg in deps]
    # build dependencies sequentially in case they depend on each other, there
    # might be dependencies across packages as well as we removed duplicates
    # before
    for pkg in dep_pkgs:
        await pkg.build(session)

    fancy_echo("Installing dependencies from the AUR...")
    for pkg in dep_pkgs:
        pkg.install(options=["--asdeps"])

    return dep_pkgs


async def install_aur_updates(
    updates: UpdateInfo, conf: Config, session: aiohttp.ClientSession
):
    headline_echo("Installing AUR updates (including git-based packages)...")
    fancy_echo("Building dependency lists for all packages...")
    async with asyncio.TaskGroup() as tg:
        for pkg in updates["aur_updates"]:
            tg.create_task(pkg.get_deps(conf, session))

    deps: MixedDeps = MixedDeps(pm_deps=[], aur_deps=[])
    try:
        deps["pm_deps"] += install_pm_deps(updates, conf)
        deps["aur_deps"] += await install_aur_deps(updates, conf, session)

        fancy_echo("Building AUR packages...")
        async with asyncio.TaskGroup() as tg:
            for pkg in updates["aur_updates"]:
                tg.create_task(pkg.build(session))
        failed_deps: list[str] = [pkg.name for pkg in deps["aur_deps"] if not pkg.built]
        if len(failed_deps) > 0:
            if not error_y_or_n(
                f"The following dependencies were not built successfully: {", ".join(failed_deps)}.",
                prompt="Do you want to continue? (This will likely lead to more errors.)",
            ):
                quit()

        fancy_echo("Installing AUR packages...")
        for pkg in updates["aur_updates"]:
            pkg.install()
    finally:
        str_deps: list[str] = [
            *deps["pm_deps"],
            *[pkg.name for pkg in deps["aur_deps"]],
        ]
        remove_installed_dependencies(str_deps)
        shutil.rmtree(BUILDDIR)


def get_log_diff_warnings(log1: list[str], log2: list[str]) -> list[str]:
    diff = log2[len(log1) :]
    warnings = []
    regex = re.compile(r"^\S+\s\[ALPM\]\swarning:\s(.+)$")
    for m in map(regex.fullmatch, diff):
        if m:
            warnings.append(m.group(1))
    return warnings


def show_pacman_warnings(conf: Config):
    """
    Compare the current pacman log with the one cached in CONF and display any new warnings.
    """
    if not (old_log := conf._pm_log):
        return  # no error message needed as it was given earlier
    if not (new_log := conf.pm_log):
        if not error_y_or_n(f"Unable to access pacman log at {conf.pm_log_path}."):
            quit()
        else:
            return

    if warnings := get_log_diff_warnings(old_log, new_log):
        fancy_echo(
            f"Pacman issued the following warnings:\n{"\n".join(warnings)}",
            prefix_color=TERMCOLORS["yellow"],
        )
        if not y_or_n("Continue?"):
            quit()


async def run():
    """Main entry point for program."""
    check_for_programs()
    conf = Config()
    updates = UpdateInfo(pm_updates=[], aur_updates=[])
    check_mirrorlist(conf)
    check_mailinglist(conf)

    if not conf.pm_log:  # initiate _pm_log field
        if not error_y_or_n(
            f"Unable to access pacman log at {conf.pm_log_path}.",
            prompt="Would you like to continue? (Warnings from the pacman log will not be collected.)",
        ):
            quit()

    async with aiohttp.ClientSession() as session:
        await gather_update_info(updates, conf, session)
        upd_count = len([*updates["pm_updates"], *updates["aur_updates"]])
        if upd_count == 0:
            fancy_echo("Nothing to do.")
            quit()
        if not y_or_n(f"Continue with {upd_count} update{'s'[:upd_count^1]}?"):
            quit()
        if len(updates["pm_updates"]) > 0:
            run_pacman_update(updates)
            show_pacman_warnings(conf)
        if len([*updates["aur_updates"]]):
            await install_aur_updates(updates, conf, session)


def start():
    if test_for_root():
        fancy_echo("Runing as root.", prefix_color=TERMCOLORS["red"])
        print("This script should only be run by a regular user.")
        exit(1)
    asyncio.run(run())
