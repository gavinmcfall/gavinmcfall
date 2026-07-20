#!/usr/bin/env python3
"""CCM — Claude Conversation Migration.

Migrates Claude Code project data (conversations, memory, session metadata)
from one local repository path to another — and, via pack/unpack, from one
machine to another (e.g. macOS → Windows/PowerShell).

Usage (same machine):
    ccm migrate /path/to/source-repo /path/to/dest-repo [--copy] [--dry-run]
    ccm migrate /old/path /new/path --move-repo   # also relocate the repo itself
    ccm list [--filter PATTERN]
    ccm inspect /path/to/repo

Usage (cross-machine, e.g. macOS → Windows):
    # On the Mac (Terminal):
    python3 ccm.py pack /Users/gavin/my-repo -o my-repo-session.zip

    # Copy the zip to the PC, then on Windows (PowerShell):
    python ccm.py unpack my-repo-session.zip C:\\Users\\gavin\\my-repo

'pack' is read-only: it bundles the repo's ~/.claude/projects/ data, its
resume-history entries, the repo-level .claude/ directory, and CLAUDE.md into
a single portable .zip. 'unpack' installs that bundle under the destination
machine's ~/.claude (%USERPROFILE%\\.claude on Windows), rewriting every path
reference from the old POSIX path to the new local path — including the
JSON-escaped form Windows paths take inside .jsonl files ("C:\\\\Users\\\\...").

Every 'migrate' or 'unpack' takes a targeted backup of the Claude data it is
about to touch first (disable with --no-backup, or --full-backup on migrate
for a whole-tree tar).
"""

import argparse
import json
import os
import re
import shutil
import sys
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath


CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"
HISTORY_FILE = CLAUDE_HOME / "history.jsonl"
BACKUP_ROOT = CLAUDE_HOME / "ccm-backups"

PACK_FORMAT = 1
MANIFEST_NAME = "ccm-manifest.json"

# Ephemeral repo-level .claude/ entries that should never travel
SKIP_REPO_CLAUDE_NAMES = {".worklog.lock", "worklog.md"}
SKIP_REPO_CLAUDE_PREFIXES = (".sid-",)


def encode_path(path: str) -> str:
    """Encode a filesystem path to Claude's project directory name.

    Claude Code replaces EVERY non-alphanumeric character with a hyphen — not
    just '/'. So underscores, dots, spaces, etc. all collapse to '-':

        /home/gavin/my-repo        → -home-gavin-my-repo
        /home/gavin/my_other_repos → -home-gavin-my-other-repos
        C:\\Users\\gavin\\my-repo      → C--Users-gavin-my-repo   (Windows)

    The same rule applies on Windows: the drive colon and every backslash
    each become a hyphen. Getting this wrong is silent and costly: data lands
    in a directory Claude never reads, so the migrated conversation never
    appears in 'claude --resume'.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def decode_path(encoded: str) -> str:
    """Best-effort decode of a Claude project directory name back to a path.

    WARNING: This is ambiguous — /home/gavin/my-repo and /home/gavin/my/repo
    both encode to the same string. Use detect_original_path() when accuracy matters.
    """
    if encoded.startswith("-"):
        return "/" + encoded[1:].replace("-", "/")
    return encoded.replace("-", "/")


def json_escaped(path: str) -> str:
    """A path as it appears INSIDE a JSON string literal.

    POSIX paths are unchanged; Windows paths get their backslashes doubled
    ('C:\\Users\\gavin' → 'C:\\\\Users\\\\gavin'). Raw text replacement in .jsonl
    files must use this form on both sides, otherwise a Windows destination
    path injected verbatim produces invalid JSON escapes and Claude silently
    fails to parse the conversation.
    """
    return json.dumps(path, ensure_ascii=False)[1:-1]


def replace_path_refs(text: str, old_path: str, new_path: str) -> str:
    """Replace repo-path references inside raw JSON/JSONL text, cross-platform."""
    old_esc = json_escaped(old_path)
    new_esc = json_escaped(new_path)
    if old_esc in text:
        text = text.replace(old_esc, new_esc)
    # A Windows-origin path could conceivably appear un-escaped in odd spots;
    # for POSIX paths old_esc == old_path so this branch never double-fires.
    if old_esc != old_path and old_path in text:
        text = text.replace(old_path, new_path)
    return text


def detect_original_path(project_dir: Path) -> str | None:
    """Detect the original repo path by reading the cwd from JSONL files.

    This is reliable because the JSONL files contain the actual working directory
    that was used when the conversation happened.
    """
    # Try sessions-index.json first (fast)
    index_file = project_dir / "sessions-index.json"
    if index_file.exists():
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for entry in data.get("entries", []):
                if "projectPath" in entry:
                    return entry["projectPath"]
        except (json.JSONDecodeError, KeyError):
            pass

    # Fall back to reading first JSONL file
    jsonl_files = sorted(project_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None

    # Read the smallest JSONL file for speed
    smallest = min(jsonl_files, key=lambda f: f.stat().st_size)
    try:
        with open(smallest, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    # Look for cwd in the message
                    if "cwd" in obj:
                        return obj["cwd"]
                    # Check nested in tool calls
                    if isinstance(obj, dict):
                        content = obj.get("message", {}).get("content", [])
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and "cwd" in block:
                                    return block["cwd"]
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    return None


def get_project_dir(repo_path: str) -> Path:
    """Get the ~/.claude/projects/ directory for a given repo path."""
    return PROJECTS_DIR / encode_path(repo_path)


def migrate_history(old_path: str, new_path: str,
                    dry_run: bool = False) -> int:
    """Repoint ~/.claude/history.jsonl entries to the destination project.

    The resume picker groups sessions by the ``project`` field in
    history.jsonl, NOT by the cwd inside the conversation .jsonl. Without this
    step a migrated session stays filed under the old path and never appears in
    ``claude --resume`` at the destination.

    ``ccm migrate`` always moves the WHOLE project, so every history entry whose
    ``project`` exactly equals ``old_path`` was a prompt issued from the repo
    being moved and must follow it. Matching on the project path — rather than
    on session IDs harvested from transcript files — is both complete and safe:
    transcripts are routinely cleaned up while their history entries (and the
    sessions-index) linger, so a session-ID filter silently strands real
    history; and no two repos share an identical absolute path, so exact-path
    matching never touches an unrelated project. Every other line is preserved
    byte-for-byte. Returns the number of lines rewritten.
    """
    if not HISTORY_FILE.exists() or old_path == new_path:
        return 0

    out = []
    changed = 0
    with open(HISTORY_FILE, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                out.append(line)
                continue
            try:
                entry = json.loads(s)
            except json.JSONDecodeError:
                out.append(line)
                continue
            if entry.get("project") == old_path:
                entry["project"] = new_path
                changed += 1
                out.append(json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n")
            else:
                out.append(line)

    if changed > 0 and not dry_run:
        with open(HISTORY_FILE, "w", encoding="utf-8", newline="") as f:
            f.writelines(out)

    return changed


def human_size(nbytes: int) -> str:
    """Format bytes as human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f}{unit}"
        nbytes /= 1024
    return f"{nbytes:.1f}TB"


def dir_size(path: Path) -> int:
    """Calculate total size of a directory."""
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total


