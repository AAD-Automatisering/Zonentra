"""Validate versions, build the integration and prepare repeatable releases."""

import argparse
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("custom_components/zonentra/manifest.json")
PROJECT = Path("pyproject.toml")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def git(*args, root=ROOT):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def version(root=ROOT):
    manifest = json.loads((root / MANIFEST).read_text())
    project = tomllib.loads((root / PROJECT).read_text())
    value = manifest["version"]
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError("The manifest version must be major.minor.patch")
    if project["project"]["version"] != value:
        raise ValueError("manifest.json and pyproject.toml versions differ")
    return value


def bump(part, root=ROOT):
    current = version(root)
    numbers = list(map(int, current.split(".")))
    index = ("major", "minor", "patch").index(part)
    numbers[index] += 1
    numbers[index + 1 :] = [0] * (2 - index)
    value = ".".join(map(str, numbers))
    manifest = json.loads((root / MANIFEST).read_text())
    manifest["version"] = value
    # Change only the project version, preserving the rest of the TOML file.
    text = (root / PROJECT).read_text()
    text, count = re.subn(
        rf'(?m)^version = "{re.escape(current)}"$', f'version = "{value}"', text
    )
    if count != 1:
        raise ValueError("Expected exactly one project version assignment")
    (root / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    (root / PROJECT).write_text(text)
    return value


def build(root=ROOT):
    value = version(root)
    directory = root / MANIFEST.parent
    files = git("ls-files", "-z", "--", str(MANIFEST.parent), root=root).split("\0")
    if str(MANIFEST) not in files:
        raise ValueError("The integration manifest must be tracked in Git")
    output = root / "dist"
    output.mkdir(exist_ok=True)
    with ZipFile(output / "zonentra.zip", "w", compression=ZIP_DEFLATED) as archive:
        for name in sorted(filter(None, files)):
            path = root / name
            if path.is_symlink() or not path.resolve().is_relative_to(
                directory.resolve()
            ):
                raise ValueError(f"Unsupported integration path: {name}")
            info = ZipInfo(path.relative_to(directory).as_posix())
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    metadata = {
        "version": value,
        "commit": git("rev-parse", "HEAD", root=root),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_number": os.environ.get("GITHUB_RUN_NUMBER"),
    }
    (output / "build-info.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return value


def prepare(source, run_id, part="patch", root=ROOT):
    """Push a version commit and tag atomically, or recover this run's commit.

    A newer main causes a skip. Re-running after a successful push must resume
    the same version, including when creating/uploading the release failed.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", source) or not run_id.isdecimal():
        raise ValueError("Invalid source SHA or workflow run ID")
    git("fetch", "origin", "main", "--tags", root=root)
    head = git("rev-parse", "origin/main", root=root)
    source_marker = f"Zonentra-Source: {source}"
    run_marker = f"Zonentra-Run: {run_id}"
    if head != source:
        message = git("log", "-1", "--format=%B", head, root=root).splitlines()
        parents = git("rev-list", "--parents", "-n", "1", head, root=root).split()[1:]
        if (
            parents != [source]
            or source_marker not in message
            or run_marker not in message
        ):
            return None  # Superseded build: a newer push owns the next release.
        git("checkout", "--detach", head, root=root)
        value = version(root)
        if git("rev-parse", f"refs/tags/{value}^{{commit}}", root=root) != head:
            raise ValueError("Release tag does not match the version commit")
        build(root)
        return value
    if git("rev-parse", "HEAD", root=root) != source:
        raise ValueError("Release checkout does not match the tested source")
    value = bump(part, root)
    # Refuse duplicate or decreasing versions rather than overwriting a tag.
    existing = [
        tuple(map(int, tag.removeprefix("v").split(".")))
        for tag in git("tag", root=root).splitlines()
        if VERSION.fullmatch(tag.removeprefix("v"))
    ]
    if existing and tuple(map(int, value.split("."))) <= max(existing):
        raise ValueError("The new version must be higher than existing release tags")
    git("add", "--", str(MANIFEST), str(PROJECT), root=root)
    git("commit", "-m", f"Release {value}\n\n{source_marker}\n{run_marker}", root=root)
    git("tag", "-a", value, "-m", f"Release {value}", root=root)
    build(root)
    # A concurrent push rejects the entire update; no orphan release tag.
    git(
        "push",
        "--atomic",
        "origin",
        "HEAD:refs/heads/main",
        f"refs/tags/{value}",
        root=root,
    )
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "bump", "build", "prepare"))
    parser.add_argument("--part", choices=("patch", "minor", "major"), default="patch")
    parser.add_argument("--source", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    args = parser.parse_args()
    if args.command == "prepare":
        value = prepare(args.source, args.run_id, args.part)
    elif args.command == "bump":
        value = bump(args.part)
    elif args.command == "build":
        value = build()
    else:
        value = version()
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as stream:
            stream.write(
                f"version={value or ''}\npublish={'true' if value else 'false'}\n"
            )
    print(value or "Skipped: main has moved beyond this build")


if __name__ == "__main__":
    main()
