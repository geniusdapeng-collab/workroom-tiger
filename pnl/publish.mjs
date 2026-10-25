import { readFile, stat } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { digest, validateRecord, validateResearch } from './lib/schema.mjs';

export async function publishRecord({ baseUrl, token, record, research = false, allowLocalHttp = false, fetcher = fetch }) {
  (research ? validateResearch : validateRecord)(record);
  if (typeof token !== 'string' || token.length < 32 || token.length > 512 || /\s/.test(token)) throw new Error('TIGER_PNL_WRITE_TOKEN must contain 32-512 whitespace-free characters');
  const url = new URL(baseUrl);
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('TIGER_PNL_URL must be an origin without credentials, path or query');
  if (url.protocol !== 'https:' && !(allowLocalHttp && url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname))) throw new Error('HTTPS is required; --allow-local-http permits loopback tests only');
  const request = async (path, options) => {
    const response = await fetcher(new URL(path, url), { ...options, redirect: 'error', signal: AbortSignal.timeout(20_000), headers: { Authorization: `Bearer ${token}`, ...options.headers } });
    const body = await response.json();
    if (!response.ok) throw new Error(`Publisher failed (${response.status} ${body.error?.code ?? 'UNKNOWN_ERROR'})`);
    return body;
  };
  const key = record.idempotencyKey;
  const posted = await request(research ? '/api/v1/research' : '/api/v1/records', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key }, body: JSON.stringify(record) });
  const verified = await request(`${research ? '/api/v1/research/receipts/' : '/api/v1/receipts/'}${encodeURIComponent(key)}`, { method: 'GET' });
  if (posted.receipt?.idempotencyKey !== key || posted.receipt?.payloadHash !== digest(record) || verified.receipt?.hash !== posted.receipt.hash || verified.receipt?.payloadHash !== digest(record) || verified.receipt?.idempotencyKey !== key) throw new Error('Read-back receipt did not match the submitted payload');
  return verified.receipt;
}
async function main() {
  const args = process.argv.slice(2); const allowLocalHttp = args.includes('--allow-local-http'); const research = args.includes('--research'); const files = args.filter(arg => !['--allow-local-http', '--research'].includes(arg));
  if (files.length !== 1 || files[0].startsWith('--')) throw new Error('Usage: node pnl/publish.mjs /private/path/snapshot.json [--research] [--allow-local-http]');
  const info = await stat(files[0]); if (!info.isFile() || info.size > 1_048_576) throw new Error('Snapshot must be a JSON file of at most 1 MiB');
  const record = JSON.parse(await readFile(files[0], 'utf8'));
  const receipt = await publishRecord({ baseUrl: process.env.TIGER_PNL_URL ?? 'https://tigerpnl.com', token: process.env.TIGER_PNL_WRITE_TOKEN, record, research, allowLocalHttp });
  console.log(JSON.stringify({ status: 'published_and_verified', receipt }, null, 2));
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) main().catch(error => { console.error(JSON.stringify({ status: 'failed', message: error.message })); process.exitCode = 1; });