def discover_files(project_dir: Path) -> dict:
    """Discover all files in a Claude project directory."""
    result = {
        "jsonl": [],       # .jsonl conversation files
        "session_dirs": [], # UUID directories (subagents, tool-results)
        "memory_dir": None, # memory/ directory path
        "index": None,     # sessions-index.json
        "other": [],       # Anything else (journal.md, CONVERSATION_SUMMARY.md, etc.)
    }

    if not project_dir or not project_dir.exists():
        return result

    for item in sorted(project_dir.iterdir()):
        if item.name == "sessions-index.json":
            result["index"] = item
        elif item.name == "memory" and item.is_dir():
            result["memory_dir"] = item
        elif item.suffix == ".jsonl":
            result["jsonl"].append(item)
        elif item.is_dir():
            result["session_dirs"].append(item)
        else:
            result["other"].append(item)

    return result


def find_source_dirs(source_path: str) -> list[tuple[Path, str]]:
    """Find Claude project directories for a source path.

    Returns a list of (path, match_type) tuples, where match_type is one of:
    "exact", "alternate", "fuzzy".

    Handles the case where the directory was renamed (underscores ↔ hyphens)
    by searching for similar encoded names.
    """
    results = []
    exact = get_project_dir(source_path)
    if exact.exists():
        results.append((exact, "exact"))

    encoded = encode_path(source_path)

    # Try common substitutions (underscore ↔ hyphen confusion from renames)
    candidates = set()
    candidates.add(encoded.replace("_", "-"))
    candidates.add(encoded.replace("-", "_"))
    candidates.discard(encoded)  # Don't re-check exact match

    for candidate in candidates:
        path = PROJECTS_DIR / candidate
        if path.exists():
            results.append((path, "alternate"))

    # Fuzzy: search for dirs containing the last path component
    last_component = Path(source_path).name
    if last_component and PROJECTS_DIR.exists():
        for d in PROJECTS_DIR.iterdir():
            if d.is_dir() and d not in [r[0] for r in results]:
                if last_component in d.name:
                    results.append((d, "fuzzy"))

    return results


def find_best_source_dir(source_path: str) -> Path | None:
    """Find the best Claude project directory for a source path.

    Prefers exact match, then alternate encoding, then fuzzy.
    Among matches of the same type, prefers the one with more data.
    """
    matches = find_source_dirs(source_path)
    if not matches:
        return None

    # Sort by: data volume first (empty dirs lose), then match type
    priority = {"exact": 0, "alternate": 1, "fuzzy": 2}

    def score(item):
        path, match_type = item
        jsonl_count = len(list(path.glob("*.jsonl")))
        has_data = 0 if jsonl_count > 0 else 1  # dirs with data first
        return (has_data, priority[match_type], -jsonl_count)

    matches.sort(key=score)
    return matches[0][0]


def rewrite_jsonl(filepath: Path, old_path: str, new_path: str, dry_run: bool = False) -> int:
    """Rewrite path references in a .jsonl conversation file.

    Uses the JSON-escaped spelling of both paths so a Windows destination
    ('C:\\Users\\...') lands as valid JSON. Returns the number of lines modified.
    """
    modified = 0
    lines = []

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            new_line = replace_path_refs(line, old_path, new_path)
            if new_line != line:
                modified += 1
            lines.append(new_line)

    if modified > 0 and not dry_run:
        with open(filepath, "w", encoding="utf-8", newline="") as f:
            f.writelines(lines)

    return modified


def rewrite_index_entries(data: dict, old_path: str, new_path: str,
                          dst_dir: Path) -> int:
    """Rewrite sessions-index entries in place for a new machine/path.

    projectPath gets the old→new prefix swap; fullPath is REBUILT from the
    destination project dir + the original filename, because across machines
    the ~/.claude prefix itself differs (/Users/gavin/.claude vs
    C:\\Users\\gavin\\.claude) and a substring swap of the encoded dir name
    would leave a stale home prefix behind.
    """
    modified = 0
    for entry in data.get("entries", []):
        changed = False
        if "projectPath" in entry and old_path in entry["projectPath"]:
            entry["projectPath"] = entry["projectPath"].replace(old_path, new_path)
            changed = True
        if "fullPath" in entry:
            # Original fullPath may be POSIX or Windows; take the basename in
            # a separator-agnostic way.
            base = PurePosixPath(entry["fullPath"].replace("\\", "/")).name
            rebuilt = str(dst_dir / base)
            if entry["fullPath"] != rebuilt:
                entry["fullPath"] = rebuilt
                changed = True
        if changed:
            modified += 1
    return modified


