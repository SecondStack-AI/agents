"""Exercise the repository commands against real package archives and working directories."""

import io
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from agent_package_format import (
    AgentPackageFile,
    AgentPackageLimits,
    agent_package_zip_bytes,
    build_agent_package,
    canonical_agent_package_json,
    serialize_agent_package,
)

REPO = Path(__file__).resolve().parents[1]
LIMITS = AgentPackageLimits(104857600, 209715200, 2000, 20, 1048576)


class AgentPackageCommandsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agents-tooling-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for directory in ("scripts", "tools"):
            shutil.copytree(REPO / directory, self.repo / directory)

    def run_command(self, script, *arguments, success=True):
        result = subprocess.run(
            [str(self.repo / "scripts" / script), *map(str, arguments)],
            capture_output=True,
            text=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def package(self, name="test-agent", skill=True):
        root = f"agents/{name}"
        skill_root = root + "/skills/test-skill"
        manifest = {
            "schemaVersion": 1,
            "package": {"name": name},
            "agent": {"name": name},
            "profile": {
                "modelId": "test-model",
                "ingress": "openai_responses",
                "thinkingLevel": None,
                "enabledTools": [],
                "webSearchModelId": None,
                "skills": [{"kind": "owned", "name": "test-skill"}] if skill else [],
            },
            "ownedSkills": [
                {"name": "test-skill", "displayName": "Test Skill", "path": skill_root}
            ]
            if skill
            else [],
            "references": [],
            "requirements": {"builtins": [], "capabilities": []},
        }
        files = {
            root + "/instructions.md": AgentPackageFile(
                b"Instructions\nUnicode: \xce\xb1\n"
            )
        }
        if skill:
            files.update(
                {
                    skill_root + "/SKILL.md": AgentPackageFile(
                        b"---\nname: test-skill\ndescription: Fixture\n---\nUse the helper.\n"
                    ),
                    skill_root + "/scripts/helper.sh": AgentPackageFile(
                        b"#!/bin/sh\nprintf ok\n", True
                    ),
                }
            )
        return build_agent_package(manifest, files, LIMITS)

    def archive(self, *packages):
        files = {}
        for package in packages:
            files.update(package.files)
            files[package.manifest_path] = AgentPackageFile(
                canonical_agent_package_json(package.manifest)
            )
        path = self.root / "export.zip"
        path.write_bytes(agent_package_zip_bytes(files))
        return path

    def test_single_export_exact_round_trip_and_documentation_preserved(self):
        package = self.package()
        archive = self.archive(package)
        target = self.repo / "agents/test-agent"
        (target / "eval").mkdir(parents=True)
        (target / "README.md").write_text("Curated documentation")
        (target / "eval/prompts.md").write_text("Curated evaluations")
        self.run_command("add-export.sh", archive)
        self.run_command("package-agent.sh", "test-agent")
        self.assertEqual(
            (self.repo / "dist/test-agent.zip").read_bytes(), archive.read_bytes()
        )
        self.assertEqual((target / "README.md").read_text(), "Curated documentation")
        self.assertEqual(
            (target / "eval/prompts.md").read_text(), "Curated evaluations"
        )
        self.assertTrue(
            (target / "skills/test-skill/scripts/helper.sh").stat().st_mode
            & stat.S_IXUSR
        )

    def test_replacement_removes_stale_skill_files_and_omitted_skills(self):
        self.run_command("add-export.sh", self.archive(self.package()))
        target = self.repo / "agents/test-agent"
        stale = target / "skills/test-skill/obsolete.txt"
        stale.write_text("Removed by next export")
        self.run_command("add-export.sh", self.archive(self.package()))
        self.assertFalse(stale.exists())
        self.run_command("add-export.sh", self.archive(self.package(skill=False)))
        self.assertFalse((target / "skills").exists())
        self.run_command("package-agent.sh", "test-agent")

    def test_collection_export_keeps_same_name_skills_independent(self):
        first, second = self.package("first"), self.package("second")
        self.run_command("add-export.sh", self.archive(first, second))
        for package in (first, second):
            name = package.manifest["package"]["name"]
            self.run_command("package-agent.sh", name)
            self.assertEqual(
                (self.repo / f"dist/{name}.zip").read_bytes(),
                serialize_agent_package(package, LIMITS),
            )

    def test_invalid_collection_rejected_before_any_destination_changes(self):
        first, second = self.package("first"), self.package("second")
        self.run_command("add-export.sh", self.archive(first))
        target = self.repo / "agents/first/instructions.md"
        target.write_text("Keep this local edit")
        files = {**first.files, **second.files}
        for package in (first, second):
            files[package.manifest_path] = AgentPackageFile(
                canonical_agent_package_json(package.manifest)
            )
        files["agents/second/instructions.md"] = AgentPackageFile(
            b"Tampered instructions"
        )
        archive = self.root / "invalid.zip"
        archive.write_bytes(agent_package_zip_bytes(files))
        self.run_command("add-export.sh", archive, success=False)
        self.assertEqual(target.read_text(), "Keep this local edit")
        self.assertFalse((self.repo / "agents/second").exists())

    def test_unsafe_zip_entries_rejected(self):
        original = self.archive(self.package()).read_bytes()
        for path, mode in (
            ("../escape", stat.S_IFREG | 0o644),
            ("agents/test-agent/skills/link", stat.S_IFLNK | 0o777),
        ):
            with self.subTest(path=path):
                buffer = io.BytesIO(original)
                with zipfile.ZipFile(buffer, "a") as archive:
                    info = zipfile.ZipInfo(path)
                    info.create_system = 3
                    info.external_attr = mode << 16
                    archive.writestr(info, b"../../outside")
                unsafe = self.root / "unsafe.zip"
                unsafe.write_bytes(buffer.getvalue())
                self.run_command("add-export.sh", unsafe, success=False)
                self.assertFalse((self.repo / "agents").exists())

    def test_destination_symlink_refused_before_collection_writes(self):
        (self.repo / "agents").mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "instructions.md").write_text("Keep outside")
        (self.repo / "agents/second").symlink_to(outside, target_is_directory=True)
        self.run_command(
            "add-export.sh",
            self.archive(self.package("first"), self.package("second")),
            success=False,
        )
        self.assertEqual((outside / "instructions.md").read_text(), "Keep outside")
        self.assertFalse((self.repo / "agents/first").exists())

    def test_edited_files_repack_and_explicit_root_skill_sources(self):
        self.run_command("add-export.sh", self.archive(self.package()))
        target = self.repo / "agents/test-agent"
        shared = self.repo / "skills/test-skill"
        shared.parent.mkdir()
        shutil.move(target / "skills/test-skill", shared)
        manifest_path = target / "agent.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["ownedSkills"][0]["path"] = "skills/test-skill"
        manifest_path.write_text(json.dumps(manifest))
        (shared / "scripts/helper.sh").write_text("#!/bin/sh\nprintf edited\n")
        self.run_command("package-agent.sh", "test-agent")
        content = (self.repo / "dist/test-agent.zip").read_bytes()
        self.run_command("package-agent.sh", "test-agent")
        self.assertEqual((self.repo / "dist/test-agent.zip").read_bytes(), content)
        self.assertEqual(
            json.loads(manifest_path.read_text())["ownedSkills"][0]["path"],
            "skills/test-skill",
        )
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertEqual(
                archive.read("agents/test-agent/skills/test-skill/scripts/helper.sh"),
                b"#!/bin/sh\nprintf edited\n",
            )
            self.assertEqual(
                json.loads(archive.read("agents/test-agent/agent.json"))["ownedSkills"][
                    0
                ]["path"],
                "agents/test-agent/skills/test-skill",
            )


if __name__ == "__main__":
    unittest.main()
