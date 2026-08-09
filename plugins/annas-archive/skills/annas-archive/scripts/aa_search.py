#!/usr/bin/env python3
"""Anna's Archive search — HTML parsing of /search (official API covers only fast_download).

Stdlib only; HTTP via curl subprocess (urllib gets 403 from the WAF on some
endpoints — TLS fingerprinting). Mirror rotation: $ANNAS_ARCHIVE_MIRROR -> .gl -> .pk -> .gd.

Usage:
  aa_search.py "query" [--ext epub] [--lang en] [--content book_nonfiction]
               [--sort newest|oldest|largest|smallest|newest_added|oldest_added]
               [--limit 10] [--page 1] [--exclude-partial]
  aa_search.py "10.1038/nature12373" [--doi]   # DOI: /scidb direct hit, fallback journals index
  aa_search.py --meta MD5                       # metadata from the /md5/<md5> page

Output: JSON to stdout. Search mode -> {"query","mirror","url_params","total_parsed","results":[...]}
where each result is {md5,title,author,publisher,ext,lang,lang_name,size_mb,year,
content,sources,fast_dl,partial,score}. Errors -> {"error": ...} + exit code
(1 args, 2 parse/API, 3 network).
"""
import argparse
import html as htmllib
import json
import os
import re
import subprocess
import sys
import urllib.parse