def rewrite_sessions_index(filepath: Path, old_path: str, new_path: str,
                           old_encoded: str, new_encoded: str,
                           dry_run: bool = False) -> int:
    """Rewrite the sessions-index.json file with new paths."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    modified = rewrite_index_entries(data, old_path, new_path,
                                     PROJECTS_DIR / new_encoded)

    if modified > 0 and not dry_run:
        with open(filepath, "w", encoding="utf-8", newline="") as f:
            json.dump(data, f, indent=4)

    return modified


def merge_sessions_index(src_index: Path, dst_index: Path, dry_run: bool = False) -> int:
    """Merge source sessions-index.json into destination, avoiding duplicates."""
    with open(src_index, "r", encoding="utf-8") as f:
        src_data = json.load(f)

    if dst_index.exists():
        with open(dst_index, "r", encoding="utf-8") as f:
            dst_data = json.load(f)
    else:
        dst_data = {"version": 1, "entries": []}

    existing_ids = {e["sessionId"] for e in dst_data.get("entries", [])}
    added = 0
    for entry in src_data.get("entries", []):
        if entry.get("sessionId") not in existing_ids:
            dst_data["entries"].append(entry)
            added += 1

    if added > 0 and not dry_run:
        with open(dst_index, "w", encoding="utf-8", newline="") as f:
            json.dump(dst_data, f, indent=4)

    return added


def migrate_project_dir(src_dir: Path, dst_dir: Path,
                        old_path: str, new_path: str,
                        copy_mode: bool = False, dry_run: bool = False,
                        no_rewrite: bool = False) -> dict:
    """Migrate Claude project data between ~/.claude/projects/ directories."""
    old_encoded = src_dir.name
    new_encoded = dst_dir.name

    summary = {
        "conversations": 0,
        "session_dirs": 0,
        "memory_files": 0,
        "index_entries": 0,
        "lines_rewritten": 0,
        "other_files": 0,
        "skipped": 0,
        "errors": [],
    }

    if not src_dir.exists():
        summary["errors"].append(f"Source project directory does not exist: {src_dir}")
        return summary

    if not dry_run:
        dst_dir.mkdir(parents=True, exist_ok=True)

    files = discover_files(src_dir)
    total_jsonl = len(files["jsonl"])

    # 1. Migrate .jsonl conversation files
    for i, jsonl_file in enumerate(files["jsonl"], 1):
        dst_file = dst_dir / jsonl_file.name
        size = human_size(jsonl_file.stat().st_size)

        if dst_file.exists():
            print(f"  [{i}/{total_jsonl}] SKIP {jsonl_file.stem[:8]}… ({size}) — exists in dest")
            summary["skipped"] += 1
            continue

        print(f"  [{i}/{total_jsonl}] {'COPY' if copy_mode else 'MOVE'} {jsonl_file.stem[:8]}… ({size})", end="", flush=True)

        if not dry_run:
            if copy_mode:
                shutil.copy2(jsonl_file, dst_file)
            else:
                shutil.move(str(jsonl_file), str(dst_file))

        # Rewrite paths
        if not no_rewrite and old_path != new_path:
            target = dst_file if not dry_run else jsonl_file
            count = rewrite_jsonl(target, old_path, new_path, dry_run=dry_run)
            summary["lines_rewritten"] += count
            if count > 0:
                print(f" → {count} lines rewritten", end="")

        print()
        summary["conversations"] += 1

    # 2. Migrate session UUID directories
    total_dirs = len(files["session_dirs"])
    for i, session_dir in enumerate(files["session_dirs"], 1):
        dst_session = dst_dir / session_dir.name

        if dst_session.exists():
            summary["skipped"] += 1
            continue

        if not dry_run:
            if copy_mode:
                shutil.copytree(session_dir, dst_session)
            else:
                shutil.move(str(session_dir), str(dst_session))

        summary["session_dirs"] += 1

    if total_dirs > 0:
        print(f"  {summary['session_dirs']} session dirs migrated" +
              (f", {summary['skipped']} skipped" if summary["skipped"] else ""))

    # 3. Migrate memory directory
    if files["memory_dir"]:
        src_memory = files["memory_dir"]
        dst_memory = dst_dir / "memory"
        mem_files = list(src_memory.rglob("*"))
        mem_count = len([f for f in mem_files if f.is_file()])

        if dst_memory.exists():
            # Merge: copy individual files that don't exist
            merged = 0
            for f in mem_files:
                if f.is_file():
                    rel = f.relative_to(src_memory)
                    dst_f = dst_memory / rel
                    if not dst_f.exists():
                        if not dry_run:
                            dst_f.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(f, dst_f)
                        merged += 1
            print(f"  MERGE memory/ — {merged} new files added ({mem_count} total in source)")
            summary["memory_files"] = merged
        else:
            print(f"  {'COPY' if copy_mode else 'MOVE'} memory/ ({mem_count} files)")
            if not dry_run:
                if copy_mode:
                    shutil.copytree(src_memory, dst_memory)
                else:
                    shutil.move(str(src_memory), str(dst_memory))
            summary["memory_files"] = mem_count

    # 4. Handle sessions-index.json
    if files["index"]:
        dst_index = dst_dir / "sessions-index.json"

        if dst_index.exists():
            # Merge indexes
            count = merge_sessions_index(files["index"], dst_index, dry_run=dry_run)
            print(f"  MERGE sessions-index.json — {count} new entries added")
            summary["index_entries"] = count

            if not no_rewrite and old_path != new_path:
                rewrite_sessions_index(
                    dst_index, old_path, new_path,
                    old_encoded, new_encoded, dry_run=dry_run
                )
        else:
            print(f"  {'COPY' if copy_mode else 'MOVE'} sessions-index.json")
            if not dry_run:
                if copy_mode:
                    shutil.copy2(files["index"], dst_index)
                else:
                    shutil.move(str(files["index"]), str(dst_index))

            if not no_rewrite and old_path != new_path:
                target = dst_index if not dry_run else files["index"]
                count = rewrite_sessions_index(
                    target, old_path, new_path,
                    old_encoded, new_encoded, dry_run=dry_run
                )
                summary["index_entries"] = count
                if count > 0:
                    print(f"    ↳ {count} index entries rewritten")

    # 5. Other files
    for other in files["other"]:
        dst_other = dst_dir / other.name
        if dst_other.exists():
            print(f"  SKIP {other.name} (exists in dest)")
            continue

        print(f"  {'COPY' if copy_mode else 'MOVE'} {other.name}")
        if not dry_run:
            if copy_mode:
                shutil.copy2(other, dst_other)
            else:
                shutil.move(str(other), str(dst_other))
        summary["other_files"] += 1

    # 6. Clean up empty source directory (move mode only)
    if not copy_mode and not dry_run and src_dir.exists():
        remaining = list(src_dir.iterdir())
        if not remaining:
            src_dir.rmdir()
            print(f"  REMOVED empty source directory")

    return summary


def is_ephemeral_repo_claude(rel_parts: tuple) -> bool:
    """True if a repo .claude/ relative path is ephemeral session state."""
    for part in rel_parts:
        if part in SKIP_REPO_CLAUDE_NAMES:
            return True
        if any(part.startswith(p) for p in SKIP_REPO_CLAUDE_PREFIXES):
            return True
    return False


def migrate_repo_claude_dir(source_path: str, dest_path: str,
                            copy_mode: bool = False, dry_run: bool = False) -> dict:
    """Migrate the .claude/ directory from the source repo root to the dest repo root."""
    src_claude = Path(source_path) / ".claude"
    dst_claude = Path(dest_path) / ".claude"

    summary = {"files": 0, "skipped": []}

    if not src_claude.exists():
        return summary

    for item in sorted(src_claude.iterdir()):
        if is_ephemeral_repo_claude((item.name,)):
            summary["skipped"].append(item.name)
            continue

        dst_item = dst_claude / item.name

        if dst_item.exists():
            print(f"  SKIP {item.name} (exists in dest)")
            summary["skipped"].append(f"{item.name} (exists)")
            continue

        print(f"  {'COPY' if copy_mode else 'MOVE'} .claude/{item.name}")

        if not dry_run:
            dst_claude.mkdir(parents=True, exist_ok=True)
            if item.is_dir():
                shutil.copytree(item, dst_item) if copy_mode else shutil.move(str(item), str(dst_item))
            else:
                shutil.copy2(item, dst_item) if copy_mode else shutil.move(str(item), str(dst_item))

        summary["files"] += 1

    return summary


def migrate_claude_md(source_path: str, dest_path: str,
                      copy_mode: bool = False, dry_run: bool = False) -> bool:
    """Migrate CLAUDE.md from source repo root to dest repo root."""
    src_md = Path(source_path) / "CLAUDE.md"
    dst_md = Path(dest_path) / "CLAUDE.md"

    if not src_md.exists():
        return False

    if dst_md.exists():
        print(f"  SKIP CLAUDE.md (exists in dest)")
        return False

    print(f"  {'COPY' if copy_mode else 'MOVE'} CLAUDE.md")
    if not dry_run:
        shutil.copy2(src_md, dst_md) if copy_mode else shutil.move(str(src_md), str(dst_md))

    return True


def fresh_backup_dir(dry_run: bool = False) -> Path:
    """Pick a timestamped backup dir, dodging same-second collisions."""
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_dir = BACKUP_ROOT / ts
    if not dry_run:
        n = 2
        while backup_dir.exists():
            backup_dir = BACKUP_ROOT / f"{ts}-{n}"
            n += 1
    return backup_dir


def create_backup(src_dir: Path | None, source_path: str, dest_path: str,
                  copy_mode: bool, move_repo: bool, full: bool = False,
                  dry_run: bool = False) -> Path | None:
    """Snapshot Claude data before a migration mutates it.

    Targeted (default): copies the source project dir from ~/.claude/projects/,
    a full copy of history.jsonl (rewritten in place by every migration), and
    the repo's .claude/ into ~/.claude/ccm-backups/<UTC-timestamp>/, plus a
    manifest.txt with the exact restore commands.

    Full: tars the entire ~/.claude tree (minus the backups dir) instead.

    Returns the backup directory, or None if there was nothing to back up.
    """
    backup_dir = fresh_backup_dir(dry_run=dry_run)
    ts = backup_dir.name

    if full:
        archive = backup_dir / "claude-full.tar.gz"
        print(f"Backup (full ~/.claude tarball): {archive}")
        if not dry_run:
            backup_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive, "w:gz") as tar:
                for item in sorted(CLAUDE_HOME.iterdir()):
                    if item == BACKUP_ROOT:  # never recurse into our own backups
                        continue
                    tar.add(item, arcname=item.name)
        return backup_dir

    captured = []

    # 1. Source project dir (the ~/.claude/projects/ data being migrated)
    if src_dir and src_dir.exists():
        dst = backup_dir / "projects" / src_dir.name
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_dir, dst)
        captured.append(f"projects/{src_dir.name}")

    # 2. history.jsonl — a single shared file every migration rewrites in place
    if HISTORY_FILE.exists():
        if not dry_run:
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HISTORY_FILE, backup_dir / "history.jsonl")
        captured.append("history.jsonl")

    # 3. Repo-level .claude/ (pre-move snapshot of the repo's own Claude data)
    repo_claude = Path(source_path) / ".claude"
    if repo_claude.exists():
        if not dry_run:
            shutil.copytree(
                repo_claude, backup_dir / "repo-claude",
                ignore=shutil.ignore_patterns(".worklog.lock", ".sid-*"),
            )
        captured.append("repo-claude/")

    if not captured:
        print("Backup: nothing to back up (no Claude data found for source).")
        return None

    if not dry_run:
        backup_dir.mkdir(parents=True, exist_ok=True)
        proj_name = src_dir.name if src_dir else "<project-dir>"
        manifest = [
            "CCM backup",
            f"created:    {ts}",
            f"source:     {source_path}",
            f"dest:       {dest_path}",
            f"mode:       {'copy' if copy_mode else 'move'}",
            f"move_repo:  {move_repo}",
            f"captured:   {', '.join(captured)}",
            "",
            "Restore project data:",
            f"  cp -a {backup_dir / 'projects' / proj_name} {PROJECTS_DIR}/",
            "Restore resume history:",
            f"  cp {backup_dir / 'history.jsonl'} {HISTORY_FILE}",
        ]
        (backup_dir / "manifest.txt").write_text("\n".join(manifest) + "\n",
                                                 encoding="utf-8")

    print(f"Backup (targeted): {backup_dir}")
    print(f"  captured: {', '.join(captured)}")
    return backup_dir


def move_repo_dir(source_path: str, dest_path: str, dry_run: bool = False) -> bool:
    """Relocate the repository directory tree from source to dest.

    Moves the whole tree including .git, so git history and remotes travel with
    it. shutil.move falls back to copy+delete across filesystems. The dest must
    not already exist (an empty dest is removed first so the move renames
    cleanly rather than nesting source inside it). Returns True on success.
    """
    src = Path(source_path)
    dst = Path(dest_path)

    if not src.exists() or not src.is_dir():
        print(f"Error: source repo is not a directory: {src}")
        return False
    if dst.exists():
        if any(dst.iterdir()):
            print(f"Error: --move-repo target exists and is not empty: {dst}")
            return False
        if not dry_run:
            dst.rmdir()  # empty — drop it so move renames rather than nests

    print("Moving repo directory:")
    print(f"  {src}")
    print(f"  → {dst}")

    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))

    return True


# ── Pack / Unpack (cross-machine, e.g. macOS → Windows) ──────────────────────


def load_history_entries(project_path: str, sessions: set[str]) -> list[dict]:
    """Collect history.jsonl entries belonging to a project.

    When a session filter is given, entries carrying a sessionId that isn't in
    the filter are dropped; entries with no sessionId field are kept, because
    dropping them would strand real resume history on the destination.
    """
    entries = []
    if not HISTORY_FILE.exists():
        return entries
    with open(HISTORY_FILE, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                entry = json.loads(s)
            except json.JSONDecodeError:
                continue
            if entry.get("project") != project_path:
                continue
            if sessions and "sessionId" in entry and entry["sessionId"] not in sessions:
                continue
            entries.append(entry)
    return entries


def cmd_pack(args):
    """Bundle a repo's Claude data into a portable .zip (read-only)."""
    source = str(Path(args.source).resolve())

    if args.source_dir:
        src_dir = PROJECTS_DIR / args.source_dir
        if not src_dir.exists():
            print(f"Error: Specified source directory does not exist: {src_dir}")
            sys.exit(1)
    else:
        src_dir = find_best_source_dir(source)

    repo_claude = Path(source) / ".claude"
    claude_md = Path(source) / "CLAUDE.md"

    if not src_dir and not repo_claude.exists() and not claude_md.exists():
        print(f"Error: No Claude Code data found for {source}")
        print(f"  Checked: {get_project_dir(source)}")
        print(f"  Checked: {repo_claude}")
        matches = find_source_dirs(source)
        if matches:
            print(f"\n  Possible matches found:")
            for m, mt in matches:
                print(f"    {m.name} ({mt})")
        print(f"\nTip: Run 'ccm list' to see all project directories,")
        print(f"     or use --source-dir <name> to specify manually.")
        sys.exit(1)

    # The path to record as "original" — what unpack will rewrite away from.
    if src_dir and src_dir.name != encode_path(source):
        detected = detect_original_path(src_dir)
        old_path = detected or source
        print(f"  Note: Source data found at alternate encoding")
        print(f"    Dir name:      {src_dir.name}")
        print(f"    Original path: {old_path}")
    else:
        old_path = source

    sessions = set(args.session or [])

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    if args.output:
        out_path = Path(args.output).resolve()
    else:
        stem = re.sub(r"[^A-Za-z0-9._-]", "-", Path(source).name) or "project"
        out_path = Path.cwd() / f"ccm-{stem}-{ts}.zip"

    files = discover_files(src_dir)
    jsonl_files = [f for f in files["jsonl"]
                   if not sessions or f.stem in sessions]
    session_dirs = [d for d in files["session_dirs"]
                    if not sessions or d.name in sessions]
    if sessions:
        missing = sessions - {f.stem for f in jsonl_files}
        if missing:
            print(f"Warning: no conversation .jsonl found for session(s): "
                  f"{', '.join(sorted(missing))}")

    history_entries = load_history_entries(old_path, sessions)

    # sessions-index, filtered if a session filter is active
    index_data = None
    if files["index"]:
        with open(files["index"], "r", encoding="utf-8") as f:
            index_data = json.load(f)
        if sessions:
            index_data["entries"] = [
                e for e in index_data.get("entries", [])
                if e.get("sessionId") in sessions
            ]

    repo_claude_files = []
    if repo_claude.exists():
        for f in sorted(repo_claude.rglob("*")):
            if f.is_file() and not is_ephemeral_repo_claude(
                    f.relative_to(repo_claude).parts):
                repo_claude_files.append(f)

    encoded = src_dir.name if src_dir else encode_path(old_path)

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}CCM — pack")
    print(f"{'=' * 60}")
    print(f"  Source:       {source}")
    if old_path != source:
        print(f"  Original:     {old_path}")
    print(f"  Project dir:  {encoded if src_dir else '(none found)'}")
    if sessions:
        print(f"  Sessions:     {', '.join(sorted(sessions))}")
    print(f"  Conversations:{len(jsonl_files):>4}")
    print(f"  Session dirs: {len(session_dirs):>4}")
    print(f"  History:      {len(history_entries):>4} entries")
    print(f"  Repo .claude/:{len(repo_claude_files):>4} files")
    print(f"  CLAUDE.md:    {'yes' if claude_md.exists() else 'no'}")
    print(f"  Output:       {out_path}")
    print()

    if args.dry_run:
        print("Re-run without --dry-run to create the archive.")
        return

    manifest = {
        "format": PACK_FORMAT,
        "created": ts,
        "source_path": old_path,
        "source_input": source,
        "encoded": encoded,
        "platform": sys.platform,
        "home": str(Path.home()),
        "sessions_filter": sorted(sessions),
        "counts": {
            "conversations": len(jsonl_files),
            "session_dirs": len(session_dirs),
            "history_entries": len(history_entries),
            "repo_claude_files": len(repo_claude_files),
            "claude_md": claude_md.exists(),
        },
    }

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST_NAME,
                   json.dumps(manifest, indent=2, ensure_ascii=False))

        if src_dir:
            for f in jsonl_files:
                z.write(f, f"projects/{f.name}")
            for d in session_dirs:
                for f in sorted(d.rglob("*")):
                    if f.is_file():
                        rel = f.relative_to(src_dir).as_posix()
                        z.write(f, f"projects/{rel}")
            if files["memory_dir"]:
                for f in sorted(files["memory_dir"].rglob("*")):
                    if f.is_file():
                        rel = f.relative_to(src_dir).as_posix()
                        z.write(f, f"projects/{rel}")
            for other in files["other"]:
                if other.is_file():
                    z.write(other, f"projects/{other.name}")
            if index_data is not None:
                z.writestr("projects/sessions-index.json",
                           json.dumps(index_data, indent=4, ensure_ascii=False))

        if history_entries:
            lines = "".join(
                json.dumps(e, separators=(",", ":"), ensure_ascii=False) + "\n"
                for e in history_entries
            )
            z.writestr("history.jsonl", lines)

        for f in repo_claude_files:
            rel = f.relative_to(repo_claude).as_posix()
            z.write(f, f"repo-claude/{rel}")

        if claude_md.exists():
            z.write(claude_md, "CLAUDE.md")

    print(f"Packed {human_size(out_path.stat().st_size)} → {out_path}")
    print()
    print("Next, on the destination machine (PowerShell example):")
    print(f"  python ccm.py unpack {out_path.name} C:\\path\\to\\repo")


