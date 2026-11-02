# Maintainer: Your Name <you@example.com>
pkgname=ascii-art-studio
pkgver=1.0.0
pkgrel=1
pkgdesc='Convert images to ASCII art (PNG, JPEG, TXT) with a Qt GUI'
arch=('any')
url='https://github.com/YOUR_USERNAME/ascii-art-studio'   # TODO: replace
license=('MIT')
depends=('python' 'pyside6' 'python-pillow' 'python-numpy' 'ttf-dejavu' 'hicolor-icon-theme')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
optdepends=('qt6-wayland: native Wayland support'
            'xcb-util-cursor: required by Qt on some X11 setups')
# Local project: the sources are taken from the directory that holds this PKGBUILD.
source=()

prepare() {
  rm -rf "$srcdir/$pkgname"
  mkdir -p "$srcdir/$pkgname"
  cp -r "$startdir"/{ascii_art_studio,tests,data,pyproject.toml,README.md,LICENSE} "$srcdir/$pkgname/"
  find "$srcdir/$pkgname" -name __pycache__ -type d -prune -exec rm -rf {} +
}

build() {
  cd "$srcdir/$pkgname"
  python -m build --wheel --no-isolation
}

check() {
  cd "$srcdir/$pkgname"
  python -m unittest discover -s tests -t .
}

package() {
  cd "$srcdir/$pkgname"
  python -m installer --destdir="$pkgdir" dist/*.whl

  install -Dm644 data/ascii-art-studio.desktop -t "$pkgdir/usr/share/applications/"
  install -Dm644 data/ascii-art-studio.svg \
    "$pkgdir/usr/share/icons/hicolor/scalable/apps/ascii-art-studio.svg"
  install -Dm644 LICENSE -t "$pkgdir/usr/share/licenses/$pkgname/"
  install -Dm644 README.md -t "$pkgdir/usr/share/doc/$pkgname/"
}
