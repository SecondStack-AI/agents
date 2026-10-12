"""Portable named-Agent package format, independent of application authority."""

import argparse
import copy
import hashlib
import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path

AGENT_PACKAGE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
AGENT_PACKAGE_HASH = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class AgentPackageLimits:
    zip_bytes: int
    unpacked_bytes: int
    files: int
    skills: int
    manifest_bytes: int


@dataclass(frozen=True)
class AgentPackageFile:
    content: bytes
    executable: bool = False


@dataclass(frozen=True)
class AgentPackage:
    manifest: dict
    files: dict[str, AgentPackageFile]
    manifest_path: str

    @property
    def digest(self) -> str:
        return self.manifest["integrity"]["packageSha256"]

    @property
    def instructions(self) -> str:
        return self.files[
            self.manifest_path.removesuffix("agent.json") + "instructions.md"
        ].content.decode("utf-8")


def canonical_agent_package_json(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def agent_package_digest(manifest: dict) -> str:
    unhashed = copy.deepcopy(manifest)
    unhashed["integrity"].pop("packageSha256", None)
    return hashlib.sha256(canonical_agent_package_json(unhashed)).hexdigest()


def agent_package_file_records(files: dict[str, AgentPackageFile]) -> list[dict]:
    return [
        {
            "path": path,
            "sizeBytes": len(file.content),
            "executable": file.executable,
            "sha256": hashlib.sha256(file.content).hexdigest(),
        }
        for path, file in sorted(files.items())
    ]


def agent_package_tree_digest(files: dict[str, AgentPackageFile]) -> str:
    return hashlib.sha256(
        canonical_agent_package_json({"files": agent_package_file_records(files)})
    ).hexdigest()


def agent_package_owned_skill_path(package_name: str, skill_name: str) -> str:
    return f"agents/{package_name}/skills/{skill_name}"


def agent_package_safe_path(path: str) -> str:
    if (
        not path
        or "\\" in path
        or "\x00" in path
        or ":" in path
        or path.startswith("/")
    ):
        raise ValueError(f"Agent package unsafe path: {path}")
    if any(part in {"", ".", ".."} for part in path.rstrip("/").split("/")):
        raise ValueError(f"Agent package unsafe path: {path}")
    return path.rstrip("/")


def _exact_package_fields(
    value, required: set[str], optional: set[str] = frozenset()
) -> dict:
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - optional
    ):
        raise ValueError(
            f"Agent package invalid fields; required: {', '.join(sorted(required))}"
        )
    return value


def _package_text(value, name: str, maximum: int, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value.strip())
    ):
        raise ValueError(f"Agent package invalid {name}")
    return value


