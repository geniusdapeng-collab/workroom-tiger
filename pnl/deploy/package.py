#!/usr/bin/env python3
"""Reproducible website package from exact Git blobs and its shared contract.

This is a website-only package. The Python research engine and its private
accounts/configuration are never deployed to the web service by this builder.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import tarfile

SHARED = "bundles/trading/capabilities/required-v2-contract.mjs"
ENTRIES = ("pnl/server.mjs", "pnl/backup.mjs", "pnl/publish.mjs")
IMPORTS = re.compile(r'''(?:\bfrom\s*|\bimport\s*\(?\s*)['"]([^'"]+)['"]''')
PDF = "pnl/public/whitepaper/tiger-trading-20260730.pdf"
PDF_SHA = "85f5abbd26b1f76c765255ce83ba73af88282e58b480887f2464e8f6e872dc67"


def allowed(path):
    parts = PurePosixPath(path).parts
    return bool(parts and not path.startswith("/") and ".." not in parts
        and (path.startswith("pnl/") or path == SHARED)
        and not any(part.startswith(".env") or part in {"node_modules", "data", "cache", "__pycache__"}
                    or part.endswith((".pem", ".key", ".pyc")) for part in parts))


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, timeout=30)


def module_closure(files):
    visited, pending = set(), list(ENTRIES)
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        if name not in files:
            raise ValueError("Website runtime dependency is absent: " + name)
        visited.add(name)
        for specifier in IMPORTS.findall(files[name][1].decode("utf-8")):
            if specifier.startswith("node:"):
                continue
            if not specifier.startswith(("./", "../")):
                raise ValueError("Unregistered website runtime dependency")
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), specifier))
            if not allowed(target) or not target.endswith(".mjs"):
                raise ValueError("Website dependency is outside the reviewed package")
            pending.append(target)
    return sorted(visited)


def pack(files):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path, (mode, data) in sorted(files.items()):
            item = tarfile.TarInfo(path)
            item.mode, item.uid, item.gid, item.mtime = mode, 0, 0, 0
            item.uname = item.gname = ""
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    target = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0, compresslevel=9) as stream:
        stream.write(raw.getvalue())
    return target.getvalue()


def build(repo, revision):
    commit = git(repo, "rev-parse", revision + "^{commit}").decode().strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("A complete Git source identity is required")
    files = {}
    for row in git(repo, "ls-tree", "-r", "-z", commit, "--", "pnl", SHARED).split(b"\0"):
        if not row:
            continue
        metadata, name = row.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        name = name.decode("utf-8")
        if mode not in {"100644", "100755"} or kind != "blob" or not allowed(name):
            raise ValueError("Only registered regular website source files are permitted")
        data = git(repo, "cat-file", "blob", blob)
        sha = hashlib.sha256(data).hexdigest()
        limit = 4104128 if name == PDF and sha == PDF_SHA else 1048576
        if len(data) > limit:
            raise ValueError("Source exceeds its registered size bound")
        files[name] = (int(mode[-3:], 8), data)
    closure = module_closure(files)
    body = pack(files)
    manifest = {"schema": "tiger.website-package.v2", "commit": commit,
        "archiveSha256": hashlib.sha256(body).hexdigest(), "archiveBytes": len(body),
        "runtimeEntries": list(ENTRIES), "runtimeClosure": closure,
        "files": [{"path": name, "mode": mode, "sha256": hashlib.sha256(data).hexdigest(),
                   "bytes": len(data)} for name, (mode, data) in sorted(files.items())]}
    return body, manifest


def transport(manifest):
    # The remote transport downloads only the committed public allowlist, checks
    # every blob and the complete deterministic archive, then creates a new file.
    code = '''import concurrent.futures,gzip,hashlib,io,json,pathlib,tarfile,urllib.request
spec=json.loads(SPEC)
shared='bundles/trading/capabilities/required-v2-contract.mjs'
def fetch(item):
 path=item['path']; parts=pathlib.PurePosixPath(path).parts
 if not (path.startswith('pnl/') or path==shared) or '..' in parts or path.startswith('/'): raise ValueError('Invalid package path')
 limit=4104128 if path==PDF and item['sha256']==PDF_SHA else 1048576
 if not 0<=item['bytes']<=limit: raise ValueError('Unregistered source size')
 url='https://raw.githubusercontent.com/geniusdapeng-collab/workroom-tiger/'+spec['commit']+'/'+path
 with urllib.request.urlopen(url,timeout=45) as response: data=response.read(item['bytes']+1)
 if len(data)!=item['bytes'] or hashlib.sha256(data).hexdigest()!=item['sha256']: raise ValueError('Source bytes differ')
 return path,item['mode'],data
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool: files=list(pool.map(fetch,spec['files']))
raw=io.BytesIO()
with tarfile.open(fileobj=raw,mode='w',format=tarfile.USTAR_FORMAT) as archive:
 for path,mode,data in sorted(files):
  item=tarfile.TarInfo(path); item.mode=mode; item.uid=item.gid=item.mtime=0; item.uname=item.gname=''; item.size=len(data); archive.addfile(item,io.BytesIO(data))
out=io.BytesIO()
with gzip.GzipFile(filename='',mode='wb',fileobj=out,mtime=0,compresslevel=9) as stream: stream.write(raw.getvalue())
body=out.getvalue()
if len(body)!=spec['archiveBytes'] or hashlib.sha256(body).hexdigest()!=spec['archiveSha256']: raise ValueError('Archive differs')
path=pathlib.Path('/var/tmp/tiger-pnl-staging')/('tiger-pnl-'+spec['commit']+'.tar.gz')
with path.open('xb') as stream: stream.write(body)
print(json.dumps({'sourceSha':spec['commit'],'archiveSha256':spec['archiveSha256'],'files':len(files),'path':str(path)}))
'''
    return code.replace("SPEC", repr(json.dumps(manifest, separators=(",", ":")))) \
        .replace("PDF_SHA", repr(PDF_SHA)).replace("PDF", repr(PDF))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--commit", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    body, manifest = build(args.repo.resolve(), args.commit)
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "tiger-pnl.tar.gz").write_bytes(body)
    (args.out / "package-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    remote = transport(manifest)
    (args.out / "fetch-package.py").write_text(remote)
    encoded = base64.b64encode(remote.encode()).decode()
    command = "python3 -c \"import base64; exec(base64.b64decode('" + encoded + "'))\""
    (args.out / "fetch-package-command.txt").write_text(command + "\n")
    print(json.dumps({key: manifest[key] for key in ("commit", "archiveSha256", "archiveBytes", "runtimeClosure")}))


if __name__ == "__main__":
    main()
