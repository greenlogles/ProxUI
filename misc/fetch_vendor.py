#!/usr/bin/env python3
"""Download the third-party UI libraries into static/vendor/ so ProxUI works offline.

Packages come from the npm registry tarballs and are pinned by version and by the
registry's sha512 integrity hash. Output paths mirror the package layout under
static/vendor/<name>@<version>/, so relative imports (noVNC's ES modules,
bootstrap-icons' font files) keep working.

Usage: python3 misc/fetch_vendor.py [--force]
"""

import base64
import hashlib
import io
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parent.parent / "static" / "vendor"
REGISTRY = "https://registry.npmjs.org"

# (name, version, sha512 integrity, paths inside the package to keep)
PACKAGES = [
    (
        "bootstrap",
        "5.1.3",
        "fcQztozJ8jToQWXxVuEyXWW+dSo8AiXWKwiSSrKWsRB/Qt+Ewwza+JWoLKiTuQLaEPhdNAJ7+Dosc9DOIqNy7Q==",
        ["dist/css/bootstrap.min.css", "dist/js/bootstrap.bundle.min.js", "LICENSE"],
    ),
    (
        "bootstrap-icons",
        "1.7.2",
        "NiR2PqC73AQOPdVSu6GJfnk+hN2z6powcistXk1JgPnKuoV2FSdSl26w931Oz9HYbKCcKUSB6ncZTYJAYJl3QQ==",
        ["font/bootstrap-icons.css", "font/fonts/", "LICENSE.md"],
    ),
    (
        "chart.js",
        "3.7.0",
        "31gVuqqKp3lDIFmzpKIrBeum4OpZsQjSIAqlOpgjosHDJZlULtvwLEZKtEhIAZc7JMPaHlYMys40Qy9Mf+1AAg==",
        ["dist/chart.min.js", "LICENSE.md"],
    ),
    (
        "@xterm/xterm",
        "5.5.0",
        "hqJHYaQb5OptNunnyAnkHyM8aCjZ1MEIDTQu1iIbbTD/xops91NB5yq1ZK/dC2JDbVWtF23zUtl9JE2NqwT87A==",
        ["css/xterm.css", "lib/xterm.js", "LICENSE"],
    ),
    (
        "@xterm/addon-fit",
        "0.10.0",
        "UFYkDm4HUahf2lnEyHvio51TNGiLK66mqP2JoATy7hRZeXaGMRDr00JiSF7m63vR5WKATF605yEggJKsw0JpMQ==",
        ["lib/addon-fit.js", "LICENSE"],
    ),
    (
        "@novnc/novnc",
        "1.4.0",
        "kW6ALMc5BuH08e/ond/I1naYcfjc19JYMN1EdtmgjjjzPGCjW8fMtVM3MwM6q7YLRjPlQ3orEvoKMgSS7RkEVQ==",
        ["core/", "vendor/pako/", "LICENSE.txt"],
    ),
    (
        "sortablejs",
        "1.15.0",
        "bv9qgVMjUMf89wAvM6AxVvS/4MX3sPeN0+agqShejLU5z5GX4C75ow1O2e5k4L6XItUyAK3gH6AxSbXrOM5e8w==",
        ["Sortable.min.js", "LICENSE"],
    ),
]


def fetch(name, version, integrity, keep, force):
    dest = VENDOR_DIR / f"{name}@{version}"
    if dest.exists() and not force:
        print(f"  {name}@{version}: present")
        return
    url = f"{REGISTRY}/{name}/-/{name.split('/')[-1]}-{version}.tgz"
    with urllib.request.urlopen(
        url, timeout=60
    ) as resp:  # nosec B310 - fixed https URL
        data = resp.read()
    digest = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if digest != integrity:
        raise SystemExit(f"{name}@{version}: sha512 mismatch (got {digest})")

    if dest.exists():
        shutil.rmtree(dest)
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            rel = member.name.split("/", 1)[1]
            if not any(
                rel == k or (k.endswith("/") and rel.startswith(k)) for k in keep
            ):
                continue
            target = (dest / rel).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise SystemExit(f"{name}@{version}: unsafe path {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())
            count += 1
    print(f"  {name}@{version}: {count} files")


def main():
    force = "--force" in sys.argv[1:]
    print(f"Vendoring UI libraries into {VENDOR_DIR}")
    for name, version, integrity, keep in PACKAGES:
        fetch(name, version, integrity, keep, force)


if __name__ == "__main__":
    main()
