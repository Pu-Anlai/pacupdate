# pacupdate - A pacman update script

I don't like pacman wrapper programs that obfuscate the entire package management process away from you. Nevertheless, keeping a pacman-based system up-to-date can be tedious as it often involves more than just running `pacman -Syu`. So here's a Python script that runs through the complete upgrade process (as I understand it). Specifically, these are the steps that the script covers:
* Update the mirrorlist if necessary
* Check if there have been any updates to the Arch News mailing list since the last upgrade
* Check for updates to packages from the pacman repos and from the AUR
  * AUR packages will be considered outdated if any of their dependencies are scheduled for an upgrade
  * AUR packages whose names end in "-git" will also be considered outdated if their upstream git repository has been updated during the last 2 weeks (by default)
* Upgrade all packages that are out-of-date
  * this includes the full build process for AUR packages
* Print out any warnings that Pacman issued during the upgrade process
* Clean up any build dependencies installed during upgrades that are no longer needed

## Options
Okay, so there's a bunch of environment variables you can set to customise the script's behaviour:
* **PACUPDATE\_MIRRORLIST\_URL**  
URL from which pacupdate will download a fresh mirrorlist. Should be customised to fit with the system's physical location.  
*Default*: "https://archlinux.org/mirrorlist/?country=all&protocol=http&protocol=https&ip_version=4"
* **PACUPDATE_RSS_FEED_URL**  
Address from which pacupdate will fetch a current copy of the news mailinglist. No real reason to change this.  
*Default*: "https://archlinux.org/feeds/news/"
* **PACUPDATE_MIRRORLIST_INTERVAL**  
Interval in days after which a new copy of the mirrorlist should be downloaded. Determined based on the modification timestamp of the system's current mirrorlist.  
*Default*: 14
* **PACUPDATE_GIT_INTERVAL**  
Interval in days after which a git AUR package (a package with a name that ends in "-git") will be checked for updates in its upstream repository.  
*Default*: 14
* **PACUPDATE_PM_ROOT**  
The root path used by libalpm for all filesystem operations. You most likely do not need or want to change this.  
*Default*: "/"
* **PACUPDATE_PM_DBPATH**  
Path to libalpm's database.  
*Default*: "/var/lib/pacman"
* **PACUPDATE_PM_CONF**  
Path to pacman's configuration file.  
*Default*: "/etc/pacman.conf"

## Warning!
As is to be expected, this script will execute some commands with elevated privileges using the sudo command. You should _not_ execute a script (most likely one with zero stars, no less) that will run sudo-prefixed commands without checking the source for what those commands are first.
