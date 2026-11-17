"""Download the official restic release binary for this machine and verify its SHA-256.

    python3 get_restic.py DEST [VERSION]      # VERSION defaults to the latest release

Used by install.sh --get-restic, for distributions whose restic package is missing or too old.
Prints the installed version.
"""
import bz2
import hashlib
import os
import platform
import sys
import urllib.request

ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "arm",
        "armv6l": "arm", "i686": "386", "i386": "386", "riscv64": "riscv64", "ppc64le": "ppc64le",
        "s390x": "s390x", "mips64": "mips64", "mips64le": "mips64le"}


def fetch(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(), r.geturl()


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    dest = sys.argv[1]
    arch = ARCH.get(platform.machine().lower())
    if not arch:
        sys.exit(f"there is no official restic build for {platform.machine()}")
    if len(sys.argv) == 3:
        tag = "v" + sys.argv[2].lstrip("v")
    else:    # github.com/<repo>/releases/latest redirects to .../tag/vX.Y.Z (no API rate limit)
        _, final = fetch("https://github.com/restic/restic/releases/latest")
        tag = final.rstrip("/").rsplit("/", 1)[1]
    ver = tag.lstrip("v")
    base = f"https://github.com/restic/restic/releases/download/{tag}/"
    name = f"restic_{ver}_linux_{arch}.bz2"
    sums, _ = fetch(base + "SHA256SUMS")
    want = next((line.split()[0] for line in sums.decode().splitlines() if line.split()[-1:] == [name]), None)
    if not want:
        sys.exit(f"{name} is not listed in the {tag} SHA256SUMS")
    data, _ = fetch(base + name, timeout=600)
    if hashlib.sha256(data).hexdigest() != want:
        sys.exit(f"checksum mismatch for {name}; not installing")
    tmp = dest + ".new"
    with open(tmp, "wb") as f:
        f.write(bz2.decompress(data))
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)
    print(ver)


if __name__ == "__main__":
    main()