MIRRORS = ["https://annas-archive.gl", "https://annas-archive.pk", "https://annas-archive.gd"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

KNOWN_EXTS = {"EPUB", "PDF", "MOBI", "AZW3", "AZW", "FB2", "FB2.ZIP", "DJVU",
              "CBR", "CBZ", "TXT", "RTF", "DOC", "DOCX", "LIT", "HTM", "HTML", "ZIP"}
EXT_RANK = {"EPUB": 8, "PDF": 6, "MOBI": 4, "AZW3": 4, "AZW": 4, "FB2": 2, "DJVU": 2}
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def fail(msg, code, details=None):
    out = {"error": msg}
    if details:
        out["details"] = details
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(code)


def mirror_list():
    env = os.environ.get("ANNAS_ARCHIVE_MIRROR", "").rstrip("/")
    if env:
        return [env] + [m for m in MIRRORS if m != env]
    return list(MIRRORS)


def http_get(url, timeout=30):
    """(status, body) via curl; raises RuntimeError on transport failure."""
    proc = subprocess.run(
        ["curl", "-sS", "--compressed", "-L", "--max-time", str(timeout),
         "-A", UA, "-w", "\n%{http_code}", url],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"curl exit {proc.returncode}: {proc.stderr.strip()[:200]}")
    body, _, status = proc.stdout.rpartition("\n")
    return int(status or 0), body


def fetch(path, allow_fail=False):
    """Try mirrors in order; return (body, mirror) or (None, None) if allow_fail."""
    errors = []
    for base in mirror_list():
        try:
            status, body = http_get(base + path)
        except RuntimeError as e:
            errors.append(f"{base}: {e}")
            continue
        if status == 200:
            return body, base
        errors.append(f"{base}: HTTP {status}")
    if allow_fail:
        return None, None
    fail("all mirrors failed", 3, errors)


def strip_tags(s):
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", s)).replace("\xa0", " ").strip()


def parse_meta_line(text):
    """'English [en] · EPUB · 0.1MB · 2013 · 📘 Book (non-fiction) · 🚀/lgli/zlib ·'"""
    out = {"lang": None, "lang_name": None, "ext": None, "size_mb": None,
           "year": None, "content": None, "sources": [], "fast_dl": False}
    parts = [p.strip() for p in htmllib.unescape(text).replace("\xa0", " ").split("·")]
    for p in parts:
        if not p:
            continue
        if "/" in p and not out["sources"]:
            out["fast_dl"] = "🚀" in p
            out["sources"] = [s for s in re.split(r"[/,\s]+", p)
                              if re.fullmatch(r"[a-z0-9_]+", s)]
            continue
        mlang = re.search(r"\[([A-Za-z]{2,3}(?:-[A-Za-z]{2,6})?)\]", p)
        if mlang and out["lang"] is None:
            out["lang"] = mlang.group(1).lower()
            out["lang_name"] = re.sub(r"^[^A-Za-z]+", "", p.split("[")[0]).strip()
            continue
        if p.upper() in KNOWN_EXTS and out["ext"] is None:
            out["ext"] = p.upper()
            continue
        msize = re.fullmatch(r"([\d.]+)\s*MB", p, re.I)
        if msize and out["size_mb"] is None:
            out["size_mb"] = float(msize.group(1))
            continue
        if re.fullmatch(r"\d{4}", p) and out["year"] is None:
            out["year"] = int(p)
            continue
        mcontent = re.search(r"(Book|Journal|Magazine|Comic|Standards?|Audiobook|Musical|Other)[^·]*", p)
        if mcontent and out["content"] is None:
            out["content"] = mcontent.group(0).strip()
    return out


def score(c):
    s = 0.0
    if c["fast_dl"]:
        s += 100
    s += 10 * len(c["sources"])
    s += EXT_RANK.get(c["ext"] or "", 0)
    if c["year"]:
        s += max(0, min(25, c["year"] - 2000))
    if c["size_mb"] is not None and c["size_mb"] >= 0.1:
        s += 5
    if c.get("partial"):
        s -= 50
    return round(s, 1)


def parse_results(page):
    partial_pos = page.find("js-partial-matches-show")
    block_re = re.compile(r'<div class="flex\s+pt-3 pb-3 border-b')
    starts = [m.start() for m in block_re.finditer(page)]
    results = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else min(len(page), start + 30000)
        block = page[start:end]
        mmd5 = re.search(r'href="/md5/([0-9a-f]{32})"', block)
        if not mmd5:
            continue
        c = {"md5": mmd5.group(1), "title": None, "author": None, "publisher": None,
             "partial": partial_pos != -1 and start > partial_pos}
        mtitle = re.search(r'<a href="/md5/[0-9a-f]{32}"[^>]*class="[^"]*text-lg[^"]*"[^>]*>(.*?)</a>', block, re.S)
        if mtitle:
            c["title"] = strip_tags(mtitle.group(1))
        mauthor = re.search(r'icon-\[mdi--user-edit\][^>]*></span>(.*?)</a>', block, re.S)
        if mauthor:
            c["author"] = strip_tags(mauthor.group(1))
        mpub = re.search(r'icon-\[mdi--company\][^>]*></span>(.*?)</a>', block, re.S)
        if mpub:
            c["publisher"] = strip_tags(mpub.group(1))
        mmeta = re.search(r'font-semibold text-sm leading-\[1\.2\] mt-2">(.*?)<', block, re.S)
        c.update(parse_meta_line(mmeta.group(1) if mmeta else ""))
        if c["year"] is None and c["publisher"]:
            myear = re.search(r"\b(19|20)\d{2}\b", c["publisher"])
            if myear:
                c["year"] = int(myear.group(0))
        c["score"] = score(c)
        results.append(c)
    return results


def page_title(page):
    m = re.search(r"<title>(.*?)</title>", page, re.S)
    if not m:
        return None
    return re.sub(r"\s*-\s*Anna.{0,3}s Archive\s*$", "", strip_tags(m.group(1))).strip()


def md5_candidate(md5, allow_fail=False):
    """Candidate dict from the public /md5/<md5> page (fallback metadata source)."""
    page, base = fetch(f"/md5/{md5}", allow_fail=allow_fail)
    if page is None:
        return None
    c = {"md5": md5, "title": page_title(page), "author": None, "publisher": None,
         "partial": False, "mirror": base}
    mmeta = re.search(r">([^<]*\[[A-Za-z]{2,3}\][^<]*·[^<]*)<", page)
    c.update(parse_meta_line(mmeta.group(1) if mmeta else ""))
    if not c["fast_dl"]:
        c["fast_dl"] = "/fast_download/" in page
    # first line of <meta name="description"> is the author on /md5/ pages
    mauthor = re.search(r'<meta name="description" content="([^"\n]*)', page)
    if mauthor and mauthor.group(1).strip():
        c["author"] = htmllib.unescape(mauthor.group(1)).strip()
    c["score"] = score(c)
    return c


def cmd_meta(md5):
    c = md5_candidate(md5)
    print(json.dumps(c, ensure_ascii=False, indent=2))


def cmd_doi(doi, limit):
    # 1) direct /scidb/<doi> — exact hit when the article is in scimag
    page, base = fetch("/scidb/" + urllib.parse.quote(doi, safe="/()"), allow_fail=True)
    if page:
        m = re.search(r'href="/md5/([0-9a-f]{32})"', page)
        if m:
            c = md5_candidate(m.group(1), allow_fail=True)
            if c is None:
                c = {"md5": m.group(1), "title": page_title(page), "fast_dl": None}
            print(json.dumps({"query": doi, "mode": "scidb", "mirror": base,
                              "results": [c]}, ensure_ascii=False, indent=2))
            return
    # 2) fallback: journals index full-text search
    params = {"q": doi, "index": "journals", "page": "1"}
    page, base = fetch("/search?" + urllib.parse.urlencode(params))
    results = parse_results(page)
    results.sort(key=lambda r: r["score"], reverse=True)
    print(json.dumps({"query": doi, "mode": "journals_search", "mirror": base,
                      "url_params": params, "total_parsed": len(results),
                      "results": results[:limit]}, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description="Anna's Archive search (HTML)")
    ap.add_argument("query", nargs="?", help="search query (title author / DOI)")
    ap.add_argument("--meta", metavar="MD5", help="fetch metadata for one md5 instead of searching")
    ap.add_argument("--doi", action="store_true", help="treat query as DOI (auto-detected too)")
    ap.add_argument("--ext", help="extension filter, e.g. epub")
    ap.add_argument("--lang", help="language filter, e.g. en")
    ap.add_argument("--content", help="book_nonfiction | book_fiction | journal_article | book_unknown")
    ap.add_argument("--sort", default="", help="newest|oldest|largest|smallest|newest_added|oldest_added (default: relevance)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--exclude-partial", action="store_true", help="drop partial matches from output")
    args = ap.parse_args()

    if args.meta:
        if not re.fullmatch(r"[0-9a-f]{32}", args.meta.lower()):
            fail("--meta expects a 32-char hex md5", 1)
        cmd_meta(args.meta.lower())
        return
    if not args.query:
        fail("query is required (or use --meta MD5)", 1)
    if args.doi or DOI_RE.match(args.query.strip()):
        cmd_doi(args.query.strip(), args.limit)
        return

    params = {"q": args.query, "page": str(args.page)}
    for k in ("ext", "lang", "content", "sort"):
        v = getattr(args, k)
        if v:
            params[k] = v
    page, base = fetch("/search?" + urllib.parse.urlencode(params))
    results = parse_results(page)
    if args.exclude_partial:
        results = [r for r in results if not r["partial"]]
    results.sort(key=lambda r: r["score"], reverse=True)
    print(json.dumps({
        "query": args.query,
        "mirror": base,
        "url_params": params,
        "total_parsed": len(results),
        "results": results[:args.limit],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
