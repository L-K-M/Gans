#!/bin/bash
#
# Clean build for Gans.
#
# Stops any in-flight builds, RESETS Xcode's build daemon, wipes Gans's DerivedData,
# then does a fresh Release build. Run from anywhere:  ./clean-build.sh
#
# If a build hangs forever at "CreateBuildDescription" / the initial clang probe (clang
# at 0% CPU), the Swift Build service has wedged. This script force-resets it before each
# build to avoid that; if it still wedges, reboot.
#
cd "$(dirname "$0")" || exit 1

# SIGKILL (-9): a process wedged in write() ignores the default SIGTERM.
killall -9 xcodebuild clang swift-frontend SWBBuildService XCBBuildService 2>/dev/null
sleep 1

xcodebuild -project Gans.xcodeproj -scheme Gans clean
rm -rf ~/Library/Developer/Xcode/DerivedData/Gans-*
xcodebuild -project Gans.xcodeproj -scheme Gans -configuration Release build
status=$?

# On a successful build, reveal the product in Finder.
if [ $status -eq 0 ]; then
    open ~/Library/Developer/Xcode/DerivedData/Gans-*/Build/Products/Release
fi

exit $status
