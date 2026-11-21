#!/bin/bash
# The post node's check (#1633), after the node's own: swift.org's toolchain of the release .swift-version pins, the one
# Bazel downloads for deploy/release-folder.sh, runs here. Every executable and library at the top of its bin/ and lib/
# finds all its shared libraries, and its clang++ with ld.lld and its swiftc each link a program that then runs. The
# runtime image check uses the baked, off-PATH toolchain. Standalone checks can download and verify a temporary copy.
#
#   post-check.sh              download SWIFT_VERSION's toolchain and check it
#   post-check.sh <usr folder> check an unpacked toolchain's usr/ (LDD may name another ldd)
set -euo pipefail
ldd=${LDD:-ldd}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
if [ $# -eq 1 ]; then
  toolchain=$1
else
  for name in SWIFT_VERSION SWIFT_SHA256; do
    if [ -z "${!name:-}" ]; then echo "post-check.sh: $name is required" >&2; exit 1; fi
  done
  release="swift-${SWIFT_VERSION}-RELEASE"
  curl --fail --silent --show-error --location --retry 3 -o "$work/swift.tar.gz" \
    "https://download.swift.org/swift-${SWIFT_VERSION}-release/ubuntu2604/${release}/${release}-ubuntu26.04.tar.gz"
  echo "${SWIFT_SHA256}  $work/swift.tar.gz" | sha256sum -c -
  mkdir "$work/toolchain"
  tar -xzf "$work/swift.tar.gz" -C "$work/toolchain" --strip-components=1 --no-same-owner
  rm "$work/swift.tar.gz"
  toolchain=$work/toolchain/usr
fi

checked=0
while IFS= read -r -d '' file; do
  [ "$(head -c 4 "$file" | od -An -c | tr -d ' ')" = '177ELF' ] || continue
  if readelf -dW "$file" | grep -q '(NEEDED)'; then
    "$ldd" "$file" | { grep 'not found' || true; } | sed "s|^[[:space:]]*|${file#"$toolchain"/}: |" >> "$work/missing"
    checked=$((checked + 1))
  fi
done < <(find "$toolchain/bin" "$toolchain/lib" -maxdepth 1 -type f -print0)
if [ -s "$work/missing" ]; then
  echo "post node: the toolchain's shared libraries are missing:" >&2
  cat "$work/missing" >&2
  exit 1
fi
[ "$checked" -gt 0 ] || { echo "post node: no dynamic executable found in $toolchain" >&2; exit 1; }

printf '#include <iostream>\nint main() { std::cout << "c++ ok" << std::endl; }\n' > "$work/hello.cc"
"$toolchain/bin/clang++" -fuse-ld=lld "$work/hello.cc" -o "$work/hello-cc"
test "$("$work/hello-cc")" = 'c++ ok'
printf 'print("swift ok")\n' > "$work/hello.swift"
"$toolchain/bin/swiftc" -module-cache-path "$work/module-cache" "$work/hello.swift" -o "$work/hello-swift"
test "$("$work/hello-swift")" = 'swift ok'
echo "post node: $checked toolchain binaries find their libraries; clang++ with ld.lld and swiftc link and run"
