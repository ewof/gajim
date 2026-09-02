#!/usr/bin/env bash
# Copyright 2016 Christoph Reiter
#
# SPDX-License-Identifier: GPL-2.0-or-later

DIR="$( cd "$( dirname "$0" )" && pwd )"
source "$DIR"/_base.sh

function main {
    require_ucrt64
    set_build_root
    echo "==> Installing host build tools"
    install_pre_deps
    echo "==> Creating build root"
    create_root
    echo "==> Installing MinGW dependencies"
    install_mingw_deps
    echo "==> Installing Python dependencies"
    install_python_deps
    echo "==> Post-install (icons/schemas)"
    post_install_deps
    echo "==> Installing Gajim into the build root"
    install_gajim
    echo "==> Cleaning build root"
    cleanup_install
    echo "==> Building NSIS installer"
    build_exe_installer
    echo "==> NSIS installers (send one of these):"
    ls -la "${BUILD_ROOT}"/Gajim.exe "${BUILD_ROOT}"/Gajim-Portable.exe
    echo "==> Building MSIX (optional, needs Windows SDK)"
    if build_msix_installer; then
        echo "==> MSIX: ${BUILD_ROOT}/Gajim.msixbundle"
    else
        echo "==> Skipping MSIX. The NSIS .exe is enough to send."
    fi
}

main "$@";
