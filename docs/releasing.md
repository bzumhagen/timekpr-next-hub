# Releasing

Releases are cut by pushing a tag. Everything else —
tests, the Arch package, the container image, the GitHub Release, the AUR
push — is
[`.github/workflows/release.yml`](../.github/workflows/release.yml).

One version covers all three packages (`timekpr-hub`,
`timekpr-hub-agent`, `timekpr-hub-core`). They are released together
because they share a wire contract and the same `timekpr_hub_core`
convergence math: an agent and a hub from different releases have no
tested-together guarantee, so giving them separate version numbers would
imply a compatibility matrix nobody maintains.

## Cutting a release

```sh
make bump VERSION=0.2.0        # rewrites all six version sites, then verifies
$EDITOR CHANGELOG.md           # move Unreleased -> ## [0.2.0] - YYYY-MM-DD
make check                     # version + lint + typecheck + tests
git commit -am "Release 0.2.0"
git tag -a v0.2.0 -m "Release 0.2.0"
git push --follow-tags
```

That's it. The tag must be `v` + the version, and the version must be
`MAJOR.MINOR.PATCH` with an optional PEP 440 pre-release suffix
(`0.2.0rc1`) — **not** `0.2.0-rc1`, because `makepkg` forbids a hyphen in
`pkgver`. A pre-release version is marked as such on GitHub and does not
move the `latest` container tag.

## What the tag produces

| Artifact | Where it lands |
| --- | --- |
| `timekpr-next-hub-X.Y.Z.tar.gz` | GitHub Release — source of record; the Arch package's `source=()` and the Proxmox install both consume it |
| `timekpr_hub*-X.Y.Z-*.whl` + sdists | GitHub Release |
| `SHA256SUMS` | GitHub Release |
| `timekpr-hub-agent-X.Y.Z-1-any.pkg.tar.zst` | GitHub Release — installable with `pacman -U` |
| `PKGBUILD` (checksummed) | GitHub Release, and pushed to the AUR |
| `ghcr.io/bzumhagen/timekpr-hub:X.Y.Z`, `:X.Y`, `:latest` | GHCR, multi-arch (amd64 + arm64) |

The release is created **after** every build job succeeds, so there is no
window in which a published release is missing assets. If a job fails
partway, nothing is published and the tag can be deleted, fixed, and
re-pushed.

## Why the version lives in six places

`scripts/check_versions.py` is what keeps them equal, and CI fails if they
drift:

- the four `pyproject.toml` files — uv wants a literal `version` in each
  workspace member;
- `AGENT_VERSION` in `agent/timekpr_hub_agent/main.py` — a literal rather
  than `importlib.metadata.version()`, because the Arch package installs
  raw `.py` files with no dist-info and the metadata lookup would raise on
  every real install;
- `pkgver` in `agent/packaging/PKGBUILD` — makepkg needs a literal, and it
  also names the release tarball the PKGBUILD downloads.

`make bump` writes all six. The release workflow re-checks them against
the tag before building anything, so a tag can never ship a package that
reports a different version than it is.

## The PKGBUILD's checksum

`agent/packaging/PKGBUILD` carries `sha256sums=('SKIP')` in this repo and
nowhere else. The checksum is of a tarball built from the tag, and the
PKGBUILD is *inside* that tarball, so it cannot contain its own digest.

`scripts/build_arch_package.sh` runs `updpkgsums` against the actual
tarball right before building, and the real-digest PKGBUILD it produces is
what goes to the GitHub Release and to the AUR. AUR users therefore always
get a checksummed PKGBUILD; only the in-repo copy is `SKIP`.

The same script runs on every PR (`--local` mode, packaging a tarball made
from the branch), so a change to `agent/packaging/` that breaks the build
fails in review rather than during a release.

## One-time setup

### AUR publishing

The `aur` job skips itself with a notice if `AUR_SSH_PRIVATE_KEY` isn't
set, so releases work fine before this is done.

1. Create an account on [aur.archlinux.org](https://aur.archlinux.org) and
   add an SSH public key to it.
2. Create the package there once, by hand — the AUR has no "create via
   push to a nonexistent repo" flow for a name that was never submitted:

   ```sh
   git clone ssh://aur@aur.archlinux.org/timekpr-hub-agent.git
   # copy PKGBUILD, .SRCINFO and timekpr-hub-agent.install from the first
   # release's assets, then commit and push
   ```

3. Add the matching **private** key as the `AUR_SSH_PRIVATE_KEY` repository
   secret (Settings → Secrets and variables → Actions).

Subsequent releases push themselves. If the AUR package is already at the
released version, the job notices and does nothing.

**Verify the pinned host key.** The `aur` job pins
`aur.archlinux.org`'s SSH fingerprint and fails closed if what answers
doesn't match. Check the pinned value in the workflow against the
[Arch wiki](https://wiki.archlinux.org/title/Arch_User_Repository) before
the first AUR release, and update it if the AUR ever rotates its host key.

### Container registry

Nothing to set up: the `image` job pushes to GHCR with the workflow's own
`GITHUB_TOKEN`. The **first** push creates the package as private, though —
make `timekpr-hub` public once (Packages → timekpr-hub → Package settings)
or nobody else can pull it.

## Releasing by hand

Not the normal path, but everything the workflow does is a make target:

```sh
make check-version                # all six agree?
make release-tarball              # dist/timekpr-next-hub-X.Y.Z.tar.gz
make build-wheels                 # dist/*.whl
make pkg                          # dist/arch/*.pkg.tar.zst (Arch host only)
make release-notes                # what the release body would say
```

`make pkg` builds from `HEAD`, so it deliberately does **not** emit a
publishable PKGBUILD — its checksum would be of a tarball that was never
released. Use `scripts/build_arch_package.sh --tarball <path>` for that.
