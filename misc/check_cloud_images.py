#!/usr/bin/env python3
"""Check that every URL in cloud_images.json is still downloadable.

Sends a HEAD request per image (falling back to a 1-byte ranged GET for servers
that reject HEAD) and exits non-zero if any image is unreachable.

Usage: python3 misc/check_cloud_images.py [path/to/cloud_images.json]
"""

import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "cloud_images.json"
TIMEOUT = 30
ATTEMPTS = 3
USER_AGENT = "proxui-cloud-image-check/1.0"


def probe(url, method):
    headers = {"User-Agent": USER_AGENT}
    if method == "GET":
        headers["Range"] = "bytes=0-0"
    req = urllib.request.Request(url, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status


def check(url):
    error = None
    for _ in range(ATTEMPTS):
        for method in ("HEAD", "GET"):
            try:
                status = probe(url, method)
                if 200 <= status < 300:
                    return None
                error = f"HTTP {status}"
            except urllib.error.HTTPError as e:
                # 404/410 is authoritative; other codes (403, 405) may only
                # mean HEAD is unsupported, so fall through to GET.
                if e.code in (404, 410):
                    return f"HTTP {e.code}"
                error = f"HTTP {e.code}"
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
    return error


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FILE
    images = json.loads(path.read_text())

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(
            zip(images, pool.map(check, (i["url"] for i in images.values())))
        )

    failed = {k: v for k, v in results.items() if v}
    for key, image in images.items():
        status = f"FAIL ({failed[key]})" if key in failed else "OK"
        print(f"{status:<30} {key:<35} {image['url']}")

    print(f"\n{len(images) - len(failed)}/{len(images)} images reachable")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
