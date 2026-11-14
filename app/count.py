#!/usr/bin/env python3
"""Count folders, files and bytes under one or more paths.

Used on both servers so the numbers are comparable: the main server imports it,
the backup server runs it over SSH and prints JSON.
"""

import argparse
import fnmatch
import json
import os
import sys


def is_excluded(rel, name, is_dir, patterns):
    """Approximate rsync's --exclude matching for the common pattern shapes."""
    for raw in patterns:
        pat = raw.strip()
        if not pat or pat.startswith("#"):
            continue
        dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        if dir_only and not is_dir:
            continue
        if pat.startswith("/"):
            if fnmatch.fnmatchcase(rel, pat.lstrip("/")):
                return True
        elif "/" in pat:
            if fnmatch.fnmatchcase(rel, pat) or fnmatch.fnmatchcase(rel, "*/" + pat):
                return True
        elif fnmatch.fnmatchcase(name, pat):
            return True
    return False


def disk_usage(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}


def count_tree(root, patterns=()):
    if not os.path.isdir(root):
        return {"missing": True}
    dirs = files = size = errors = 0
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        try:
            with os.scandir(os.path.join(root, rel_dir)) as entries:
                for entry in entries:
                    rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    if is_excluded(rel, entry.name, is_dir, patterns):
                        continue
                    if is_dir:
                        dirs += 1
                        stack.append(rel)
                        continue
                    files += 1
                    try:
                        size += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        errors += 1
        except OSError:
            errors += 1
    return {"dirs": dirs, "files": files, "bytes": size, "errors": errors, "disk": disk_usage(root)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exclude", action="append", default=[], help="rsync-style exclude pattern")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    result = {path: count_tree(path, args.exclude) for path in args.paths}
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
