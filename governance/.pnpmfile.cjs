const { createHash } = require("node:crypto");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");

// Tiger's canonical UI URL now uses the verified GitHub release mirror.
// This existing local transport mirror remains byte-identical to the approved
// archive; package versions, resolution URLs and integrity gates stay intact.
// See vendor/ui-artifacts/README.md for recovery provenance and maintenance.
const MIRROR = Object.freeze({
  package: "@workloom/ui",
  version: "0.1.17",
  assetName: "workloom-ui-0.1.17.tgz",
  url: "https://github.com/geniusdapeng-collab/workloom-im/releases/download/ui-v0.1.17/workloom-ui-0.1.17.tgz",
  integrity: "sha512-iJolO8NyIDoEZlMsX3BPJba9/6LbAhjhCgW7Kw5W/dRkW3MOsZ14Odlm6azcSnnpY9HhqAtea1YiYLZi4utLsw==",
});

function mirrorError(message, cause) {
  return new Error(`UI artifact mirror: ${message}`, cause ? { cause } : undefined);
}

function readConsumerState() {
  try {
    return JSON.parse(readFileSync(join(__dirname, ".workloom-ui.json"), "utf8"));
  } catch (cause) {
    throw mirrorError("cannot read consumer state .workloom-ui.json", cause);
  }
}

function verifiedArtifact(state = readConsumerState()) {
  if (state.package !== MIRROR.package || state.version !== MIRROR.version ||
      state.artifact?.url !== MIRROR.url || state.artifact?.sha512 !== MIRROR.integrity ||
      state.artifact?.assetName !== MIRROR.assetName) {
    throw mirrorError("consumer state does not match the approved mirrored release");
  }
  const target = join(__dirname, "vendor", "ui-artifacts", MIRROR.assetName);
  let bytes;
  try {
    bytes = readFileSync(target);
  } catch (cause) {
    throw mirrorError(`cannot read ${target}`, cause);
  }
  const integrity = `sha512-${createHash("sha512").update(bytes).digest("base64")}`;
  if (integrity !== MIRROR.integrity) {
    throw mirrorError(`artifact integrity mismatch for ${MIRROR.assetName}`);
  }
  return target;
}

function localResolution(resolution) {
  if (resolution.tarball !== MIRROR.url) return null;
  // New URL resolution may not have an integrity yet. Supply the approved SRI
  // before extraction; an existing integrity is never weakened or replaced.
  if (resolution.integrity != null && resolution.integrity !== MIRROR.integrity) {
    throw mirrorError("lockfile integrity does not match the approved release");
  }
  return {
    ...resolution,
    tarball: `file:${verifiedArtifact()}`,
    integrity: MIRROR.integrity,
  };
}

function preResolution({ wantedLockfile }) {
  const state = readConsumerState();
  // Manifests and consumer state change before pnpm resolves an upgrade. Let a
  // different approved release replace the old wanted/current lockfile through
  // ordinary pnpm resolution and the existing consumer-state gates.
  if (state.artifact?.url !== MIRROR.url) return;
  const packageKey = `${MIRROR.package}@${MIRROR.url}`;
  const snapshots = Object.entries(wantedLockfile?.packages ?? {}).filter(([key, entry]) =>
    key === packageKey || key.startsWith(`${packageKey}(`) || entry.resolution?.tarball === MIRROR.url);
  verifiedArtifact(state);
  for (const [, entry] of snapshots) {
    if (entry.version !== MIRROR.version || entry.resolution?.tarball !== MIRROR.url ||
        entry.resolution?.integrity !== MIRROR.integrity) {
      throw mirrorError("lockfile does not match the approved mirrored release");
    }
  }
  // Keep the in-memory lockfile untouched, including --lockfile-only. Early
  // configuration validation below also covers optimistic cached installs.
}

module.exports = {
  hooks: {
    updateConfig(config) {
      // pnpm can skip resolution on an optimistic repeat install. Validate the
      // active archive while loading configuration, before that shortcut.
      const state = readConsumerState();
      if (state.artifact?.url === MIRROR.url) verifiedArtifact(state);
      return config;
    },
    preResolution,
    // pnpm 10 (the version pinned in package.json) supplies fetcher factories.
    fetchers: {
      remoteTarball: ({ defaultFetchers }) => (cafs, resolution, options) => {
        const local = localResolution(resolution);
        return local
          ? defaultFetchers.localTarball(cafs, local, options)
          : defaultFetchers.remoteTarball(cafs, resolution, options);
      },
    },
  },
  // pnpm 11 moved fetcher registration to this top-level extension interface.
  // Both paths delegate extraction to pnpm with the original approved SRI.
  fetchers: [{
    canFetch: (_packageId, resolution) => resolution.tarball === MIRROR.url,
    fetch: (cafs, resolution, options, fetchers) =>
      fetchers.localTarball(cafs, localResolution(resolution), options),
  }],
};