def safe_zip_rel(name: str, prefix: str) -> Path | None:
    """Relative Path for a zip member under prefix, or None if unsafe/foreign."""
    if not name.startswith(prefix) or name.endswith("/"):
        return None
    rel = PurePosixPath(name[len(prefix):])
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        return None
    return Path(*rel.parts)


def append_history_entries(entries: list[dict], new_path: str,
                           dry_run: bool = False) -> int:
    """Append packed history entries to the local history.jsonl, repointed
    to the destination path and deduplicated against existing entries."""
    existing_keys = set()
    needs_leading_newline = False
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        needs_leading_newline = bool(content) and not content.endswith("\n")
        for line in content.splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                e = json.loads(s)
            except json.JSONDecodeError:
                continue
            existing_keys.add((e.get("project"), e.get("display"),
                               e.get("timestamp"), e.get("sessionId")))

    added = 0
    out_lines = []
    for entry in entries:
        entry = dict(entry)
        entry["project"] = new_path
        key = (entry.get("project"), entry.get("display"),
               entry.get("timestamp"), entry.get("sessionId"))
        if key in existing_keys:
            continue
        existing_keys.add(key)
        out_lines.append(json.dumps(entry, separators=(",", ":"),
                                    ensure_ascii=False) + "\n")
        added += 1

    if added > 0 and not dry_run:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, "a", encoding="utf-8", newline="") as f:
            if needs_leading_newline:
                f.write("\n")
            f.writelines(out_lines)

    return added


