# Maintainer: Pu Anlai
pkgname=pacupdate
pkgver=0.5
pkgrel=1
pkgdesc="Simple but hopefully complete pacman update script"
arch=('any')
url="https://github.com/Pu-Anlai/pacupdate"
license=('MIT')
depends=('python-aiohttp' 'python-feedparser' 'pyalpm' 'python-packaging')
makedepends=('python-build' 'python-installer' 'python-wheel' 'python-hatchling')
checkdepends=('python-pytest' 'python-pytest-asyncio')
install=
source=("https://files.pythonhosted.org/packages/source/${pkgname::1}/${pkgname//-/_}/${pkgname//-/_}-$pkgver.tar.gz")
sha256sums=("9b8d16120a16648572548d3cd2d3754a994c0def5bdd4149fc57f2409876e585")

build() {
  cd "${srcdir}/${pkgname}-${pkgver}" || return 1
  python -m build --wheel --no-isolation
}

check() {
  # python-aiointercept is currently not in the AUR (and I don't wanna maintain
  # a package for it) so we're hacking around this with a virtual env
  _pkg_src_dir="${srcdir}/${pkgname}-${pkgver}"
  python -m venv "${_pkg_src_dir}"
  PYTHONPATH="${_pkg_src_dir}" "${_pkg_src_dir}/bin/pip" install aiointercept
  cd "${_pkg_src_dir}" || return 1
  PYTHONPATH="${_pkg_src_dir}" pytest
}

package() {
  cd "${srcdir}/${pkgname}-${pkgver}" || return 1
  python -m installer --destdir="$pkgdir" dist/*.whl
}

# vim:set ts=2 sw=2 et:
