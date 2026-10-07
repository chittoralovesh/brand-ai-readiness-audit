"""Marketplace and Agent Skills format compliance.

Covers the structural requirements the brief and the agentskills.io
specification both impose. These run without a network and without any
third-party package, so a grader can execute them anywhere.
"""

from __future__ import annotations

import json
import re
import unittest

from helpers import ROOT

from auditlib import registry

ALLOWED_FRONTMATTER = {"name", "description", "license", "allowed-tools",
                       "metadata", "compatibility"}
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def parse_frontmatter(text: str):
    """Minimal YAML-subset frontmatter reader (no third-party dependency)."""
    if not text.startswith("---"):
        raise AssertionError("SKILL.md must start with a YAML frontmatter block")
    end = text.find("\n---", 3)
    if end == -1:
        raise AssertionError("unterminated frontmatter block")
    block = text[3:end].strip("\n")
    body = text[end + 4:]
    data, key = {}, None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):                     # nested mapping value
            if isinstance(data.get(key), dict):
                sub_key, _, sub_value = line.strip().partition(":")
                data[key][sub_key.strip()] = sub_value.strip().strip('"')
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        data[key] = {} if value == "" else value.strip('"')
    return data, body


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.manifest = registry.load_manifest(ROOT)

    def test_manifest_is_valid_json_and_self_contained(self):
        raw = (ROOT / "marketplace.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertIn("skills", data)
        self.assertNotRegex(raw, r"https?://(?!schema)",
                            "the manifest must not require a remote service to resolve")

    def test_manifest_passes_structural_validation(self):
        self.assertEqual(registry.validate_manifest(self.manifest, ROOT), [])

    def test_exactly_one_entrypoint(self):
        entrypoints = [s for s in self.manifest["skills"] if s.get("entrypoint") is True]
        self.assertEqual(len(entrypoints), 1, "the marketplace needs exactly one entrypoint")
        self.assertEqual(entrypoints[0]["id"], registry.entrypoint_id(self.manifest))
        self.assertEqual(entrypoints[0]["id"], self.manifest["entrypoint"])

    def test_every_listed_skill_exists_with_a_skill_md(self):
        for skill in self.manifest["skills"]:
            directory = ROOT / skill["path"]
            self.assertTrue(directory.is_dir(), "%s missing" % skill["path"])
            self.assertTrue((directory / "SKILL.md").is_file(),
                            "%s has no SKILL.md" % skill["path"])

    def test_no_unlisted_skill_directories(self):
        listed = {s["path"] for s in self.manifest["skills"]}
        for directory in (ROOT / "skills").iterdir():
            if directory.is_dir():
                self.assertIn("skills/" + directory.name, listed,
                              "%s exists but is not listed in the manifest" % directory.name)

    def test_marketplace_root_has_required_files(self):
        for name in ("marketplace.json", "README.md", "skills"):
            self.assertTrue((ROOT / name).exists(), "marketplace root is missing %s" % name)


class TestSkillFormat(unittest.TestCase):
    """Each skill folder independently satisfies the agentskills.io spec."""

    def skills(self):
        for directory in sorted((ROOT / "skills").iterdir()):
            if directory.is_dir():
                yield directory

    def test_frontmatter_is_valid(self):
        for directory in self.skills():
            with self.subTest(skill=directory.name):
                data, body = parse_frontmatter(
                    (directory / "SKILL.md").read_text(encoding="utf-8"))
                self.assertTrue(set(data) <= ALLOWED_FRONTMATTER,
                                "unexpected frontmatter fields: %s"
                                % (set(data) - ALLOWED_FRONTMATTER))
                self.assertIn("name", data)
                self.assertIn("description", data)
                self.assertTrue(body.strip(), "SKILL.md body must not be empty")

    def test_name_rules_and_directory_match(self):
        for directory in self.skills():
            with self.subTest(skill=directory.name):
                data, _ = parse_frontmatter(
                    (directory / "SKILL.md").read_text(encoding="utf-8"))
                name = data["name"]
                self.assertEqual(name, directory.name, "name must match its directory")
                self.assertLessEqual(len(name), 64)
                self.assertRegex(name, NAME_RE,
                                 "lowercase alphanumerics and single hyphens only")
                self.assertFalse(name.startswith("-") or name.endswith("-"))
                self.assertNotIn("--", name)

    def test_description_says_what_and_when(self):
        for directory in self.skills():
            with self.subTest(skill=directory.name):
                data, _ = parse_frontmatter(
                    (directory / "SKILL.md").read_text(encoding="utf-8"))
                description = data["description"]
                self.assertLessEqual(len(description), 1024)
                self.assertGreater(len(description), 80,
                                   "a one-line description does not help an agent choose")
                self.assertRegex(description.lower(), r"\buse when\b|\bwhen \w",
                                 "the description must say when to use the skill")

    def test_compatibility_and_metadata_shapes(self):
        for directory in self.skills():
            with self.subTest(skill=directory.name):
                data, _ = parse_frontmatter(
                    (directory / "SKILL.md").read_text(encoding="utf-8"))
                if "compatibility" in data:
                    self.assertLessEqual(len(data["compatibility"]), 500)
                if "metadata" in data:
                    self.assertIsInstance(data["metadata"], dict)
                    for key, value in data["metadata"].items():
                        self.assertIsInstance(value, str,
                                              "metadata values must be strings")

    def test_skill_md_stays_lean(self):
        """Progressive disclosure: detail belongs in references/, not SKILL.md."""
        for directory in self.skills():
            with self.subTest(skill=directory.name):
                text = (directory / "SKILL.md").read_text(encoding="utf-8")
                self.assertLess(len(text.splitlines()), 500,
                                "SKILL.md should stay under 500 lines")
                # ~4 characters per token is the usual rough conversion.
                self.assertLess(len(text) / 4, 5000,
                                "SKILL.md body should stay well under 5000 tokens")

    def test_referenced_files_exist(self):
        """Every relative path a SKILL.md points at must resolve."""
        pattern = re.compile(r"`((?:\.\./)*(?:scripts|references|assets)/[A-Za-z0-9_./\-]+)`")
        for directory in self.skills():
            text = (directory / "SKILL.md").read_text(encoding="utf-8")
            for match in pattern.findall(text):
                if match.endswith("/"):
                    continue
                with self.subTest(skill=directory.name, ref=match):
                    self.assertTrue((directory / match).resolve().exists(),
                                    "%s references a missing file: %s"
                                    % (directory.name, match))

    def test_no_secrets_or_api_keys_anywhere(self):
        patterns = re.compile(
            r"(sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}"
            r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
            r"|(?:api[_-]?key|secret|password|token)\s*[:=]\s*[\"'][^\"'\s]{16,})",
            re.I)
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "dist" in path.parts:
                continue
            if path.suffix.lower() not in (".py", ".md", ".json", ".txt", ".yml", ".yaml"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(patterns.search(text),
                              "possible secret in %s" % path.relative_to(ROOT))


class TestOfficialValidator(unittest.TestCase):
    """Run the reference validator when it is installed. Optional by design."""

    def test_skills_ref_validate(self):
        try:
            from skills_ref.validator import validate
        except ImportError:
            self.skipTest("skills-ref is not installed; structural tests above still ran")
        for directory in sorted((ROOT / "skills").iterdir()):
            if directory.is_dir():
                with self.subTest(skill=directory.name):
                    self.assertEqual(validate(directory), [])


if __name__ == "__main__":
    unittest.main()