def cmd_unpack(args):
    """Install a ccm pack archive under this machine's ~/.claude."""
    archive = Path(args.archive).resolve()
    if not archive.exists():
        print(f"Error: archive not found: {archive}")
        sys.exit(1)

    dest = str(Path(args.dest).resolve())
    dest_p = Path(dest)
    if not dest_p.exists():
        if args.create_dest:
            if not args.dry_run:
                dest_p.mkdir(parents=True, exist_ok=True)
            print(f"Created destination repo directory: {dest}")
        else:
            print(f"Error: Destination repo does not exist: {dest}")
            print(f"  Clone/create the repo there first, or pass --create-dest.")
            sys.exit(1)

    with zipfile.ZipFile(archive) as z:
        try:
            manifest = json.loads(z.read(MANIFEST_NAME).decode("utf-8"))
        except KeyError:
            print(f"Error: {archive.name} is not a ccm pack "
                  f"(missing {MANIFEST_NAME})")
            sys.exit(1)

        if manifest.get("format", 0) > PACK_FORMAT:
            print(f"Error: archive format {manifest['format']} is newer than "
                  f"this ccm understands ({PACK_FORMAT}). Update ccm.py.")
            sys.exit(1)

        old_path = manifest["source_path"]
        new_encoded = encode_path(dest)
        dst_dir = PROJECTS_DIR / new_encoded

        print(f"\n{'[DRY RUN] ' if args.dry_run else ''}CCM — unpack")
        print(f"{'=' * 60}")
        print(f"  Archive:     {archive.name}")
        print(f"  Packed from: {old_path}  ({manifest.get('platform', '?')})")
        print(f"  Dest:        {dest}")
        print(f"  Project dir: {dst_dir}")
        print(f"  Rewrite:     {'no' if args.no_rewrite else 'yes'}")
        print()

        # Backup anything we're about to touch
        if not args.no_backup:
            backup_dir = fresh_backup_dir(dry_run=args.dry_run)
            captured = []
            if not args.dry_run:
                if dst_dir.exists():
                    shutil.copytree(dst_dir, backup_dir / "projects" / dst_dir.name)
                    captured.append(f"projects/{dst_dir.name}")
                if HISTORY_FILE.exists():
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(HISTORY_FILE, backup_dir / "history.jsonl")
                    captured.append("history.jsonl")
            if captured:
                print(f"Backup (targeted): {backup_dir}")
                print(f"  captured: {', '.join(captured)}")
                print()

        names = z.namelist()
        summary = {"conversations": 0, "session_files": 0, "memory_files": 0,
                   "other_files": 0, "lines_rewritten": 0, "index_entries": 0,
                   "history_added": 0, "repo_files": 0, "skipped": 0}

        # 1. Project data (conversations, session dirs, memory, other)
        index_name = None
        for name in names:
            rel = safe_zip_rel(name, "projects/")
            if rel is None:
                continue
            if rel.as_posix() == "sessions-index.json":
                index_name = name
                continue

            target = dst_dir / rel
            if target.exists():
                summary["skipped"] += 1
                continue

            data = z.read(name)

            if len(rel.parts) == 1 and rel.suffix == ".jsonl":
                text = data.decode("utf-8", errors="replace")
                if not args.no_rewrite and old_path != dest:
                    new_text = replace_path_refs(text, old_path, dest)
                    summary["lines_rewritten"] += sum(
                        1 for a, b in zip(text.splitlines(), new_text.splitlines())
                        if a != b)
                    text = new_text
                print(f"  UNPACK {rel.stem[:8]}… ({human_size(len(data))})")
                if not args.dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "w", encoding="utf-8", newline="") as f:
                        f.write(text)
                summary["conversations"] += 1
            else:
                if not args.dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "wb") as f:
                        f.write(data)
                if rel.parts[0] == "memory":
                    summary["memory_files"] += 1
                elif len(rel.parts) > 1:
                    summary["session_files"] += 1
                else:
                    summary["other_files"] += 1

        # 2. sessions-index.json — rewrite for this machine, then merge
        if index_name:
            index_data = json.loads(z.read(index_name).decode("utf-8"))
            if not args.no_rewrite:
                rewrite_index_entries(index_data, old_path, dest, dst_dir)
            dst_index = dst_dir / "sessions-index.json"
            if dst_index.exists():
                with open(dst_index, "r", encoding="utf-8") as f:
                    dst_data = json.load(f)
                existing_ids = {e.get("sessionId")
                                for e in dst_data.get("entries", [])}
                added = 0
                for entry in index_data.get("entries", []):
                    if entry.get("sessionId") not in existing_ids:
                        dst_data["entries"].append(entry)
                        added += 1
                if added and not args.dry_run:
                    with open(dst_index, "w", encoding="utf-8", newline="") as f:
                        json.dump(dst_data, f, indent=4)
                print(f"  MERGE sessions-index.json — {added} new entries")
                summary["index_entries"] = added
            else:
                if not args.dry_run:
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    with open(dst_index, "w", encoding="utf-8", newline="") as f:
                        json.dump(index_data, f, indent=4)
                count = len(index_data.get("entries", []))
                print(f"  UNPACK sessions-index.json ({count} entries)")
                summary["index_entries"] = count

        # 3. Resume history → local history.jsonl, repointed to dest
        if "history.jsonl" in names:
            entries = []
            for line in z.read("history.jsonl").decode(
                    "utf-8", errors="replace").splitlines():
                s = line.strip()
                if not s:
                    continue
                try:
                    entries.append(json.loads(s))
                except json.JSONDecodeError:
                    continue
            summary["history_added"] = append_history_entries(
                entries, dest, dry_run=args.dry_run)
            print(f"  APPEND history.jsonl — {summary['history_added']} entries "
                  f"→ {dest}")

        # 4. Repo-level .claude/ and CLAUDE.md into the destination repo
        if not args.skip_repo_files:
            for name in names:
                rel = safe_zip_rel(name, "repo-claude/")
                if rel is None:
                    continue
                target = dest_p / ".claude" / rel
                if target.exists():
                    summary["skipped"] += 1
                    continue
                print(f"  UNPACK .claude/{rel.as_posix()}")
                if not args.dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "wb") as f:
                        f.write(z.read(name))
                summary["repo_files"] += 1

            if "CLAUDE.md" in names:
                target = dest_p / "CLAUDE.md"
                if target.exists():
                    print(f"  SKIP CLAUDE.md (exists in dest)")
                    summary["skipped"] += 1
                else:
                    print(f"  UNPACK CLAUDE.md")
                    if not args.dry_run:
                        with open(target, "wb") as f:
                            f.write(z.read("CLAUDE.md"))
                    summary["repo_files"] += 1

    print()
    print(f"{'=' * 60}")
    print(f"Summary{'  (DRY RUN — no changes made)' if args.dry_run else ''}:")
    print(f"  Conversations unpacked:  {summary['conversations']}")
    print(f"  Session files:           {summary['session_files']}")
    print(f"  Memory files:            {summary['memory_files']}")
    print(f"  Index entries:           {summary['index_entries']}")
    print(f"  JSONL lines rewritten:   {summary['lines_rewritten']}")
    print(f"  History entries added:   {summary['history_added']}")
    print(f"  Repo files:              {summary['repo_files']}")
    if summary["skipped"]:
        print(f"  Skipped (already exist): {summary['skipped']}")
    print()
    if args.dry_run:
        print("Re-run without --dry-run to execute.")
    else:
        print(f"Done! You can now run 'claude --resume' in {dest}")


