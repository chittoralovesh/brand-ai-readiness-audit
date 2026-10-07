#!/usr/bin/env python3
"""Validate the marketplace and build the submission ZIP.

    python tools/package_submission.py                 # validate + package
    python tools/package_submission.py --check-only    # validate only
    python tools/package_submission.py --out dist/x.zip

The ZIP contains a single top-level directory named after the marketplace, so it
unpacks to `brand-ai-readiness-audit/` with `marketplace.json` at its root.

Packaging refuses to produce an invalid submission. It fails on:

  * a malformed manifest, a missing skill folder, or a missing SKILL.md;
  * anything other than exactly one entrypoint;
  * a skill whose `name` does not match its directory;
  * frontmatter fields outside the agentskills.io specification;
  * anything that looks like a credential;
  * a development artefact that slipped into the tree;
  * a ZIP at or above the 50 MB submission limit.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "audit-orchestrator" / "scripts"))

from auditlib import registry                                       # noqa: E402

MAX_ZIP_BYTES = 50 * 1024 * 1024
TARGET_ZIP_BYTES = 5 * 1024 * 1024        # our own goal, not a hard limit

# Directories and files that must never reach the submission.
EXCLUDE_DIRS = {
    ".git", ".github", ".idea", ".vscode", ".claude", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules", ".venv", "venv", "env", "dist",
    "build", ".tox", ".eggs", "htmlcov", ".cache", "site-packages", ".ipynb_checkpoints",
}
EXCLUDE_GLOBS = (
    "*.pyc", "*.pyo", "*.pyd", "*.so", "*.dylib", "*.dll", "*.exe", "*.log",
    "*.tmp", "*.swp", "*.orig", "*.rej", "*.bak", ".DS_Store", "Thumbs.db",
    "*.zip", "*.tar", "*.tar.gz", "*.whl", "*.egg-info",
    # Model weights and other heavyweight artefacts, explicitly forbidden.
    "*.bin", "*.pt", "*.pth", "*.onnx", "*.h5", "*.ckpt", "*.safetensors",
    "*.gguf", "*.npz", "*.parquet", "*.sqlite", "*.db",
    # Local audit output.
    "report.json", "snapshot.json", ".audit-snapshot.json", "*.snap.json",
)
REQUIRED_ROOT = ("marketplace.json", "README.md", "skills")
ALLOWED_FRONTMATTER = {"name", "description", "license", "allowed-tools",
                       "metadata", "compatibility"}
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?:api[_-]?key|secret|password|token)\s*[:=]\s*[\"'][^\"'\s]{16,})", re.I)
TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".yml", ".yaml", ".cfg", ".toml"}


def excluded(path: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return True
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in EXCLUDE_GLOBS)


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and not excluded(path.relative_to(root)):
            yield path


def read_frontmatter(path: Path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None, "does not start with a YAML frontmatter block"
    end = text.find("\n---", 3)
    if end == -1:
        return None, "has an unterminated frontmatter block"
    data, key = {}, None
    for line in text[3:end].strip("\n").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            if isinstance(data.get(key), dict):
                sub, _, value = line.strip().partition(":")
                data[key][sub.strip()] = value.strip().strip('"')
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        data[key] = {} if value == "" else value.strip('"')
    return data, ""


def validate(root: Path) -> list:
    problems = []

    for name in REQUIRED_ROOT:
        if not (root / name).exists():
            problems.append("marketplace root is missing %s" % name)

    try:
        manifest = registry.load_manifest(root)
    except Exception as exc:
        return problems + ["marketplace.json could not be read: %s" % exc]
    problems.extend(registry.validate_manifest(manifest, root))

    for skill in manifest.get("skills", []):
        if not isinstance(skill, dict) or not skill.get("path"):
            continue
        skill_md = root / skill["path"] / "SKILL.md"
        if not skill_md.is_file():
            continue
        data, error = read_frontmatter(skill_md)
        where = skill.get("id") or skill["path"]
        if data is None:
            problems.append("%s/SKILL.md %s" % (where, error))
            continue
        extra = set(data) - ALLOWED_FRONTMATTER
        if extra:
            problems.append("%s/SKILL.md has non-spec frontmatter fields: %s"
                            % (where, ", ".join(sorted(extra))))
        name = data.get("name", "")
        if not NAME_RE.match(name or ""):
            problems.append("%s/SKILL.md name %r breaks the naming rules" % (where, name))
        if name != Path(skill["path"]).name:
            problems.append("%s/SKILL.md name %r does not match its directory"
                            % (where, name))
        description = data.get("description", "")
        if not description or len(description) > 1024:
            problems.append("%s/SKILL.md description must be 1-1024 characters" % where)
        if len(data.get("compatibility", "")) > 500:
            problems.append("%s/SKILL.md compatibility exceeds 500 characters" % where)

    for path in iter_files(root):
        if path.suffix.lower() in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if SECRET_RE.search(text):
                problems.append("possible credential in %s" % path.relative_to(root))
        if path.stat().st_size > 5 * 1024 * 1024:
            problems.append("%s is %d MB - unexpectedly large for this package"
                            % (path.relative_to(root), path.stat().st_size // 1024 // 1024))
    return problems


def build(root: Path, destination: Path, prefix: str) -> tuple:
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = list(iter_files(root))
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, str(Path(prefix) / path.relative_to(root)))
    return files, destination.stat().st_size


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--out", default="")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    print("Validating %s" % root)
    problems = validate(root)
    if problems:
        print("\nFAILED - the marketplace is not submission-ready:")
        for problem in problems:
            print("  x %s" % problem)
        return 1
    print("  ok  manifest, entrypoint, skill folders and frontmatter all valid")
    print("  ok  no credentials or oversized files found")

    if args.check_only:
        return 0

    manifest = registry.load_manifest(root)
    prefix = manifest.get("name") or "brand-ai-readiness-audit"
    destination = Path(args.out) if args.out else root / "dist" / ("%s.zip" % prefix)
    files, size = build(root, destination, prefix)

    print("\nPackaged %d file(s) into %s/" % (len(files), prefix))
    print("  %s" % destination)
    print("  compressed: %.2f MB   (limit 50 MB, target under 5 MB)"
          % (size / 1024 / 1024))
    raw = sum(f.stat().st_size for f in files)
    print("  uncompressed: %.2f MB" % (raw / 1024 / 1024))

    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        for required in ("marketplace.json", "README.md"):
            if "%s/%s" % (prefix, required) not in names:
                print("  x %s missing from the archive" % required)
                return 1
        if archive.testzip() is not None:
            print("  x the archive failed its integrity check")
            return 1
    print("  ok  archive contains marketplace.json and README.md at its root")

    if size >= MAX_ZIP_BYTES:
        print("\nFAILED: %.2f MB is at or above the 50 MB submission limit"
              % (size / 1024 / 1024))
        return 1
    if size >= TARGET_ZIP_BYTES:
        print("  note: above our own 5 MB target, but well inside the 50 MB limit")
    print("\nSubmission ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
