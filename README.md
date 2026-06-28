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

There is an AUR PKGBUILD available [here](https://aur.archlinux.org/packages/pacupdate).

## Configuration
To customise the script's behavior, a config file can be created at $XDG_CONFIG_HOME/pacupdate/pacupdate.conf (defaults to ~/.config/pacupdate/pacupdate.conf). Configuration settings should be put either under a `[general]` or a `[build_commands]` header and follow a simple format of `option = setting`.
### General
These options are supported under the `[general]` header
* **mirrorlist_url**  
URL from which pacupdate will download a fresh mirrorlist. Should be customised to fit with the system's physical location.  
*Default*: "https://archlinux.org/mirrorlist/?country=all&protocol=http&protocol=https&ip_version=4"
* **rss_feed_url**  
Address from which pacupdate will fetch a current copy of the news mailinglist. No real reason to change this.  
*Default*: "https://archlinux.org/feeds/news/"
* **mirrorlist_interval**  
Interval in days after which a new copy of the mirrorlist should be downloaded. Determined based on the modification timestamp of the system's current mirrorlist.  
*Default*: 14
* **git_interval**  
Interval in days after which a git AUR package (a package with a name that ends in "-git") will be checked for updates in its upstream repository.  
*Default*: 14

The following should only be changed if you have a very specific reason for doing so and you know what you are doing:
* **mirrorlist_path**  
The path to the mirrorlist used by pacman.
* **pacman_db_root**  
The root path used by libalpm for all filesystem operations.  
*Default*: "/"
* **pacman_db_path**  
Path to libalpm's database.  
*Default*: "/var/lib/pacman"
* **pacman_conf_path**  
Path to pacman's configuration file.  
*Default*: "/etc/pacman.conf"
### Build Commands
You can set a custom build command for individual AUR packages by specifying the name of the package followed by `=` and the command under the `[build_command]` header. This command will be run instead of `makepkg` from inside the folder containing the PKGBUILD. For example:
```
[build_commands]
mu = LANG=en_US.UTF-8 LC=C.UTF-8 makepkg
```
The custom command should result in a package being created in the same fashion that a succesful execution of makepkg would. Otherwise the script will not be able to locate the resulting package.  
In nearly all cases, the commands specified here should still include a call to makepkg in some way or another.

## Warning!
As is to be expected, this script will execute some commands with elevated privileges using the sudo command. You should _not_ execute a script (most likely one with zero stars, no less) that will run sudo-prefixed commands without checking the source for what those commands are first.