# ── Subcommands ──────────────────────────────────────────────────────────────


def cmd_list(args):
    """List all Claude project directories."""
    if not PROJECTS_DIR.exists():
        print("No projects directory found.")
        return

    dirs = sorted(PROJECTS_DIR.iterdir())
    pattern = args.filter.lower() if args.filter else None

    print(f"\nClaude Code project directories ({PROJECTS_DIR}):\n")
    print(f"{'Encoded Name':<55} {'Convos':>6} {'Size':>8} {'Path Exists':>12}")
    print(f"{'─' * 55} {'─' * 6} {'─' * 8} {'─' * 12}")

    for d in dirs:
        if not d.is_dir():
            continue

        name = d.name
        if pattern and pattern not in name.lower():
            continue

        # Count conversations
        jsonl_count = len(list(d.glob("*.jsonl")))
        size = human_size(dir_size(d))

        # Check if the decoded path exists on disk
        decoded = decode_path(name)
        exists = "yes" if Path(decoded).exists() else "ORPHANED"

        # Truncate long names
        display_name = name if len(name) <= 55 else name[:52] + "…"
        print(f"{display_name:<55} {jsonl_count:>6} {size:>8} {exists:>12}")

    print()


def cmd_inspect(args):
    """Show detailed info about a project's Claude data."""
    repo_path = str(Path(args.path).resolve())

    print(f"\nInspecting Claude data for: {repo_path}\n")

    # Check ~/.claude/projects/ — show ALL matches
    matches = find_source_dirs(repo_path)
    exact_dir = get_project_dir(repo_path)

    if matches:
        for src_dir, match_type in matches:
            tag = "" if match_type == "exact" else f" ({match_type} match)"
            files = discover_files(src_dir)
            size = human_size(dir_size(src_dir))
            convos = len(files["jsonl"])

            print(f"  Project dir: {src_dir.name}{tag}")
            print(f"    Size:          {size}")
            print(f"    Conversations: {convos}")
            print(f"    Session dirs:  {len(files['session_dirs'])}")
            print(f"    Memory:        {'yes' if files['memory_dir'] else 'no'}")
            if files["memory_dir"]:
                mem_files = [f for f in files["memory_dir"].rglob("*") if f.is_file()]
                print(f"    Memory files:  {len(mem_files)}")
            print(f"    Sessions idx:  {'yes' if files['index'] else 'no'}")
            if files["other"]:
                print(f"    Other files:   {', '.join(f.name for f in files['other'])}")
            print()

        if len(matches) > 1:
            best = find_best_source_dir(repo_path)
            print(f"  Best match: {best.name}")
            print(f"  Use --source-dir to override if needed.")
            print()
    else:
        print(f"  No project data in ~/.claude/projects/")

    # Check repo .claude/
    repo_claude = Path(repo_path) / ".claude"
    if repo_claude.exists():
        print(f"\n  Repo .claude/ directory:")
        for item in sorted(repo_claude.iterdir()):
            kind = "dir" if item.is_dir() else human_size(item.stat().st_size)
            print(f"    {item.name:<35} {kind}")
    else:
        print(f"\n  No .claude/ directory in repo root")

    # Check CLAUDE.md
    claude_md = Path(repo_path) / "CLAUDE.md"
    if claude_md.exists():
        print(f"\n  CLAUDE.md: {human_size(claude_md.stat().st_size)}")

    print()


