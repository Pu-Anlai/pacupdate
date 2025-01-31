import asyncio
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from calendar import timegm
from configparser import ConfigParser
from dataclasses import dataclass
from html.parser import HTMLParser
from time import time
from typing import Iterable, Iterator, Literal, NoReturn, TypedDict
from urllib.error import URLError
from urllib.request import urlopen

import aiohttp
import feedparser
import pyalpm

TERMCOLORS = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "bold": "\033[1m",
    "default": "\033[0m",
}
BUILDDIR = tempfile.mkdtemp()


async def make_aur_request(pkg: str, session: aiohttp.ClientSession) -> dict | None:
    async with session.get(
        f"https://aur.archlinux.org/rpc/v5/info?arg[]={pkg}"
    ) as resp:
        r = await resp.text()
    try:
        resp = json.loads(r)
    except json.JSONDecodeError:
        return None

    if int(resp["resultcount"]) > 1:
        raise TypeError(
            f"Queried the AUR for single package {pkg} but got multiple results. This should never happen."
        )
    elif int(resp["resultcount"]) == 0:
        return None
    else:
        return resp["results"][0]


def getenv_int(env: str) -> int | None:
    """Cast env to int if possible return None otherwise"""
    try:
        return int(os.getenv(env, ""))
    except ValueError:
        return None


class UpdateInfo(TypedDict):
    pm_updates: list[str]
    # TypedDicts are evaluated eagerly. So we need to use string literals here
    # to not create a circular dependency for the type checker.
    aur_updates: list["AURPackage"]


class FeedPrinter(HTMLParser):
    """Cleans up HTML tags and does nothing else."""

    def __init__(self):
        self.text = ""
        super().__init__()

    def handle_data(self, data: str):
        self.text += data


@dataclass
class Config:
    """Config object that holds settings set in the environment as well as temporary information."""

    mirrorlist_url: str = (
        os.getenv("PACUPDATE_MIRRORLIST_URL")
        or "https://archlinux.org/mirrorlist/?country=all&protocol=http&protocol=https&ip_version=4"
    )
    rss_feed_url: str = (
        os.getenv("PACUPDATE_RSS_FEED_URL") or "https://archlinux.org/feeds/news/"
    )
    mirrorlist_interval: int = getenv_int("PACUPDATE_MIRRORLIST_INTERVAL") or 14
    mirrorlist_path: str = "/etc/pacman.d/mirrorlist"
    curl_timeout: int = getenv_int("PACUPDATE_CURL_TIMEOUT") or 20
    git_interval: int = getenv_int("PACUPDATE_GIT_INTERVAL") or 14
    pm_db_root: str = os.getenv("PACUPDATE_PM_ROOT") or "/"
    pm_db_path: str = os.getenv("PACUPDATE_PM_DBPATH") or "/var/lib/pacman"
    pm_conf_path: str = os.getenv("PACUPDATE_PM_CONF") or "/etc/pacman.conf"

    def __init__(self):
        self.git_interval_secs = self.git_interval * 60 * 60 * 24

    @property
    def pm_conf(self) -> ConfigParser:
        if not hasattr(self, "_pm_conf"):
            try:
                self._pm_conf = ConfigParser(allow_no_value=True)
                self._pm_conf.read(self.pm_conf_path)
            except FileNotFoundError as e:
                die(str(e), exit_code=1)

        return self._pm_conf

    @property
    def pm_log_path(self) -> str:
        if not hasattr(self, "_pm_log_path"):
            try:
                self._pm_log_path = self.pm_conf["options"]["logfile"]
            except KeyError:
                self._pm_log_path = os.path.join("/", "var", "log", "pacman.log")

        return self._pm_log_path

    def get_pm_log(self) -> str:
        with open(self.pm_log_path, mode="r") as f:
            return f.read()

    @property
    def pm_handle(self) -> pyalpm.Handle:
        if not hasattr(self, "_pm_handle"):
            self.init_pm_handle()
        return self._pm_handle

    def init_pm_handle(self):
        try:
            self._pm_handle = pyalpm.Handle(self.pm_db_root, self.pm_db_path)
        except pyalpm.error as e:
            die(str(e), exit_code=1)

    @property
    def pm_local_db(self) -> pyalpm.DB:
        if not hasattr(self, "_pm_local_db"):
            self.init_pm_local_db()
        return self._pm_local_db

    def init_pm_local_db(self):
        """Initialize the pacman database held in the pm_db field."""
        try:
            self._pm_local_db = self.pm_handle.get_localdb()
        except pyalpm.error as e:
            die(str(e), exit_code=1)

    @property
    def pm_sync_dbs(self) -> pyalpm.DB:
        if not hasattr(self, "_pm_sync_dbs"):
            self.init_pm_sync_db()
        return self._pm_sync_dbs

    def init_pm_sync_db(self):
        repos = self.pm_conf.sections()
        try:
            repos.remove("options")
        except ValueError:
            pass

        try:
            for r in repos:
                self.pm_handle.register_syncdb(r, 0)
            self._pm_sync_dbs = self.pm_handle.get_syncdbs()
        except pyalpm.error as e:
            die(str(e), exit_code=1)

    @property
    def pm_sync_pkgcache_str(self) -> list[str]:
        if not hasattr(self, "_pm_sync_pkgcache"):
            self.init_pm_sync_pkgcache()
        return self._pm_sync_pkgcache

    def init_pm_sync_pkgcache(self):
        self._pm_sync_pkgcache = []
        for db in self.pm_sync_dbs:
            self._pm_sync_pkgcache.extend(pkg.name for pkg in db.pkgcache)


