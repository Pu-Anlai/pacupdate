import os
import tempfile
from configparser import ConfigParser
from dataclasses import dataclass
from typing import TypedDict

import pyalpm

from .aurpkgs import AURPackage
from .shared import die, getenv_int

TERMCOLORS = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "bold": "\033[1m",
    "default": "\033[0m",
}
BUILDDIR = tempfile.mkdtemp()


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
            self.init_pm_sync_pkgcache_str()
        return self._pm_sync_pkgcache_str

    def init_pm_sync_pkgcache_str(self):
        self._pm_sync_pkgcache_str = []
        for db in self.pm_sync_dbs:
            self._pm_sync_pkgcache_str.extend(pkg.name for pkg in db.pkgcache)
