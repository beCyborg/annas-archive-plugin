#!/usr/bin/env python3
"""Anna's Archive fast download — official member API /dyn/api/fast_download.json.

Stdlib only; HTTP via curl subprocess (urllib gets 403 from the WAF on some
endpoints). Mirror rotation: $ANNAS_ARCHIVE_MIRROR -> .gl -> .pk -> .gd.
Key: $ANNAS_ARCHIVE_KEY (never printed; scrubbed from error messages).

Usage:
  aa_download.py MD5 [--out DIR] [--name "Author — Title (Year)"]

--name is the target filename WITHOUT extension (extension is taken from the
download URL); defaults to the server-side filename. NOTE: repeated download
of the same md5 DOES spend quota (observed 2026-07; FAQ's 18h-dedup claim did
not hold) — don't re-download needlessly.

Output: JSON to stdout: {saved_path, size_bytes, ext, magic, quota} on success,
{"error": ..., "quota": ...} on failure. Exit codes: 1 args/env, 2 API error,
3 network, 4 file write/verify.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse

MIRRORS = ["https://annas-archive.gl", "https://annas-archive.pk", "https://annas-archive.gd"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

KEY = os.environ.get("ANNAS_ARCHIVE_KEY", "")


def scrub(s):
    return str(s).replace(KEY, "***") if KEY else str(s)


def fail(msg, code, extra=None):
    out = {"error": scrub(msg)}
    if extra:
        out.update(extra)
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(code)


def mirror_list():
    env = os.environ.get("ANNAS_ARCHIVE_MIRROR", "").rstrip("/")
    if env:
        return [env] + [m for m in MIRRORS if m != env]
    return list(MIRRORS)


def api_fast_download(md5):
    qs = urllib.parse.urlencode({"md5": md5, "key": KEY})
    errors = []
    for base in mirror_list():
        proc = subprocess.run(
            ["curl", "-sS", "--compressed", "-L", "--max-time", "30",
             "-A", UA, "-w", "\n%{http_code}", base + "/dyn/api/fast_download.json?" + qs],
            capture_output=True, text=True)
        if proc.returncode != 0:
            errors.append(scrub(f"{base}: curl exit {proc.returncode}: {proc.stderr.strip()[:200]}"))
            continue
        body, _, status = proc.stdout.rpartition("\n")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            errors.append(scrub(f"{base}: HTTP {status}, non-JSON body"))
    fail("all mirrors failed on fast_download.json", 3, {"details": errors})


def sanitize_name(name):
    name = re.sub(r'[/\\:*?"<>|]', " ", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:180] or "book"


def main():
    ap = argparse.ArgumentParser(description="Anna's Archive fast download")
    ap.add_argument("md5")
    ap.add_argument("--out", default=os.path.expanduser("~/Books/AnnasArchive"))
    ap.add_argument("--name", help="target filename without extension")
    args = ap.parse_args()

    if not KEY:
        fail("ANNAS_ARCHIVE_KEY not set", 1)
    md5 = args.md5.lower()
    if not re.fullmatch(r"[0-9a-f]{32}", md5):
        fail("md5 must be 32 hex chars", 1)

    data = api_fast_download(md5)
    quota = data.get("account_fast_download_info")
    if not data.get("download_url"):
        fail(f"API error: {data.get('error') or 'no download_url'}", 2, {"quota": quota})

    url = data["download_url"]
    url_name = os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path))
    ext = os.path.splitext(url_name)[1].lstrip(".").lower() or "bin"
    base_name = sanitize_name(args.name) if args.name else sanitize_name(os.path.splitext(url_name)[0])
    os.makedirs(args.out, exist_ok=True)
    dest = os.path.join(args.out, f"{base_name}.{ext}")

    proc = subprocess.run(
        ["curl", "-sS", "-L", "--max-time", "600", "-A", UA,
         "-o", dest, "-w", "%{http_code}", url],
        capture_output=True, text=True)
    status = proc.stdout.strip()
    if proc.returncode != 0 or status != "200":
        if os.path.exists(dest):
            os.remove(dest)
        fail(f"download failed: HTTP {status or '?'} {proc.stderr.strip()[:200]}", 3, {"quota": quota})

    size = os.path.getsize(dest)
    if size < 1024:
        os.remove(dest)
        fail(f"downloaded file suspiciously small ({size} bytes), removed", 4, {"quota": quota})
    with open(dest, "rb") as f:
        magic = f.read(8)

    print(json.dumps({
        "saved_path": dest,
        "size_bytes": size,
        "ext": ext,
        "magic": magic.decode("latin-1"),
        "quota": quota,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