class AURDeps(TypedDict):
    depends: list[str]
    make_depends: list[str]
    check_depends: list[str]


class AURPackage:
    """Representation of an AUR package with helper methods attached to it."""

    def __init__(self, name: str, conf: Config):
        self.name = name
        self.local_version: str | None = self.get_local_version(conf)
        self.retrieved = False
        self.built = False
        self.has_deps = False
        self.installed = False
        self.aur_url: str | None = None
        self.archive_path: str | None = None
        self.build_dir = os.path.join(BUILDDIR, self.name)
        os.makedirs(self.build_dir)
        self.build_error = None

    @property
    def pm_deps(self) -> AURDeps:
        if not hasattr(self, "_pm_deps"):
            self._pm_deps = AURDeps(depends=[], make_depends=[], check_depends=[])
        return self._pm_deps

    @pm_deps.setter
    def pm_deps(self, value: AURDeps):
        self._pm_deps = value

    @property
    def aur_deps(self) -> AURDeps:
        if not hasattr(self, "_aur_deps"):
            self._aur_deps = AURDeps(depends=[], make_depends=[], check_depends=[])
        return self._aur_deps

    @aur_deps.setter
    def aur_deps(self, value: AURDeps):
        """Dependency between these will be in ascending order, meaning the
        packages in the list should be built from beginning to end."""
        self._aur_deps = value

    @property
    def lost_deps(self) -> AURDeps:
        if not hasattr(self, "_lost_deps"):
            self._lost_deps = AURDeps(depends=[], make_depends=[], check_depends=[])
        return self._lost_deps

    @lost_deps.setter
    def lost_deps(self, value: AURDeps):
        self._lost_deps = value

    @property
    def is_built(self) -> bool:
        """Indicate whether a package has been built or not."""
        return hasattr(self, "pkg_location")

    async def get_outdated(
        self, session: aiohttp.ClientSession, *args, **kwargs
    ) -> bool | None:
        if not hasattr(self, "_is_outdated"):
            resp = await self.get_aurweb_response(session)
            if resp is None:
                return None
            else:
                self._is_outdated = self.local_version != resp["Version"]
        return self._is_outdated

    async def get_rebuild_required(
        self, updates: UpdateInfo, conf: Config, session: aiohttp.ClientSession
    ) -> bool | None:
        """This checks whether any of the package's dependencies have been
        updated in the repos or the AUR. This would lead to it requiring to be
        rebuilt as well."""
        if not hasattr(self, "_rebuild_required"):
            if not self.has_deps:
                await self.get_deps(conf, session)

            for dep in (*self.pm_deps["depends"], *self.aur_deps):
                if dep in (
                    *updates["pm_updates"],
                    *(pkg.name for pkg in updates["aur_updates"]),
                ):
                    self._rebuild_required = True
            else:
                self._rebuild_required = False

        return self._rebuild_required

    async def get_aurweb_response(self, session: aiohttp.ClientSession) -> dict | None:
        if not hasattr(self, "_aurweb_response"):
            self._aurweb_response = await make_aur_request(self.name, session)
        return self._aurweb_response

    def get_local_version(self, conf: Config) -> str | None:
        pkg = conf.pm_local_db.get_pkg(self.name)
        if pkg is None:
            return None
        else:
            return pkg.version

    async def ensure_paths(self, session: aiohttp.ClientSession):
        if not self.archive_path is None:
            return
        resp = await self.get_aurweb_response(session)
        if resp is None:
            return
        self.url = f"https://aur.archlinux.org/{resp["URLPath"]}"
        self.archive_path = os.path.join(
            self.build_dir, os.path.basename(resp["URLPath"])
        )

    async def build(self, session: aiohttp.ClientSession):
        aurweb_response = await self.get_aurweb_response(session)
        if aurweb_response is None:
            return
        await self.ensure_paths(session)

        if self.url is None or self.archive_path is None:
            return

        if not self.retrieved:
            await self.retrieve_package(session)
        await self.makepkg_this()

    async def makepkg_this(self):
        proc = await asyncio.create_subprocess_exec(
            "makepkg",
            cwd=os.path.join(self.build_dir, self.name),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            self.build_error = stderr.decode()
            return

        proc = await asyncio.create_subprocess_exec(
            "makepkg",
            "--packagelist",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            self.build_error = stderr.decode()
            return

        self.pkg_location = stdout.decode().split("\n")
        self.built = True

    async def retrieve_package(self, session: aiohttp.ClientSession):
        """Download and extract package from URL_PATH. URLPath is specified in the Aurweb response object."""
        await self.ensure_paths(session)
        if self.url is None or self.archive_path is None:
            return
        async with session.get(self.url) as response:
            with open(self.archive_path, "wb") as f:
                async for chunk in response.content.iter_chunked(512 * 1024):
                    f.write(chunk)
            with tarfile.open(self.archive_path, "r") as f:
                f.extractall(path=self.build_dir, filter="tar")
        self.retrieved = True

    async def get_deps(self, conf: Config, session: aiohttp.ClientSession):
        if not self.has_deps:
            resp = await self.get_aurweb_response(session)
            await self._get_deps(resp, conf, session)
            self.has_deps = True

    async def _get_deps(
        self, aurweb_response: dict | None, conf: Config, session: aiohttp.ClientSession
    ):
        if aurweb_response is None:
            return
        await self._assign_deps(aurweb_response, conf, session)

    async def _assign_deps(
        self, resp: dict, conf: Config, session: aiohttp.ClientSession
    ):
        """Assign deps in DEPS to either {pm,aur,lost}_deps depending on where they can be found.
        For AUR deps, add their own dependencies as well."""
        trans = {
            "Depends": "depends",
            "MakeDepends": "make_depends",
            "CheckDepends": "check_depends",
        }
        for k, v in trans.items():
            try:
                for dep in resp[k]:
                    source = await self._get_dep_source(dep, conf, session)
                    getattr(self, source)[v].append(dep)
            except KeyError:
                continue

    async def _get_dep_source(
        self, dep: str, conf: Config, session: aiohttp.ClientSession
    ) -> Literal["pm_deps", "aur_deps", "lost_deps"]:
        # first check if dep is in the official repos
        if dep in [pkg for pkg in conf.pm_sync_pkgcache_str]:
            return "pm_deps"
        # next check if dep is available from the AUR
        req = await make_aur_request(dep, session)
        # if not: no idea where it may be
        if req is None:
            return "lost_deps"
        else:
            await self._get_deps(req, conf, session)
            return "aur_deps"

    def install(self, options: list[str] = []):
        """Attempt to install package. Additional OPTIONS will be passed to pacman."""
        if not self.built:
            fancy_echo(
                f"Package {self.name} was not built succesfully. Skipping installation...",
                prefix_color=TERMCOLORS["red"],
            )
            return
        for path in self.pkg_location:
            rc = subprocess.call(["sudo", "pacman", "-U", *options, path])
            self.installed = rc == 0


class GitPackage(AURPackage):
    """Subclass of AURPackage adapted for git packages."""

    def __init__(self, name: str, conf: Config):
        super().__init__(name, conf)
        self.update_reason = ""
        self.local_pkg = conf.pm_local_db.get_pkg(self.name)
        self.local_revision_id = self._get_local_revision_id()

    def _get_local_revision_id(self) -> str | None:
        version = self.local_pkg.version
        rev_id = version.rsplit(".")[-1].split("-")[0]
        # this is the best we can do to check if what we got is actually a commit hash
        if len(rev_id) < 7 or not rev_id.isalnum():
            return None
        else:
            return rev_id

    async def get_upstream_revision_id(
        self, session: aiohttp.ClientSession
    ) -> str | None:
        if not hasattr(self, "_us_rev_id"):
            await self.retrieve_package(session)
            if not self.retrieved:
                return None
            pkgbuild = os.path.join(self.build_dir, self.name, "PKGBUILD")
            git_url = await self._get_source_from_pkgbuild(pkgbuild)
            if git_url is None:
                return None
            git_log = await self._git_ls_remote(git_url)
            if git_log is None:
                return None
            else:
                self._us_rev_id = git_log.split("\n")[0][:7]
        return self._us_rev_id

    async def _git_ls_remote(self, url: str) -> str | None:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-remote",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        return stdout.decode()

    async def _get_source_from_pkgbuild(self, pkgbuild) -> str | None:
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            f'source {pkgbuild} && set -- $source && echo "$@"',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        git_url = [x for x in stdout.decode().split("\n") if x.startswith("git+")]
        if len(git_url) != 1:
            return None
        else:
            return git_url[0][4:]

    async def get_outdated(self, session: aiohttp.ClientSession, conf: Config) -> bool:
        """Return True if the package passes requirements for an update"""
        if not hasattr(self, "_is_outdated"):
            # first do the regular AUR check, i.e. check if an update has been submitted to the AUR
            if await super().get_outdated(session):
                self._is_outdated = True  # redundant but more clear
            # check if enough time has passed to warrant checking for an update
            elif self.local_pkg.installdate + conf.git_interval_secs > time():
                self._is_outdated = False
            # if we didn't retrieve an upstream revision id, let's just schedule an update anyway
            elif await self.get_upstream_revision_id(session) is None:
                self._is_outdated = True
                self.update_reason = f"Unable to determine latest commit of {self.name}. Scheduling an update anyway."
            else:
                self._is_outdated = (
                    await self.get_upstream_revision_id(session)
                    != self.local_revision_id
                )
        return self._is_outdated


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
            print("\n", end="")
            return True
        elif response.lower() == "n":
            return False


def headline_echo(msg: str, color: str = TERMCOLORS["green"], leading_nl: bool = True):
    prefix = "\n==>" if leading_nl else "==>"
    fancy_echo(msg, prefix=prefix, prefix_color=color)


def die(msg: str, exit_code: int = 0) -> NoReturn:
    """Print out msg and quit the program with exit code 1."""
    fancy_echo(msg, prefix_color=TERMCOLORS["red"])
    exit(exit_code)


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
        if not y_or_n(
            f"The following command failed:\n{cmd}\nWould you like to continue? (This may lead to additional errors.)"
        ):
            quit()


def retain_first_value_only(l: Iterable) -> list:
    """Take a list and remove all duplicate values beyond their *first* occurence."""
    seen = set()
    return [x for x in l if not (x in seen or seen.add(x))]


def update_mirrorlist(conf: Config):
    """Replace current mirrorlist with one fetched from archlinux.org."""

    try:
        r = urlopen(conf.mirrorlist_url, timeout=float(conf.curl_timeout))
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
        fancy_echo(
            f"There was a an error parsing the Arch RSS feed:\n{e}.",
            prefix_color=TERMCOLORS["red"],
        )
        if y_or_n("Do you want to continue?"):
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


def print_package_errors(pkgs: list[str], op: str):
    if len(pkgs) == 0:
        return
    fancy_echo(
        f"While {op}, an error occured during the processing of the following packages:\n{"\n".join(pkgs)}"
    )
    if not y_or_n("Do you want to continue?"):
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

    errors = [x.name for x in aur_pkgs if await x.get_outdated(session, conf) is None]
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
        x.name
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


def print_pacman_warnings(loglines: list[str]):
    """Take a list of lines from the pacman log file, extract all the warnings
    from them and print them to the terminal."""
    warnings = []
    for l in filter(lambda x: len(x.strip()) != 0, loglines):
        s = l.split(" ")
        if s[2].lower().startswith("warning"):
            warnings.append(" ".join(s[3:]))
    if len(warnings) == 0:
        return

    fancy_echo(
        "Pacman issued the following warnings:", prefix_color=TERMCOLORS["yellow"]
    )
    for w in warnings:
        print(f"- {w}")


def run_pacman_update(updates: UpdateInfo, conf: Config):
    """Run `pacman -Syu`."""
    log_before = conf.get_pm_log().split("\n")
    headline_echo("Installing updates from the pacman repositories...")
    if "archlinux-keyring" in updates["pm_updates"]:
        fancy_echo("Upgrading archlinux-keyring ahead of other packages...")
        call_shell_cmd("sudo pacman -Sy archlinux-keyring")
    call_shell_cmd("sudo pacman -Syu")
    print_pacman_warnings(conf.get_pm_log().split("\n")[len(log_before) :])


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


def install_pm_deps(updates: UpdateInfo, conf: Config) -> list[str]:
    """Install all dependencies in UPDATES that are available in the pacman repos
    or do nothing if there are none. Return a list of all dependencies that were found
    """
    deps: list[str] = get_all_deps_from_aurdeps("pm_deps", updates["aur_updates"])
    # get rid of all deps that are not in the repos
    deps = [d for d in deps if d in conf.pm_sync_pkgcache_str]
    if len(deps) == 0:
        return []
    fancy_echo("Installing dependencies from the pacman repos...")
    call_shell_cmd(f"sudo pacman -S --asdeps --needed {' '.join(deps)}")
    return deps


def remove_installed_dependencies(deps: list[str]):
    """Remove all packages in DEPS that are no longer required by any other package."""
    call_shell_cmd(f"sudo pacman -Ru {" ".join(deps)}")


async def install_aur_deps(
    updates: UpdateInfo, conf: Config, session: aiohttp.ClientSession
) -> list[str]:
    """Build and install all AUR dependencies in UPDATES or do nothing if there
    are none. Return a list of all the dependecies that were collected."""
    deps: list[str] = get_all_deps_from_aurdeps("aur_deps", updates["aur_updates"])
    if len(deps) == 0:
        return []

    fancy_echo("Building dependencies from the AUR...")
    deps = retain_first_value_only(deps)
    dep_pkgs: list[AURPackage] = [AURPackage(pkg, conf) for pkg in deps]
    # build dependencies sequentially in case they depend on each other, there
    # might be dependencies across packages as well as we removed duplicates
    # before
    for pkg in dep_pkgs:
        await pkg.build(session)

    fancy_echo("Installing dependencies from the AUR...")
    for pkg in dep_pkgs:
        pkg.install(options=["--asdeps"])

    return deps


async def install_aur_updates(
    updates: UpdateInfo, conf: Config, session: aiohttp.ClientSession
):
    headline_echo("Installing AUR updates (including git-based packages)...")
    fancy_echo("Building dependency lists for all packages...")
    async with asyncio.TaskGroup() as tg:
        for pkg in updates["aur_updates"]:
            tg.create_task(pkg.get_deps(conf, session))

    deps = []
    try:
        deps += install_pm_deps(updates, conf)
        deps += await install_aur_deps(updates, conf, session)

        fancy_echo("Building AUR packages...")
        async with asyncio.TaskGroup() as tg:
            for pkg in updates["aur_updates"]:
                tg.create_task(pkg.build(session))
        failed: list[AURPackage] = [pkg for pkg in deps if not pkg.built]
        for pkg in failed:
            fancy_echo(
                f'Trying to build package "{pkg.name}" produced the following error:'
            )
            print(pkg.build_error)
        if not y_or_n("Do you want to continue?"):
            exit()

        fancy_echo("Installing AUR packages...")
        for pkg in updates["aur_updates"]:
            pkg.install()
    finally:
        remove_installed_dependencies(deps)


async def run():
    """Main entry point for program."""
    check_for_programs()
    conf = Config()
    updates = UpdateInfo(pm_updates=[], aur_updates=[])
    check_mirrorlist(conf)
    check_mailinglist(conf)
    async with aiohttp.ClientSession() as session:
        await gather_update_info(updates, conf, session)
        upd_count = len([*updates["pm_updates"], *updates["aur_updates"]])
        if not y_or_n(f"Continue with {upd_count} update{'s'[:upd_count^1]}?"):
            quit()
        if len(updates["pm_updates"]) > 0:
            run_pacman_update(updates, conf)
        if len([*updates["aur_updates"]]):
            await install_aur_updates(updates, conf, session)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        die("Aborted by user.", exit_code=1)
