# Approved UI artifact transport mirror

This directory contains the exact archive already approved by Tiger's
`.workloom-ui.json` and `pnpm-lock.yaml`. On 2026-10-20 the canonical CNB
download returned HTTP 404, preventing a clean frozen installation.

| Property | Value |
| --- | --- |
| Package | `@workloom/ui@0.1.17` |
| Archive | `workloom-ui-0.1.17.tgz` |
| Size | 90,041 bytes |
| Original SHA-512 | `sha512-iJolO8NyIDoEZlMsX3BPJba9/6LbAhjhCgW7Kw5W/dRkW3MOsZ14Odlm6azcSnnpY9HhqAtea1YiYLZi4utLsw==` |
| Source tag | [`ui-v0.1.17`](https://github.com/geniusdapeng-collab/workloom-im/tree/ui-v0.1.17) |
| Source commit | `91610daf0962a6c24bc7b8cde6c802662eb8b2f7` |

The archive was recovered from that public source commit using its original
prepack process (`pnpm@10.14.0`), followed by `npm@11.6.2 pack --ignore-scripts`.
The prepack checks passed 56 tests. The resulting archive matched the original
approved SHA-512 exactly. No UI source, dependency version, strategy, or business
configuration was changed in the recovery.

`../../.pnpmfile.cjs` intercepts only this exact canonical artifact URL and
delegates to pnpm's local tarball fetcher. It validates the consumer identity,
version, archive name, locked integrity and archive bytes. pnpm still verifies
the approved integrity during extraction. Missing or corrupted mirror files
fail before installation, including when the store already contains the UI.
All other packages keep their ordinary download behavior.

The checked-in lockfile, package overrides, consumer state and shared UI gates
retain the canonical CNB URL and original SHA-512. The hook does not rewrite
lockfile objects or store an absolute local path. Its complete source is bound
by `pnpmfileChecksum` in the lockfile.

To verify the mirror without installing dependencies, run from the repository
root:

```sh
node --test tests/install-contract.test.mjs tests/ui-artifact-mirror.test.mjs
```

Normal installation remains `pnpm install --frozen-lockfile` from `governance/`,
using the package manager pinned in `package.json`. The fetcher adapters cover
the pinned pnpm 10 API and pnpm 11's new registration API.
The interfaces are documented in [pnpm's hook reference](https://pnpm.io/pnpmfile)
and the [pinned pnpm 10.14.0 fetcher implementation](https://github.com/pnpm/pnpm/blob/v10.14.0/pkg-manager/client/src/index.ts).

Future UI upgrades continue through the approved upgrade process. A different
approved artifact URL uses its own normal remote transport. To mirror another
unavailable release, first recover the exact bytes and verify its already
approved SHA-512, then update the mirror binding, archive and hook checksum
together. Never change an approved checksum merely to accept a reconstruction.
