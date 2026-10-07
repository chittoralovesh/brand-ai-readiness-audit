"""Resolve the marketplace manifest and locate sibling skills.

The orchestrator composes the other skills by reading `marketplace.json` - the
same manifest the grading harness reads - rather than by hard-coding paths. That
keeps the manifest the single source of truth about what the marketplace
contains, and means a skill added to (or removed from) the manifest is picked up
without touching orchestrator code.

The manifest is required to be self-contained: no network access, no external
service, no environment variables.
"""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST_NAME = "marketplace.json"


class ManifestError(RuntimeError):
    pass


def find_marketplace_root(start: Path = None) -> Path:
    """Walk upwards from `start` until a directory containing the manifest."""
    here = Path(start or __file__).resolve()
    for candidate in [here] + list(here.parents):
        if candidate.is_dir() and (candidate / MANIFEST_NAME).is_file():
            return candidate
    raise ManifestError(
        "could not locate %s above %s - the orchestrator must run from inside the "
        "marketplace directory" % (MANIFEST_NAME, here))


def load_manifest(root: Path = None) -> dict:
    root = Path(root) if root else find_marketplace_root()
    path = root / MANIFEST_NAME
    with path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    manifest["_root"] = str(root)
    return manifest


def validate_manifest(manifest: dict, root: Path = None) -> list:
    """Structural checks the packaging tool and the test suite both reuse."""
    errors = []
    root = Path(root or manifest.get("_root") or ".")
    if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
        errors.append("manifest `name` must be a non-empty string")
    if not isinstance(manifest.get("version"), str) or not manifest["version"].strip():
        errors.append("manifest `version` must be a non-empty string")
    skills = manifest.get("skills")
    if not isinstance(skills, list) or not skills:
        return errors + ["manifest `skills` must be a non-empty list"]

    seen_ids, entrypoints = set(), []
    for index, skill in enumerate(skills):
        where = "skills[%d]" % index
        if not isinstance(skill, dict):
            errors.append("%s is not an object" % where)
            continue
        skill_id = skill.get("id")
        path = skill.get("path")
        if not isinstance(skill_id, str) or not skill_id.strip():
            errors.append("%s.id must be a non-empty string" % where)
        elif skill_id in seen_ids:
            errors.append("%s.id %r is duplicated" % (where, skill_id))
        else:
            seen_ids.add(skill_id)
        if not isinstance(path, str) or not path.strip():
            errors.append("%s.path must be a non-empty string" % where)
            continue
        if path.startswith("/") or ".." in Path(path).parts:
            errors.append("%s.path must be a relative path inside the marketplace" % where)
            continue
        skill_dir = root / path
        if not skill_dir.is_dir():
            errors.append("%s.path %r does not exist" % (where, path))
            continue
        if not (skill_dir / "SKILL.md").is_file():
            errors.append("%s (%s) has no SKILL.md" % (where, path))
        if skill_id and skill_dir.name != skill_id:
            errors.append("%s.id %r does not match its directory name %r"
                          % (where, skill_id, skill_dir.name))
        if skill.get("entrypoint") is True:
            entrypoints.append(skill_id)

    if len(entrypoints) != 1:
        errors.append("the manifest must mark exactly one skill as the entrypoint; found %d %s"
                      % (len(entrypoints), entrypoints))
    return errors


def skill_paths(manifest: dict) -> dict:
    root = Path(manifest.get("_root") or ".")
    return {skill["id"]: root / skill["path"]
            for skill in manifest.get("skills", [])
            if isinstance(skill, dict) and skill.get("id") and skill.get("path")}


def entrypoint_id(manifest: dict) -> str:
    for skill in manifest.get("skills", []):
        if isinstance(skill, dict) and skill.get("entrypoint") is True:
            return skill.get("id", "")
    return ""
