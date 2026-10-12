"""Store validated single-Agent or collection exports without replacing repo documentation."""

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from agent_package_format import (
    AgentPackageFile,
    AgentPackageLimits,
    agent_package_zip_bytes,
    canonical_agent_package_json,
    parse_agent_package,
    read_agent_package_zip_files,
)

EXPORT_LIMITS = AgentPackageLimits(104857600, 209715200, 2000, 20, 1048576)


def read_agent_export_packages(archive_path: Path):
    if archive_path.stat().st_size > EXPORT_LIMITS.zip_bytes:
        raise ValueError("Agent export exceeds compressed limit")
    files = read_agent_package_zip_files(archive_path.read_bytes(), EXPORT_LIMITS)
    manifests = sorted(
        path
        for path in files
        if len(path.split("/")) == 3
        and path.startswith("agents/")
        and path.endswith("/agent.json")
    )
    if not 1 <= len(manifests) <= 50:
        raise ValueError("Agent export requires between one and 50 Agent manifests")
    packages, covered = [], set()
    for manifest_path in manifests:
        prefix = manifest_path.removesuffix("agent.json")
        payload = {
            path: file for path, file in files.items() if path.startswith(prefix)
        }
        packages.append(
            parse_agent_package(agent_package_zip_bytes(payload), EXPORT_LIMITS)
        )
        covered.update(payload)
    if covered != files.keys():
        raise ValueError("Agent export contains files outside its Agent packages")
    return packages


def check_agent_export_destination(repo: Path, package):
    target = repo / "agents" / package.manifest["package"]["name"]
    for directory in [target, *target.parents]:
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError(
                f"Agent export destination must be a directory without symlinks: {directory}"
            )
    for name in ("agent.json", "instructions.md"):
        path = target / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"Agent export destination must be a regular file: {path}")
    skills = target / "skills"
    if skills.is_symlink() or (skills.exists() and not skills.is_dir()):
        raise ValueError(
            f"Agent export Skill destination must be a directory: {skills}"
        )
    return target


def store_agent_export_packages(repo: Path, packages):
    targets = [check_agent_export_destination(repo, package) for package in packages]
    repo.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".agent-export-", dir=repo) as temp:
        staging = Path(temp)
        for package in packages:
            payload = {
                **package.files,
                package.manifest_path: AgentPackageFile(
                    canonical_agent_package_json(package.manifest)
                ),
            }
            for name, file in payload.items():
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(file.content)
                path.chmod(0o755 if file.executable else 0o644)
        for package, target in zip(packages, targets):
            source = staging / "agents" / package.manifest["package"]["name"]
            target.mkdir(parents=True, exist_ok=True)
            if (target / "skills").exists():
                shutil.rmtree(target / "skills")
            if (source / "skills").exists():
                os.replace(source / "skills", target / "skills")
            for name in ("instructions.md", "agent.json"):
                os.replace(source / name, target / name)
            print(f"Stored {package.manifest['package']['name']}: {package.digest}")


def add_agent_export_command():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    store_agent_export_packages(
        args.repo.absolute(), read_agent_export_packages(args.archive)
    )


if __name__ == "__main__":
    add_agent_export_command()
