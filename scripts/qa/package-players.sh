#!/bin/sh

# Package built QA players for teammates. Each archive unpacks directly into
# QAArtifacts/player/, which is where the platform-aware default player path looks, so a
# teammate never has to pass --player.
#
# macOS uses ditto rather than zip because a .app is a signed bundle: ditto preserves the
# code signature and extended attributes that plain zip drops, and a bundle that fails
# signature validation is rejected before it can report anything useful.

set -eu
. "$(dirname -- "$0")/common.sh"

QA_PACKAGE_DIR=${QA_PACKAGE_DIR:-$QA_PROJECT_ROOT/QAArtifacts/dist}
QA_MAC_PLAYER=${QA_MAC_PLAYER:-$QA_DEFAULT_PLAYER_BUNDLE}

command -v ditto >/dev/null 2>&1 || qa_fail_config "ditto was not found. Packaging requires macOS."
mkdir -p "$QA_PACKAGE_DIR"

QA_PACKAGED=0

# The macOS build lives beside its episode artifacts, so archive the bundle itself rather
# than its parent directory. --keepParent keeps QaGameplay.app inside the archive.
if [ -d "$QA_MAC_PLAYER" ]; then
  ditto -c -k --sequesterRsrc --keepParent "$QA_MAC_PLAYER" "$QA_PACKAGE_DIR/qa-player-macos.zip"
  QA_PACKAGED=$((QA_PACKAGED + 1))
  printf '%s\n' "Packaged macOS: $QA_PACKAGE_DIR/qa-player-macos.zip"
fi

# Linux and Windows builds are archived without their parent, so the executable and its
# companion _Data directory land directly in QAArtifacts/player/ when unpacked.
for QA_PACKAGE_PLATFORM in linux windows; do
  QA_PACKAGE_SOURCE="$QA_PACKAGE_DIR/$QA_PACKAGE_PLATFORM"
  [ -d "$QA_PACKAGE_SOURCE" ] || continue
  ditto -c -k --sequesterRsrc "$QA_PACKAGE_SOURCE" "$QA_PACKAGE_DIR/qa-player-$QA_PACKAGE_PLATFORM.zip"
  QA_PACKAGED=$((QA_PACKAGED + 1))
  printf '%s\n' "Packaged $QA_PACKAGE_PLATFORM: $QA_PACKAGE_DIR/qa-player-$QA_PACKAGE_PLATFORM.zip"
done

[ "$QA_PACKAGED" -gt 0 ] ||
  qa_fail "No player builds were found. Run scripts/qa/build-player.sh and the per-platform build scripts first."
printf '%s\n' "Packaged $QA_PACKAGED player build(s)."
