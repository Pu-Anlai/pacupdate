import aiohttp
import pytest
from aioresponses import aioresponses
from pacupdate.structs import AURDeps, AURPackage, Config


@pytest.mark.asyncio
async def test_AURPackage():
    with aioresponses() as mock:
        mock.get(
            "https://aur.archlinux.org/rpc/v5/info?arg[]=clightd-git",
            body='{"resultcount":1,"results":[{"Conflicts":["clightd"],"Depends":["ddcutil","libdrm","libiio","libjpeg-turbo","libmodule","libusb","libx11","libxext","linux-api-headers","libxrandr","polkit","systemd-libs","wayland"],"Description":"Bus interface to change screen brightness and capture frames from webcam.","FirstSubmitted":1492337955,"ID":1389403,"Keywords":[],"LastModified":1705783001,"License":["GPL-3.0-or-later"],"Maintainer":"yochananmarqos","MakeDepends":["cmake","git"],"Name":"clightd-git","NumVotes":8,"OutOfDate":null,"PackageBase":"clightd-git","PackageBaseID":121546,"Popularity":0,"Provides":["clightd"],"Submitter":"Nierro","URL":"https://github.com/FedeDP/Clightd","URLPath":"/cgit/aur.git/snapshot/clightd-git.tar.gz","Version":"5.8.r9.g7d447d0-1"}],"type":"multiinfo","version":5}',
            content_type="application/json",
        )
        mock.get(
            "https://aur.archlinux.org/rpc/v5/info?arg[]=libmodule",
            body='{"resultcount":1,"results":[{"Description":"C linux library to build simple and modular projects","FirstSubmitted":1525164351,"ID":835850,"Keywords":[],"LastModified":1608459422,"License":["MIT"],"Maintainer":"Nierro","MakeDepends":["git","cmake"],"Name":"libmodule","NumVotes":10,"OutOfDate":null,"PackageBase":"libmodule","PackageBaseID":131994,"Popularity":0.65239,"Submitter":"Nierro","URL":"https://github.com/FedeDP/libmodule","URLPath":"/cgit/aur.git/snapshot/libmodule.tar.gz","Version":"5.0.1-1"}],"type":"multiinfo","version":5}',
            content_type="application/json",
        )
        conf = Config()
        pkg = AURPackage("clightd-git", conf)
        async with aiohttp.ClientSession() as session:
            await pkg.get_deps(conf, session)
            assert pkg.lost_deps == AURDeps(
                depends=[], make_depends=[], check_depends=[]
            )
            assert pkg.aur_deps == AURDeps(
                depends=["libmodule"], make_depends=[], check_depends=[]
            )
            assert pkg.pm_deps == AURDeps(
                depends=[
                    "ddcutil",
                    "libdrm",
                    "libiio",
                    "libjpeg-turbo",
                    "libusb",
                    "libx11",
                    "libxext",
                    "linux-api-headers",
                    "libxrandr",
                    "polkit",
                    "systemd-libs",
                    "wayland",
                ],
                make_depends=["git", "cmake"],
                check_depends=[],
            )
