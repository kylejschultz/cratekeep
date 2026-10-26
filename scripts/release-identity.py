"""Validate explicit release tags and supply Docker build identity to CI."""
import os
import re
import subprocess
from pathlib import Path


def identity(ref: str, sha: str) -> tuple[str, list[str]]:
    image = "ghcr.io/kylejschultz/cratekeep"
    version = "development"
    tags = [f"{image}:{sha}", f"{image}:dev"]
    if ref.startswith("refs/tags/"):
        tag = ref.removeprefix("refs/tags/")
        if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
            raise ValueError("Release tags must use vMAJOR.MINOR.PATCH without leading zeros.")
        subprocess.run(["git", "merge-base", "--is-ancestor", sha, "origin/release"], check=True)
        version = tag[1:]
        tags = [f"{image}:{sha}-{version}", f"{image}:{version}", f"{image}:latest"]
    return version, tags


if __name__ == "__main__":
    version, tags = identity(os.environ["REF"], os.environ["SHA"])
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"version={version}\ntags<<CRATEKEEP_TAGS\n")
        output.write("\n".join(tags) + "\nCRATEKEEP_TAGS\n")
