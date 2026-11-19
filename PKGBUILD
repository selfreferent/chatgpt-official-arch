# SPDX-License-Identifier: 0BSD
# Maintainer: selfreferent

pkgname=chatgpt-official-bin
pkgver=26.1111.71704
pkgrel=1
pkgdesc="Official ChatGPT desktop app for Linux, repackaged for Arch/Artix"
arch=('x86_64')
url='https://developers.openai.com/codex/app'
# OpenAI does not ship a license for the proprietary ChatGPT application.
# The Debian copyright file currently contains third-party Electron notices only.
license=('unknown')

provides=("chatgpt=${pkgver}")
conflicts=('chatgpt')

depends=(
    'alsa-lib'
    'at-spi2-core'
    'cairo'
    'dbus'
    'expat'
    'gdk-pixbuf2'
    'glib2>=2.49.3'     # Debian libglib2.0-0 and libglib2.0-bin
    'glibc'
    'gtk3'
    'libcups'
    'libdrm'
    'libgcc'
    'libglvnd'          # Debian libgl1
    'libnotify'
    'libstdc++'
    'libudev.so'
    'libusb'
    'libx11'
    'libxcb'
    'libxcomposite'
    'libxdamage'
    'libxext'
    'libxfixes'
    'libxkbcommon'
    'libxrandr'
    'mesa'              # Debian libgbm1
    'nspr'
    'libcrypto.so=3-64'  # Debian libssl3; retain ABI 3 across OpenSSL upgrades
    'libssl.so=3-64'
    'nss'
    'pango'
    'tpm2-tss'          # Debian libtss2-*
    'vulkan-driver'     # Debian mesa-vulkan-drivers | vulkan-icd
    'vulkan-icd-loader'
    'xdg-utils'
    'xz'
)

makedepends=('python' 'curl')

optdepends=(
    'git: Codex Git/repository workflows'
    'python: bundled optional helper scripts'
)

_deb="chatgpt_${pkgver}_amd64.deb"
_upstream_base='https://persistent.oaistatic.com/codex-app-prod/linux/deb/pool/main/c/chatgpt'

source_x86_64=(
    "${_deb}::${_upstream_base}/${_deb}"
)

source=(
    'upstream.py'
    'upstream-depends.txt'
    'upstream-layout.json'
)
sha256sums=(
    '660cf11a067eee1e706bd25c2ea1e84ab090b92de5b30c53db4e0372792f318d'
    'ed54de42f676120eed6b52fdc0d5aaccdb03fcdf2d26a93dacbf8ff8e956104f'
    '92362e44c8b3bcbfeff1849796965348e7990871add4d56f5e2916c082bbb2bf'
)
noextract=("${_deb}")

sha256sums_x86_64=(
    '01eef7c77c835f7e1afb92ba1c29178a4c19d6c66573bdd3b86520e2abf806a4'
)

# Preserve OpenAI's prebuilt binaries exactly.
options=('!strip' '!debug')

package() {
    local work="$srcdir/deb-unpack"
    rm -rf "$work"
    python3 "$srcdir/upstream.py" deb "$srcdir/$_deb" "$pkgver" \
        "$srcdir/upstream-depends.txt" "$srcdir/upstream-layout.json" "$work/data" || return 1
    local data="$work/data"

    install -d \
        "$pkgdir/usr/bin" \
        "$pkgdir/usr/lib" \
        "$pkgdir/usr/share/applications" \
        "$pkgdir/usr/share/pixmaps"

    # Copy only the upstream application payload required on Arch/Artix.
    # Debian APT integration, maintainer state, and AppArmor files are
    # intentionally not copied into the package.
    cp -a --no-preserve=ownership \
        "$data/usr/lib/chatgpt" \
        "$pkgdir/usr/lib/"

    cp -a --no-preserve=ownership \
        "$data/usr/bin/chatgpt" \
        "$pkgdir/usr/bin/"

    install -Dm644 \
        "$data/usr/share/applications/chatgpt.desktop" \
        "$pkgdir/usr/share/applications/chatgpt.desktop"

    install -Dm644 \
        "$data/usr/share/pixmaps/chatgpt.png" \
        "$pkgdir/usr/share/pixmaps/chatgpt.png"

    # Preserve upstream's third-party copyright notice without treating it as
    # the license for the proprietary ChatGPT application.
    install -Dm644 \
        "$data/usr/share/doc/chatgpt/copyright" \
        "$pkgdir/usr/share/doc/$pkgname/third-party-notices"
}
