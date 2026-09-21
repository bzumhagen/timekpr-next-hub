#!/usr/bin/env bash
# Build agent/packaging/PKGBUILD in a throwaway scratch directory.
#
# Used by both .github/workflows/ci.yml (--local, on every PR) and
# .github/workflows/release.yml (--tarball, on a tag), so there is one
# copy of the makepkg dance rather than two drifting blocks of YAML. Also
# runnable by hand on any Arch box -- `make pkg`.
#
# Two modes:
#   --local            build a tarball from the current HEAD and package
#                      that. Proves the PKGBUILD works; the PKGBUILD it
#                      produces is NOT publishable, because its checksum is
#                      of a tarball that was never released.
#   --tarball PATH     package an already-built release tarball (what the
#                      release workflow attaches to the GitHub Release).
#                      The PKGBUILD it produces carries that tarball's real
#                      sha256 and is what gets pushed to the AUR.
#
# Outputs into --out (default dist/arch/): the .pkg.tar.zst, and in
# --tarball mode the checksummed PKGBUILD, its .SRCINFO, and the .install
# file -- i.e. exactly the three files the AUR repo holds.
set -euo pipefail

usage() {
	cat >&2 <<-EOF
	usage: $0 (--local | --tarball PATH) [--out DIR]

	  --local          package a tarball built from the current HEAD
	  --tarball PATH   package this already-built release tarball
	  --out DIR        where to put the results (default: dist/arch)
	EOF
	exit 2
}

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mode=""
tarball=""
out_dir="$repo_root/dist/arch"

while [[ $# -gt 0 ]]; do
	case "$1" in
	--local)
		mode=local
		shift
		;;
	--tarball)
		mode=tarball
		tarball="${2:-}"
		[[ -n "$tarball" ]] || usage
		shift 2
		;;
	--out)
		out_dir="${2:-}"
		[[ -n "$out_dir" ]] || usage
		shift 2
		;;
	*) usage ;;
	esac
done
[[ -n "$mode" ]] || usage

mkdir -p "$out_dir"
out_dir=$(cd "$out_dir" && pwd)

packaging_dir="$repo_root/agent/packaging"
# The one version the whole build keys off. check_versions.py has already
# proven (in CI's lint job, and in the release workflow's own gate) that
# this equals the tag and every pyproject.
pkgver=$(sed -n 's/^pkgver=//p' "$packaging_dir/PKGBUILD")
[[ -n "$pkgver" ]] || {
	echo "could not read pkgver from $packaging_dir/PKGBUILD" >&2
	exit 1
}

# `source=(...::URL)` renames the download to this; makepkg skips the
# download entirely when a file of that name is already in SRCDEST, which
# is how both modes build with no network and no published release.
src_name="timekpr-hub-agent-$pkgver.tar.gz"

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

cp "$packaging_dir/PKGBUILD" "$packaging_dir/timekpr-hub-agent.install" "$scratch/"

if [[ "$mode" == local ]]; then
	echo "==> building a tarball from HEAD (prefix timekpr-next-hub-$pkgver/)"
	# Run before the drop to `builder` below, so the archive is made by
	# whoever owns the checkout. `-c safe.directory` (per-invocation, not
	# a global config write) covers running as root over a bind-mounted
	# checkout owned by someone else, which is what a local container
	# reproduction of the CI job looks like.
	git -c safe.directory='*' -C "$repo_root" archive --format=tar.gz \
		--prefix="timekpr-next-hub-$pkgver/" -o "$scratch/$src_name" HEAD
else
	[[ -f "$tarball" ]] || {
		echo "no such tarball: $tarball" >&2
		exit 1
	}
	cp "$tarball" "$scratch/$src_name"
fi

# makepkg refuses to run as root, which is exactly what a container is.
build_as=""
if [[ $(id -u) -eq 0 ]]; then
	id -u builder >/dev/null 2>&1 || useradd -m builder
	chown -R builder "$scratch"
	build_as="builder"
fi

run() {
	if [[ -n "$build_as" ]]; then
		runuser -u "$build_as" -- env "BUILDDIR=$scratch/build" "PKGDEST=$scratch" \
			"SRCDEST=$scratch" "$@"
	else
		BUILDDIR="$scratch/build" PKGDEST="$scratch" SRCDEST="$scratch" "$@"
	fi
}

cd "$scratch"

echo "==> updpkgsums (replaces the repo's placeholder SKIP with the real digest)"
run updpkgsums

echo "==> makepkg"
# --nodeps: `timekpr-next` is AUR-only, so it cannot be installed here --
# and nothing in depends= is needed to *build* an arch=any package whose
# check() is stdlib-only anyway.
run makepkg --noconfirm --nodeps --cleanbuild

run makepkg --printsrcinfo >"$scratch/.SRCINFO"

cp "$scratch"/*.pkg.tar.zst "$out_dir/"

if [[ "$mode" == tarball ]]; then
	cp "$scratch/PKGBUILD" "$scratch/.SRCINFO" "$scratch/timekpr-hub-agent.install" "$out_dir/"
else
	echo "==> --local: not emitting PKGBUILD/.SRCINFO -- their checksum is of an" >&2
	echo "    unpublished tarball and must never reach the AUR" >&2
fi

if command -v namcap >/dev/null 2>&1; then
	# Advisory only, and expected to be noisy: the build above is
	# --nodeps, so namcap sees none of depends= installed and reports
	# every cross-module import as an "uninstalled dependency". Worth
	# scanning for the things it does catch -- file permissions, missing
	# licenses, bad paths.
	echo "==> namcap (advisory)"
	namcap "$out_dir"/*.pkg.tar.zst || true
fi

echo "==> done:"
ls -1 "$out_dir"
