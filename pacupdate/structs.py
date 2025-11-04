import asyncio
import os
import subprocess
import tarfile
from configparser import ConfigParser
from dataclasses import dataclass
from time import time
from typing import Literal, TypedDict

import aiohttp
import pyalpm

from .shared import (
    BUILDDIR,
    TERMCOLORS,
    die,
    error_y_or_n,
    fancy_echo,
    getenv_int,
    make_aur_request,
)


class UpdateInfo(TypedDict):
    pm_updates: list[str]
    # TypedDicts are evaluated eagerly. So we need to use string literals here
    # to not create a circular dependency for the type checker.
    aur_updates: list["AURPackage"]


class AURDeps(TypedDict):
    depends: list[str]
    make_depends: list[str]
    check_depends: list[str]


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

    @property
    def pm_log(self) -> list[str] | None:
        if not hasattr(self, "_pm_log"):
            try:
                with open(self.pm_log_path, mode="r") as f:
                    self._pm_log = f.readlines()
            except OSError:
                self._pm_log = None
        return self._pm_log

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
            self.init_pm_sync_pkgcache_str()
        return self._pm_sync_pkgcache_str

    def init_pm_sync_pkgcache_str(self):
        self._pm_sync_pkgcache_str = []
        for db in self.pm_sync_dbs:
            self._pm_sync_pkgcache_str.extend(pkg.name for pkg in db.pkgcache)


class AURPackage:
    """Representation of an AUR package with helper methods attached to it."""

    def __init__(self, name: str, conf: Config):
        self.name = name
        self.local_version: str | None = self.get_local_version(conf)
        self.retrieved = False
        self.built = False
        self.has_deps = False
        self.installed = False
        self.url: str | None = None
        self.archive_path: str | None = None
        self.build_dir = os.path.join(BUILDDIR, self.name)
        self.build_error = None
        self.error_msg = ""

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
                self.error_msg = "Package not found in AUR."
                return None
            else:
                self._is_outdated = (
                    pyalpm.vercmp(resp["Version"], self.local_version) > 0
                )
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
            self.error_msg = "Package not found in local database."
            return None
        else:
            return pkg.version

    async def ensure_paths(self, session: aiohttp.ClientSession):
        if not self.archive_path is None:
            return
        os.makedirs(self.build_dir, exist_ok=True)
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

        await self.retrieve_package(session)
        if not self.retrieved:
            self.error_msg = "Unable to download package from AUR."
            return

        await self.makepkg_this()

    async def makepkg_this(self):
        fancy_echo(f"Running makepkg on {self.name}...")
        proc = await asyncio.create_subprocess_exec(
            "makepkg",
            cwd=os.path.join(self.build_dir, self.name),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            self.build_error = stderr.decode()
        fancy_echo(f"Finished building {self.name}.")

        proc = await asyncio.create_subprocess_exec(
            "makepkg",
            "--packagelist",
            cwd=os.path.join(self.build_dir, self.name),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            self.build_error = stderr.decode()

        self.pkg_location = list(filter(os.path.exists, stdout.decode().split("\n")))
        if len(self.pkg_location) > 0:
            self.built = True

    async def retrieve_package(self, session: aiohttp.ClientSession):
        """Download and extract package from URL_PATH. URLPath is specified in the Aurweb response object."""
        if self.retrieved:
            return
        await self.ensure_paths(session)
        if self.url is None or self.archive_path is None:
            return
        async with session.get(self.url) as response:
            with open(self.archive_path, "wb") as f:
                async for chunk in response.content.iter_chunked(512 * 1024):
                    f.write(chunk)
            if not self.tar_extract():
                return
        self.retrieved = True

    def tar_extract(self) -> bool:
        """Extract tar file at self.archive_path. Return success status"""
        try:
            with tarfile.open(self.archive_path, "r") as f:
                f.extractall(path=self.build_dir, filter="tar")
                return True
        except tarfile.ReadError:
            return False

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
                    target = getattr(self, source)[v]
                    if dep not in target:
                        target.append(dep)
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
            if self.build_error is not None:
                self.build_error = self.build_error.strip()
            error_y_or_n(
                f"Package {self.name} cannot be installed because of the following error during its build process:\n{TERMCOLORS["default"]}{self.build_error}",
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
            self.error_msg = "Unable to determine local revision id."
            return None
        else:
            return rev_id

    async def get_upstream_revision_id(
        self, session: aiohttp.ClientSession
    ) -> str | None:
        await self.ensure_paths(session)
        if not hasattr(self, "_us_rev_id"):
            await self.retrieve_package(session)
            if not self.retrieved:
                self.error_msg = "Unable to download package from AUR."
                return None
            pkgbuild = os.path.join(self.build_dir, self.name, "PKGBUILD")
            git_url = await self._get_source_from_pkgbuild(pkgbuild)
            if git_url is None:
                self.error_msg = "Unable to retrieve upstream url from PKGBUILD."
                return None
            git_log = await self._git_ls_remote(git_url)
            if git_log is None:
                self.error_msg = (
                    "Unable to retrieve upstream log for package's git repo."
                )
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