def cmd_migrate(args):
    """Migrate Claude project data between repos."""
    source = str(Path(args.source).resolve())
    dest = str(Path(args.dest).resolve())

    if args.move_repo and args.copy:
        print("Error: --move-repo and --copy are incompatible "
              "(--move-repo relocates the repo, which is inherently a move).")
        sys.exit(1)

    # Find source project directory
    if hasattr(args, "source_dir") and args.source_dir:
        # Manual override — use exact directory name
        src_dir = PROJECTS_DIR / args.source_dir
        if not src_dir.exists():
            print(f"Error: Specified source directory does not exist: {src_dir}")
            sys.exit(1)
    else:
        src_dir = find_best_source_dir(source)

    src_repo_claude = Path(source) / ".claude"

    if not src_dir and not src_repo_claude.exists():
        print(f"Error: No Claude Code data found for {source}")
        print(f"  Checked: {get_project_dir(source)}")
        print(f"  Checked: {src_repo_claude}")
        # Show possible matches
        matches = find_source_dirs(source)
        if matches:
            print(f"\n  Possible matches found:")
            for m, mt in matches:
                print(f"    {m.name} ({mt})")
        print(f"\nTip: Run 'ccm list' to see all project directories,")
        print(f"     or 'ccm inspect {source}' for details.")
        print(f"     Use --source-dir <name> to specify manually.")
        sys.exit(1)

    # Destination existence rules depend on whether we're relocating the repo.
    dest_exists = Path(dest).exists()
    if args.move_repo:
        if not Path(source).exists():
            print(f"Error: --move-repo given but source repo does not exist: {source}")
            sys.exit(1)
        if dest_exists and any(Path(dest).iterdir()):
            print(f"Error: --move-repo target exists and is not empty: {dest}")
            sys.exit(1)
    elif not dest_exists:
        print(f"Error: Destination repo does not exist: {dest}")
        print(f"  (pass --move-repo to relocate the source repo to this path)")
        sys.exit(1)

    dst_dir = get_project_dir(dest)

    # Repo-level .claude/ and CLAUDE.md travel *inside* a --move-repo relocation,
    # so the separate repo-file migration steps must not also try to move them.
    skip_repo_files = args.skip_repo_files or args.move_repo

    # Determine the old_path for rewriting
    # If source dir was found at an alternate encoding, detect the original path from data
    if src_dir and src_dir.name != encode_path(source):
        detected = detect_original_path(src_dir)
        old_path = detected or source
        print(f"  Note: Source data found at alternate encoding")
        print(f"    Dir name:      {src_dir.name}")
        print(f"    Original path: {old_path}")
    else:
        old_path = source

    # Show plan
    mode = "COPY" if args.copy else "MOVE"
    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}CCM — Claude Conversation Migration")
    print(f"{'=' * 60}")
    print(f"  Source:      {source}")
    if old_path != source:
        print(f"  Old path:    {old_path}")
    print(f"  Dest:        {dest}")
    print(f"  Mode:        {mode}")
    print(f"  Move repo:   {'yes' if args.move_repo else 'no'}")
    print(f"  Backup:      {'no' if args.no_backup else ('full' if args.full_backup else 'targeted')}")
    print(f"  Rewrite:     {'no' if args.no_rewrite else 'yes'}")
    if src_dir:
        size = human_size(dir_size(src_dir))
        files = discover_files(src_dir)
        print(f"  Data size:   {size}")
        print(f"  Convos:      {len(files['jsonl'])}")
    print()

    # Step 0: Back up everything the migration is about to touch, before any
    # mutation, so a botched run is recoverable.
    backup_dir = None
    if not args.no_backup:
        backup_dir = create_backup(
            src_dir, source, dest,
            copy_mode=args.copy, move_repo=args.move_repo,
            full=args.full_backup, dry_run=args.dry_run,
        )
        print()

    # Step 0b: Relocate the repository directory itself, if requested. Done
    # before the project-data migration so the repo's .claude/ lands at dest
    # under the move; project-dir detection keys off the encoded source *path*,
    # which still resolves after the directory has moved.
    if args.move_repo:
        if not move_repo_dir(source, dest, dry_run=args.dry_run):
            print("Aborting: repo move failed.")
            sys.exit(1)
        print()

    # Step 1: Migrate ~/.claude/projects/ data
    if src_dir:
        print(f"Migrating project data:")
        print(f"  {src_dir.name}/")
        print(f"  → {dst_dir.name}/")
        print()

        t0 = time.time()
        proj_summary = migrate_project_dir(
            src_dir, dst_dir,
            old_path, dest,
            copy_mode=args.copy,
            dry_run=args.dry_run,
            no_rewrite=args.no_rewrite,
        )
        elapsed = time.time() - t0
        print()

        if proj_summary["errors"]:
            for err in proj_summary["errors"]:
                print(f"  ERROR: {err}")
            print()
    else:
        proj_summary = {"conversations": 0, "session_dirs": 0, "memory_files": 0,
                        "index_entries": 0, "lines_rewritten": 0, "other_files": 0,
                        "skipped": 0, "errors": []}
        elapsed = 0

    # Step 1b: Repoint history.jsonl so the resume picker finds the sessions
    # at the destination. This is what makes 'claude --resume' list them.
    history_lines = 0
    if not args.no_rewrite and old_path != dest:
        history_lines = migrate_history(old_path, dest, dry_run=args.dry_run)
        if history_lines > 0:
            print(f"Repointing resume history (~/.claude/history.jsonl):")
            print(f"  {history_lines} entries → {dest}")
            print()

    # Step 2: Migrate repo-level .claude/ and CLAUDE.md
    if not skip_repo_files:
        repo_summary = {"files": 0, "skipped": []}
        claude_md = False

        if src_repo_claude.exists():
            print(f"Migrating repo .claude/ directory:")
            repo_summary = migrate_repo_claude_dir(
                source, dest,
                copy_mode=args.copy,
                dry_run=args.dry_run,
            )
            print()

        src_claude_md = Path(source) / "CLAUDE.md"
        if src_claude_md.exists():
            claude_md = migrate_claude_md(
                source, dest,
                copy_mode=args.copy,
                dry_run=args.dry_run,
            )
            print()
    else:
        repo_summary = {"files": 0, "skipped": []}
        claude_md = False

    # Summary
    print(f"{'=' * 60}")
    print(f"Summary{'  (DRY RUN — no changes made)' if args.dry_run else ''}:")
    print(f"  Conversations migrated:  {proj_summary['conversations']}")
    print(f"  Session dirs migrated:   {proj_summary['session_dirs']}")
    if proj_summary["skipped"]:
        print(f"  Skipped (already exist): {proj_summary['skipped']}")
    print(f"  Memory files migrated:   {proj_summary['memory_files']}")
    print(f"  Index entries:           {proj_summary['index_entries']}")
    print(f"  JSONL lines rewritten:   {proj_summary['lines_rewritten']}")
    print(f"  History entries moved:   {history_lines}")
    if args.move_repo:
        print(f"  Repo directory moved:    {'(dry run) ' if args.dry_run else ''}{source} → {dest}")
    elif not skip_repo_files:
        print(f"  Repo .claude/ files:     {repo_summary['files']}")
        print(f"  CLAUDE.md migrated:      {'yes' if claude_md else 'no'}")
    if backup_dir:
        label = "Backup would be at" if args.dry_run else "Backup saved to"
        print(f"  {label}:       {backup_dir}")
    if elapsed > 1:
        print(f"  Time elapsed:            {elapsed:.1f}s")
    print()

    if not args.dry_run:
        print(f"Done! You can now run 'claude --resume' in {dest}")
    else:
        print(f"Re-run without --dry-run to execute.")


