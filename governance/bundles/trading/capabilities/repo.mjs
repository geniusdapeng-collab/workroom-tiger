/** Tiger industry adapter. Packaged kernel root is supplied by the trusted launcher. */
import { isAbsolute, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const kernel = process.env.TIGER_KERNEL_ROOT ?? resolve(import.meta.dirname, "..", "..", "..", "..");
if (!isAbsolute(kernel)) throw new Error("TIGER_KERNEL_ROOT must be an absolute trusted launcher path");
const { createRepoCapabilities } = await import(pathToFileURL(resolve(kernel, "bundles/trading/capabilities/repo.mjs")));
const capabilities = createRepoCapabilities(resolve(import.meta.dirname, "..", "..", ".."));
export const { repoStatus, repoDocs, bundleSummary, repoReadiness } = capabilities;