def validate_agent_package_manifest(
    manifest: dict, limits: AgentPackageLimits, *, git_sources: bool = False
) -> None:
    _exact_package_fields(
        manifest,
        {
            "schemaVersion",
            "package",
            "agent",
            "profile",
            "ownedSkills",
            "references",
            "requirements",
            "integrity",
        },
    )
    if type(manifest["schemaVersion"]) is not int or manifest["schemaVersion"] != 1:
        raise ValueError("Agent package unsupported schema version")
    package = _exact_package_fields(manifest["package"], {"name"}, {"version"})
    if not AGENT_PACKAGE_NAME.fullmatch(
        _package_text(package["name"], "package name", 100)
    ):
        raise ValueError("Agent package name must be kebab-case")
    if "version" in package:
        _package_text(package["version"], "package version", 100)
    agent = _exact_package_fields(manifest["agent"], {"name"}, {"summary"})
    _package_text(agent["name"], "Agent name", 100)
    if "summary" in agent:
        _package_text(agent["summary"], "Agent summary", 2000, empty=True)
    profile = _exact_package_fields(
        manifest["profile"],
        {
            "modelId",
            "ingress",
            "thinkingLevel",
            "enabledTools",
            "webSearchModelId",
            "skills",
        },
        {"chatProfileKey"},
    )
    _package_text(profile["modelId"], "modelId", 300)
    if not isinstance(profile["ingress"], str) or profile["ingress"] not in {
        "openai_chat",
        "openai_responses",
        "anthropic_messages",
        "google_stream_generate_content",
    }:
        raise ValueError("Agent package invalid model ingress")
    if "chatProfileKey" in profile:
        _package_text(profile["chatProfileKey"], "chatProfileKey", 300)
    if profile["thinkingLevel"] not in [
        None,
        "off",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]:
        raise ValueError("Agent package invalid thinking level")
    if profile["webSearchModelId"] is not None:
        _package_text(profile["webSearchModelId"], "webSearchModelId", 300)
    tools = profile["enabledTools"]
    if (
        not isinstance(tools, list)
        or len(tools) > 100
        or any(not isinstance(tool, str) or not tool for tool in tools)
        or len(set(tools)) != len(tools)
    ):
        raise ValueError("Agent package invalid tool selection")
    owned, references, selections = (
        manifest["ownedSkills"],
        manifest["references"],
        profile["skills"],
    )
    if (
        not isinstance(owned, list)
        or len(owned) > limits.skills
        or not isinstance(references, list)
        or len(references) > 200
        or not isinstance(selections, list)
        or len(selections) > 200
    ):
        raise ValueError("Agent package invalid Skill lists")
    owned_names, shared_names = set(), set()
    for skill in owned:
        _exact_package_fields(skill, {"name", "displayName", "path"})
        name = _package_text(skill["name"], "Skill name", 100)
        paths = {agent_package_owned_skill_path(package["name"], name)}
        if git_sources:
            paths.add(f"skills/{name}")
        if (
            not AGENT_PACKAGE_NAME.fullmatch(name)
            or name in owned_names
            or not isinstance(skill["path"], str)
            or skill["path"] not in paths
        ):
            raise ValueError("Agent package invalid or duplicate owned Skill")
        _package_text(skill["displayName"], "Skill display name", 100)
        owned_names.add(name)
    for reference in references:
        _exact_package_fields(
            reference, {"name"}, {"namespace", "expectedDigest", "notes"}
        )
        name = _package_text(reference["name"], "shared Skill name", 100)
        if (
            not AGENT_PACKAGE_NAME.fullmatch(name)
            or name in shared_names
            or name in owned_names
        ):
            raise ValueError("Agent package ambiguous shared Skill name")
        for key in {"namespace", "notes"} & reference.keys():
            _package_text(reference[key], key, 3000, empty=True)
        if "expectedDigest" in reference and (
            not isinstance(reference["expectedDigest"], str)
            or not AGENT_PACKAGE_HASH.fullmatch(reference["expectedDigest"])
        ):
            raise ValueError("Agent package invalid shared Skill digest")
        shared_names.add(name)
    selected = set()
    for skill in selections:
        _exact_package_fields(skill, {"name", "kind"})
        name, kind = skill["name"], skill["kind"]
        if (
            not isinstance(kind, str)
            or kind not in {"owned", "shared"}
            or not isinstance(name, str)
            or name in selected
            or name not in (owned_names if kind == "owned" else shared_names)
        ):
            raise ValueError("Agent package unresolved or duplicate Skill selection")
        selected.add(name)
    requirements = _exact_package_fields(
        manifest["requirements"], {"builtins", "capabilities"}, {"notes"}
    )
    for key in ("builtins", "capabilities"):
        values = requirements[key]
        if (
            not isinstance(values, list)
            or len(values) > 100
            or any(not isinstance(value, str) or not value for value in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError("Agent package invalid prerequisites")
    if "notes" in requirements:
        _package_text(requirements["notes"], "prerequisite notes", 3000, empty=True)
    integrity = _exact_package_fields(manifest["integrity"], {"files", "packageSha256"})
    if not isinstance(
        integrity["packageSha256"], str
    ) or not AGENT_PACKAGE_HASH.fullmatch(integrity["packageSha256"]):
        raise ValueError("Agent package invalid integrity digest")
    records = integrity["files"]
    if not isinstance(records, list) or len(records) > limits.files:
        raise ValueError("Agent package invalid integrity file list")
    for record in records:
        _exact_package_fields(record, {"path", "sizeBytes", "sha256", "executable"})
        agent_package_safe_path(_package_text(record["path"], "file path", 1000))
        if (
            type(record["sizeBytes"]) is not int
            or record["sizeBytes"] < 0
            or type(record["executable"]) is not bool
            or not isinstance(record["sha256"], str)
            or not AGENT_PACKAGE_HASH.fullmatch(record["sha256"])
        ):
            raise ValueError("Agent package invalid file integrity record")


def canonicalize_agent_package_manifest(manifest: dict) -> dict:
    manifest = copy.deepcopy(manifest)
    for key in ("enabledTools",):
        manifest["profile"][key].sort()
    manifest["profile"]["skills"].sort(key=lambda skill: (skill["kind"], skill["name"]))
    manifest["ownedSkills"].sort(key=lambda skill: skill["name"])
    manifest["references"].sort(key=lambda skill: skill["name"])
    for key in ("builtins", "capabilities"):
        manifest["requirements"][key].sort()
    manifest["integrity"]["files"].sort(key=lambda file: file["path"])
    return manifest


def validate_agent_package_provenance(provenance: dict | None) -> None:
    if provenance is None:
        return
    _exact_package_fields(
        provenance,
        {"sourceDigest", "effectiveDigest", "creationOperationId", "manifest"},
    )
    for key in ("sourceDigest", "effectiveDigest"):
        if not isinstance(provenance[key], str) or not AGENT_PACKAGE_HASH.fullmatch(
            provenance[key]
        ):
            raise ValueError(f"Agent package invalid provenance {key}")
    if provenance["creationOperationId"] is not None:
        _package_text(provenance["creationOperationId"], "creationOperationId", 150)
    manifest = copy.deepcopy(provenance["manifest"])
    if not isinstance(manifest, dict) or "integrity" in manifest:
        raise ValueError("Agent package provenance requires manifest annotations")
    manifest["integrity"] = {"files": [], "packageSha256": "0" * 64}
    validate_agent_package_manifest(
        manifest, AgentPackageLimits(0, 0, 2000, 200, 32768)
    )
    if len(canonical_agent_package_json(provenance)) > 32768:
        raise ValueError("Agent package provenance exceeds 32768 bytes")


def build_agent_package(
    manifest: dict,
    files: dict[str, AgentPackageFile],
    limits: AgentPackageLimits,
    *,
    git_sources: bool = False,
) -> AgentPackage:
    manifest = copy.deepcopy(manifest)
    manifest["integrity"] = {
        "files": agent_package_file_records(files),
        "packageSha256": "0" * 64,
    }
    manifest = canonicalize_agent_package_manifest(manifest)
    manifest["integrity"]["packageSha256"] = agent_package_digest(manifest)
    path = f"agents/{manifest['package']['name']}/agent.json"
    package = AgentPackage(manifest, files, path)
    validate_agent_package_content(package, limits, git_sources=git_sources)
    return package


def validate_agent_package_content(
    package: AgentPackage, limits: AgentPackageLimits, *, git_sources: bool = False
) -> None:
    manifest, files = package.manifest, package.files
    validate_agent_package_manifest(manifest, limits, git_sources=git_sources)
    manifest_bytes = len(canonical_agent_package_json(manifest))
    if (
        manifest_bytes > limits.manifest_bytes
        or len(files) + 1 > limits.files
        or manifest_bytes + sum(len(file.content) for file in files.values())
        > limits.unpacked_bytes
    ):
        raise ValueError("Agent package exceeds content limits")
    if package.manifest_path != f"agents/{manifest['package']['name']}/agent.json":
        raise ValueError("Agent package manifest path does not match its name")
    instruction_path = (
        package.manifest_path.removesuffix("agent.json") + "instructions.md"
    )
    if instruction_path not in files or len(package.instructions) > 32_000:
        raise ValueError(
            "Agent package instructions missing or exceed 32000 characters"
        )
    roots = {skill["path"] + "/" for skill in manifest["ownedSkills"]}
    for path in files:
        agent_package_safe_path(path)
        if path != instruction_path and not any(
            path.startswith(root) for root in roots
        ):
            raise ValueError(f"Agent package unlisted payload: {path}")
    for root in roots:
        if root + "SKILL.md" not in files:
            raise ValueError(f"Agent package Skill missing SKILL.md: {root}")
    if manifest["integrity"]["files"] != agent_package_file_records(files) or manifest[
        "integrity"
    ]["packageSha256"] != agent_package_digest(manifest):
        raise ValueError("Agent package integrity mismatch")
    if manifest != canonicalize_agent_package_manifest(manifest):
        raise ValueError(
            "Agent package set-like lists must use canonical order; run pack"
        )


def agent_package_zip_bytes(files: dict[str, AgentPackageFile]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, file in sorted(files.items()):
            info = zipfile.ZipInfo(agent_package_safe_path(path), (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (
                stat.S_IFREG | (0o755 if file.executable else 0o644)
            ) << 16
            archive.writestr(info, file.content)
    return output.getvalue()


def serialize_agent_package(package: AgentPackage, limits: AgentPackageLimits) -> bytes:
    validate_agent_package_content(package, limits)
    result = agent_package_zip_bytes(
        {
            **package.files,
            package.manifest_path: AgentPackageFile(
                canonical_agent_package_json(package.manifest)
            ),
        }
    )
    if len(result) > limits.zip_bytes:
        raise ValueError("Agent package exceeds compressed limit")
    return result


def read_agent_package_zip_files(
    content: bytes, limits: AgentPackageLimits
) -> dict[str, AgentPackageFile]:
    if len(content) > limits.zip_bytes:
        raise ValueError("Agent package exceeds compressed limit")
    files, paths, total = {}, set(), 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if len(archive.infolist()) > limits.files * 2 + 100:
            raise ValueError("Agent package exceeds entry limit")
        for info in archive.infolist():
            path = agent_package_safe_path(info.filename)
            if path.casefold() in paths:
                raise ValueError(f"Agent package duplicate path: {path}")
            paths.add(path.casefold())
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ValueError(f"Agent package unsupported compression: {path}")
            if (
                info.flag_bits & 1
                or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
                or mode & 0o7000
            ):
                raise ValueError(f"Agent package encrypted or special entry: {path}")
            if info.is_dir():
                if kind == stat.S_IFREG:
                    raise ValueError(f"Agent package invalid directory mode: {path}")
                continue
            if (
                kind == stat.S_IFDIR
                or len(files) >= limits.files
                or info.file_size > limits.unpacked_bytes - total
            ):
                raise ValueError(
                    "Agent package exceeds unpacked limits or has invalid mode"
                )
            with archive.open(info) as source:
                data = source.read(limits.unpacked_bytes - total + 1)
            total += len(data)
            if total > limits.unpacked_bytes:
                raise ValueError("Agent package exceeds unpacked limit")
            files[path] = AgentPackageFile(data, bool(mode & 0o111))
    return files


def _unique_package_json_pairs(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Agent package duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_agent_package(content: bytes, limits: AgentPackageLimits) -> AgentPackage:
    files = read_agent_package_zip_files(content, limits)
    manifests = [
        path for path in files if re.fullmatch(r"agents/[^/]+/agent\.json", path)
    ]
    if len(manifests) != 1:
        raise ValueError("Agent package requires exactly one agents/<name>/agent.json")
    path = manifests[0]
    raw = files.pop(path)
    if raw.executable or len(raw.content) > limits.manifest_bytes:
        raise ValueError("Agent package invalid manifest size or mode")
    manifest = json.loads(raw.content, object_pairs_hook=_unique_package_json_pairs)
    package = AgentPackage(manifest, files, path)
    validate_agent_package_content(package, limits)
    return package


def agent_package_command() -> None:
    parser = argparse.ArgumentParser(
        description="Pack or validate one Git-shaped named-Agent package"
    )
    parser.add_argument("action", choices=["pack", "validate"])
    parser.add_argument("source", type=Path)
    parser.add_argument("--agent", help="Agent directory name within the checkout")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    # Offline schema-1 ceilings match the server template; server deployment limits may be lower.
    limits = AgentPackageLimits(104857600, 209715200, 2000, 20, 1048576)
    if args.action == "validate":
        package = parse_agent_package(args.source.read_bytes(), limits)
    else:
        if (
            not args.agent
            or not AGENT_PACKAGE_NAME.fullmatch(args.agent)
            or not args.output
        ):
            parser.error("pack requires --agent <name> and --output <archive.zip>")
        root = args.source.resolve()
        manifest_path = root / "agents" / args.agent / "agent.json"
        if (
            any(path.is_symlink() for path in [manifest_path, *manifest_path.parents])
            or not manifest_path.is_file()
        ):
            parser.error("manifest must be a regular file without symlink ancestors")
        manifest = json.loads(
            manifest_path.read_bytes(), object_pairs_hook=_unique_package_json_pairs
        )
        if manifest["package"]["name"] != args.agent:
            parser.error("package name must match the selected Agent directory")
        files = {}
        sources = [root / "agents" / args.agent / "instructions.md"]
        for skill in manifest["ownedSkills"]:
            if not AGENT_PACKAGE_NAME.fullmatch(skill["name"]) or skill["path"] not in {
                agent_package_owned_skill_path(args.agent, skill["name"]),
                f"skills/{skill['name']}",
            }:
                parser.error("unsafe Skill directory")
            skill_root = root / skill["path"]
            if skill_root.is_symlink() or not skill_root.is_dir():
                parser.error("Skill directory must exist without symlinks")
            sources.extend(skill_root.rglob("*"))
        for source in sources:
            if source.is_symlink() or any(
                parent.is_symlink() for parent in source.parents
            ):
                parser.error(f"symlink is forbidden: {source}")
            if source.is_dir():
                continue
            if not source.is_file():
                parser.error(f"not a regular file: {source}")
            if len(
                files
            ) >= limits.files or source.stat().st_size > limits.unpacked_bytes - sum(
                len(file.content) for file in files.values()
            ):
                parser.error("checkout exceeds package limits")
            files[source.relative_to(root).as_posix()] = AgentPackageFile(
                source.read_bytes(), bool(source.stat().st_mode & 0o111)
            )
        source_package = build_agent_package(manifest, files, limits, git_sources=True)
        portable_manifest = copy.deepcopy(source_package.manifest)
        instruction_path = f"agents/{args.agent}/instructions.md"
        portable_files = {instruction_path: files[instruction_path]}
        for skill in portable_manifest["ownedSkills"]:
            source_prefix = skill["path"] + "/"
            skill["path"] = agent_package_owned_skill_path(args.agent, skill["name"])
            portable_files.update(
                {
                    skill["path"] + "/" + path[len(source_prefix) :]: file
                    for path, file in files.items()
                    if path.startswith(source_prefix)
                }
            )
        package = build_agent_package(portable_manifest, portable_files, limits)
        archive = serialize_agent_package(package, limits)
        # Keep explicit Git source locations; the archive has its own normalized integrity.
        manifest_path.write_bytes(canonical_agent_package_json(source_package.manifest))
        args.output.write_bytes(archive)
    print(package.digest)


if __name__ == "__main__":
    agent_package_command()