def main():
    # Windows consoles often default to a legacy code page; never let a
    # progress glyph (→, …) crash the run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass

    parser = argparse.ArgumentParser(
        prog="ccm",
        description="CCM — Claude Conversation Migration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ccm list                                              # Show all project dirs
  ccm list --filter fleet                               # Filter by name
  ccm inspect /home/gavin/my-repo                       # Show project details
  ccm migrate /home/gavin/old-repo /home/gavin/new-repo # Move project data
  ccm migrate /old /new --move-repo                     # Also relocate the repo dir
  ccm migrate ... --copy                                # Copy instead of move
  ccm migrate ... --no-backup                           # Skip the pre-move backup
  ccm migrate ... --full-backup                         # Tar all of ~/.claude first
  ccm migrate ... --dry-run                             # Preview without changes

Cross-machine (macOS → Windows/PowerShell):
  # On the Mac:
  python3 ccm.py pack /Users/gavin/my-repo -o my-repo-session.zip
  python3 ccm.py pack /Users/gavin/my-repo --session <SESSION-UUID>  # one session only

  # Copy the zip to the PC, then in PowerShell:
  python ccm.py unpack my-repo-session.zip C:\\Users\\gavin\\my-repo
  python ccm.py unpack my-repo-session.zip C:\\Users\\gavin\\my-repo --dry-run
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # list
    list_parser = subparsers.add_parser("list", aliases=["ls"],
                                        help="List all Claude project directories")
    list_parser.add_argument("--filter", "-f", help="Filter by name pattern")

    # inspect
    inspect_parser = subparsers.add_parser("inspect", aliases=["info"],
                                           help="Show details about a project's Claude data")
    inspect_parser.add_argument("path", help="Repository path to inspect")

    # migrate
    migrate_parser = subparsers.add_parser("migrate", aliases=["mv", "cp"],
                                           help="Migrate project data between repos")
    migrate_parser.add_argument("source", help="Source repository path")
    migrate_parser.add_argument("dest", help="Destination repository path")
    migrate_parser.add_argument("--copy", action="store_true",
                                help="Copy instead of move (keeps source files intact). "
                                     "Note: ~/.claude/history.jsonl is a single shared file "
                                     "and a session can only be filed under one project, so "
                                     "the resume entry is still repointed to the destination — "
                                     "the source location will no longer list it in 'claude --resume'.")
    migrate_parser.add_argument("--dry-run", "-n", action="store_true",
                                help="Show what would be done without making changes")
    migrate_parser.add_argument("--no-rewrite", action="store_true",
                                help="Skip rewriting path references in conversation files")
    migrate_parser.add_argument("--skip-repo-files", action="store_true",
                                help="Skip migrating .claude/ dir and CLAUDE.md from repo root")
    migrate_parser.add_argument("--source-dir", metavar="DIR_NAME",
                                help="Override auto-detected source project dir name "
                                     "(e.g. -home-gavin-old-repo)")
    migrate_parser.add_argument("--move-repo", action="store_true",
                                help="Also relocate the repository directory itself from "
                                     "<source> to <dest> (moves the whole tree incl. .git). "
                                     "<dest> must not exist, or must be empty. Incompatible "
                                     "with --copy. The repo's own .claude/ and CLAUDE.md "
                                     "travel with the move.")
    migrate_parser.add_argument("--no-backup", action="store_true",
                                help="Skip the pre-migration backup of Claude data "
                                     "(not recommended).")
    migrate_parser.add_argument("--full-backup", action="store_true",
                                help="Back up the entire ~/.claude tree as a tarball instead "
                                     "of the default targeted snapshot.")

    # pack
    pack_parser = subparsers.add_parser(
        "pack", aliases=["export"],
        help="Bundle a repo's Claude data into a portable .zip (for another machine)")
    pack_parser.add_argument("source", help="Repository path to pack")
    pack_parser.add_argument("--output", "-o",
                             help="Output .zip path (default: ccm-<repo>-<timestamp>.zip)")
    pack_parser.add_argument("--session", action="append", metavar="SESSION_ID",
                             help="Pack only this session UUID (repeatable). "
                                  "Default: the whole project.")
    pack_parser.add_argument("--source-dir", metavar="DIR_NAME",
                             help="Override auto-detected source project dir name")
    pack_parser.add_argument("--dry-run", "-n", action="store_true",
                             help="Show what would be packed without writing the archive")

    # unpack
    unpack_parser = subparsers.add_parser(
        "unpack", aliases=["import"],
        help="Install a ccm pack .zip under this machine's ~/.claude")
    unpack_parser.add_argument("archive", help="Path to the ccm pack .zip")
    unpack_parser.add_argument("dest",
                               help="Local repository path the sessions should attach to "
                                    "(e.g. C:\\Users\\gavin\\my-repo)")
    unpack_parser.add_argument("--dry-run", "-n", action="store_true",
                               help="Show what would be done without making changes")
    unpack_parser.add_argument("--no-rewrite", action="store_true",
                               help="Skip rewriting path references in conversation files")
    unpack_parser.add_argument("--no-backup", action="store_true",
                               help="Skip backing up existing local Claude data first")
    unpack_parser.add_argument("--skip-repo-files", action="store_true",
                               help="Skip restoring .claude/ dir and CLAUDE.md into the repo")
    unpack_parser.add_argument("--create-dest", action="store_true",
                               help="Create the destination repo directory if it "
                                    "doesn't exist yet")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command in ("list", "ls"):
        cmd_list(args)
    elif args.command in ("inspect", "info"):
        cmd_inspect(args)
    elif args.command in ("migrate", "mv", "cp"):
        if args.command == "cp":
            args.copy = True
        cmd_migrate(args)
    elif args.command in ("pack", "export"):
        cmd_pack(args)
    elif args.command in ("unpack", "import"):
        cmd_unpack(args)


if __name__ == "__main__":
    main()
