#!/bin/bash
set -euo pipefail
test "$(id -u)" = 10001
for tool in bazel git rsync python3 node curl jq openssl caddy kubectl ssh-keygen strip readelf cc; do
  command -v "$tool"
done
# What deploy/release-folder.sh does with them on the post node (#1350): strip a static binary, and read its program
# headers and dynamic section.
strip --version | head -n 1
readelf -lW /usr/local/bin/kubectl | grep -q GNU_STACK
readelf -dW /usr/local/bin/kubectl > /dev/null
# The policy suite compiles Linux subreaper fixtures; check the compiler and libc
# headers as the non-root job user before the image can be published (#1520).
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cat > "$work/probe.c" <<'C'
#include <sys/prctl.h>
int main(void) { return prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0); }
C
cc -std=c11 -Wall -Wextra -Werror -O2 "$work/probe.c" -o "$work/probe"
"$work/probe"
# The image carries the pinned Bazel binary. Keep pipefail so a failed invocation
# cannot pass just by printing the expected label before it exits.
bazel version --gnu_format | grep -Fx 'bazel 9.3.2'
test -w "$HOME"
test ! -e /usr/bin/swift
# The Swift runtime a farm-built aid loads (#1521, its NEEDED libraries in /usr/lib/swift/linux), each finding all of its
# own; and still no Swift or clang compiler (the cheap layer's C fixtures use Ubuntu's gcc, #1520).
for lib in libswiftCore libswiftSwiftOnoneSupport libswiftObservation libswift_Concurrency libswift_StringProcessing \
  libswift_RegexParser libswiftGlibc libBlocksRuntime libdispatch libswiftDispatch libswiftSynchronization \
  libFoundationEssentials libFoundation libFoundationInternationalization libFoundationNetworking lib_FoundationICU; do
  test -f "/usr/lib/swift/linux/$lib.so"
  if ldd "/usr/lib/swift/linux/$lib.so" | grep 'not found'; then echo "node image: $lib.so is missing a library" >&2; exit 1; fi
done
for tool in swift swiftc clang clang++; do
  if command -v "$tool"; then echo "node image: $tool is on PATH; the node has no Swift or clang compiler" >&2; exit 1; fi
done
python3 -c 'import ast; ast.parse(open("/usr/local/bin/zuul-farm-credential").read())'
echo 'node image: tools, Bazel, the Swift runtime without a Swift compiler, writable home and non-root user checked'
