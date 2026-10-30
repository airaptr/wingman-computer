#!/usr/bin/env python3
"""Portable home archives with image identity, credential exclusions and staged restore."""
import argparse
import datetime
import io
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import tarfile
import tempfile
import time

METADATA = ".wingman-snapshot.json"
VERSION = 1
RESERVED = (".wingman-restore-", ".wingman-backup-")


def member_path(name):
    normalized = posixpath.normpath(name)
    if name.startswith("/") or normalized == ".." or normalized.startswith("../"):
        raise ValueError("the archive contains a path outside the home folder")
    if any(part.startswith(RESERVED) for part in normalized.split("/")):
        raise ValueError("the archive contains a reserved restore path")
    return normalized


def excluded(name):
    parts = member_path(name).split("/")
    return (parts[-1] in (METADATA, ".claude.json", ".credentials.json",
                          "SingletonLock", "SingletonCookie", "SingletonSocket")
            or parts[-2:] == ["gh", "hosts.yml"])


def save_archive(destination, home, image, digest):
    home = Path(home)
    destination = Path(destination)
    temporary = destination.with_name(destination.name + ".part")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("a partial snapshot already exists; choose a different output file")

    def select(info):
        if any(part.startswith(RESERVED) for part in info.name.split("/")):
            return None
        if excluded(info.name) or not (info.isfile() or info.isdir() or info.issym() or info.islnk()):
            return None
        if info.issym():
            target = info.linkname
            if target.startswith(str(home) + "/"):
                target = posixpath.relpath(target[len(str(home)) + 1:],
                                          posixpath.dirname(member_path(info.name)) or ".")
            member_path(posixpath.join(posixpath.dirname(member_path(info.name)), target))
            if target.startswith("/"):
                raise ValueError("an external symlink cannot be saved in a portable snapshot")
            info.linkname = target
        elif info.islnk():
            member_path(info.linkname)
        return info

    metadata = json.dumps({
        "version": VERSION,
        "image": image,
        "image_digest": digest,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "excluded_credentials": [".claude.json", ".credentials.json", "gh/hosts.yml"],
    }, indent=2).encode() + b"\n"
    try:
        with tarfile.open(temporary, "x", format=tarfile.PAX_FORMAT) as archive:
            archive.add(home, arcname=".", filter=select)
            info = tarfile.TarInfo("./" + METADATA)
            info.size, info.mode, info.uid, info.gid = len(metadata), 0o600, 1000, 1000
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(metadata))
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def read_metadata(archive):
    matches = [m for m in archive.getmembers() if member_path(m.name) == METADATA]
    if len(matches) != 1 or not matches[0].isfile() or not 0 < matches[0].size <= 16384:
        raise ValueError("the snapshot needs one valid image metadata file")
    with archive.extractfile(matches[0]) as stream:
        metadata = json.load(stream)
    if (not isinstance(metadata, dict) or type(metadata.get("version")) is not int
            or metadata["version"] != VERSION
            or not isinstance(metadata.get("image"), str) or not metadata["image"]
            or not isinstance(metadata.get("image_digest"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", metadata["image_digest"])):
        raise ValueError("the snapshot metadata is invalid or from an unsupported version")
    return metadata


def remove_staging(path, home):
    if path.parent != home or not path.name.startswith(RESERVED) or path.is_symlink():
        raise ValueError("refusing to remove a path not owned by this restore")
    # owner-checked: path is the exact tempfile.mkdtemp() directory on this box's mounted home volume.
    shutil.rmtree(path)


def restore_archive(source, home, digest, force=False, uid=1000, gid=1000):
    home = Path(home)
    with tarfile.open(source, "r:*") as archive:
        metadata = read_metadata(archive)
        if metadata["image_digest"] != digest and not force:
            raise ValueError("the snapshot uses a different image digest; use --force to override it")
        for info in archive.getmembers():
            name = member_path(info.name)
            if not (info.isfile() or info.isdir() or info.issym() or info.islnk()):
                raise ValueError("the snapshot contains a device, socket or other unsupported entry")
            if name != METADATA and excluded(name):
                raise ValueError("the snapshot contains excluded account credentials")
            if info.issym():
                member_path(posixpath.join(posixpath.dirname(name), info.linkname))
                if info.linkname.startswith("/"):
                    raise ValueError("the snapshot contains an absolute symlink")
            elif info.islnk():
                member_path(info.linkname)

        stage = Path(tempfile.mkdtemp(prefix=RESERVED[0], dir=home))
        backup = None
        try:
            archive.extractall(stage, filter="data")
            for root, dirs, files in os.walk(stage):
                for name in dirs + files:
                    os.lchown(Path(root) / name, uid, gid)
            backup = Path(tempfile.mkdtemp(prefix=RESERVED[1], dir=home))
            old, new = [], []
            try:
                for entry in home.iterdir():
                    if entry not in (stage, backup):
                        os.replace(entry, backup / entry.name)
                        old.append(entry.name)
                for entry in stage.iterdir():
                    os.replace(entry, home / entry.name)
                    new.append(entry.name)
            except OSError:
                for name in reversed(new):
                    os.replace(home / name, stage / name)
                for name in reversed(old):
                    os.replace(backup / name, home / name)
                raise
            remove_staging(backup, home)
            backup = None
        finally:
            remove_staging(stage, home)
            if backup is not None and not any(backup.iterdir()):
                remove_staging(backup, home)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verb", choices=("save", "restore"))
    parser.add_argument("archive", type=Path)
    parser.add_argument("image")
    parser.add_argument("digest")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    home = Path("/home/box")
    if not os.path.ismount(home):
        parser.exit(1, "snapshot: the box home volume must be mounted at /home/box\n")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.digest):
        parser.exit(1, "snapshot: the image digest is missing or invalid\n")
    try:
        if args.verb == "save":
            save_archive(args.archive, home, args.image, args.digest)
        else:
            restore_archive(args.archive, home, args.digest, args.force)
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"snapshot: {error}\n")


if __name__ == "__main__":
    main()
