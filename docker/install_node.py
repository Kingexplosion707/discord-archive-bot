"""Installs the official Node.js build for this CPU into /usr/local (used by TV Tracker)."""
import platform
import sys
import tarfile
import tempfile
import urllib.request

version = sys.argv[1]
arch = {"x86_64": "x64", "aarch64": "arm64"}[platform.machine()]
name = f"node-v{version}-linux-{arch}"
url = f"https://nodejs.org/dist/v{version}/{name}.tar.xz"

with tempfile.NamedTemporaryFile(suffix=".tar.xz") as archive:
    urllib.request.urlretrieve(url, archive.name)
    with tarfile.open(archive.name) as tar:
        members = []
        for member in tar.getmembers():
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1] or parts[1].startswith(("include/", "share/")):
                continue
            member.name = parts[1]
            members.append(member)
        tar.extractall("/usr/local", members=members, filter="tar")
print(f"Installed Node.js {version} ({arch})")
