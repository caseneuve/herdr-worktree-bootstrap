#!/usr/bin/env python3
"""Seed a new Herdr worktree from repository-specific local configuration."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

CONFIG_FILENAME = "config.toml"
EVENT_HOOK_NAME = "worktree.created"
EVENT_WIRE_NAME = "worktree_created"


class BootstrapError(Exception):
    """Raised for invalid plugin configuration or event data."""


@dataclass(frozen=True)
class WorktreeRule:
    repo: Path
    paths: tuple[str, ...]
    git_hooks: bool
    git_submodules: bool
    commands: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class CopyOperation:
    source: Path
    destination: Path
    destination_root: Path


def canonical_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def path_exists(path: Path) -> bool:
    """Return true for normal files, directories, and broken symbolic links."""
    return os.path.lexists(path)


def as_non_empty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BootstrapError(f"{field} must be a non-empty string")
    return value


def parse_paths(value: object, rule_number: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise BootstrapError(f"worktree rule {rule_number}.paths must be an array")
    return tuple(
        as_non_empty_string(item, f"worktree rule {rule_number}.paths[{index}]")
        for index, item in enumerate(value)
    )


def parse_git_hooks(value: object, rule_number: int) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise BootstrapError(f"worktree rule {rule_number}.git-hooks must be a boolean")
    return value


def parse_git_submodules(value: object, rule_number: int) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise BootstrapError(f"worktree rule {rule_number}.git-submodules must be a boolean")
    return value


def parse_commands(value: object, rule_number: int) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise BootstrapError(f"worktree rule {rule_number}.commands must be an array")

    commands: list[tuple[str, ...]] = []
    for command_index, command in enumerate(value):
        if not isinstance(command, list) or not command:
            raise BootstrapError(
                f"worktree rule {rule_number}.commands[{command_index}] "
                "must be a non-empty argv array"
            )
        commands.append(
            tuple(
                as_non_empty_string(
                    argument,
                    f"worktree rule {rule_number}.commands[{command_index}][{argument_index}]",
                )
                for argument_index, argument in enumerate(command)
            )
        )
    return tuple(commands)


def load_rules(config_path: Path) -> tuple[WorktreeRule, ...]:
    try:
        with config_path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BootstrapError(f"could not read {config_path}: {error}") from error

    raw_rules = config.get("worktree", [])
    if not isinstance(raw_rules, list):
        raise BootstrapError("worktree must be an array of tables")

    rules: list[WorktreeRule] = []
    for index, raw_rule in enumerate(raw_rules, start=1):
        if not isinstance(raw_rule, dict):
            raise BootstrapError(f"worktree rule {index} must be a table")
        repo = as_non_empty_string(raw_rule.get("repo"), f"worktree rule {index}.repo")
        rules.append(
            WorktreeRule(
                repo=canonical_path(Path(repo)),
                paths=parse_paths(raw_rule.get("paths"), index),
                git_hooks=parse_git_hooks(raw_rule.get("git-hooks"), index),
                git_submodules=parse_git_submodules(raw_rule.get("git-submodules"), index),
                commands=parse_commands(raw_rule.get("commands"), index),
            )
        )
    return tuple(rules)


def worktree_paths_from_event(event: object) -> tuple[Path, Path]:
    if not isinstance(event, dict) or event.get("event") not in {
        EVENT_HOOK_NAME,
        EVENT_WIRE_NAME,
    }:
        raise BootstrapError("expected a worktree-created event")

    data = event.get("data")
    if not isinstance(data, dict):
        raise BootstrapError("event data is missing")
    if data.get("type") not in {None, EVENT_WIRE_NAME}:
        raise BootstrapError("event data is not a worktree-created event")
    workspace = data.get("workspace")
    worktree = data.get("worktree")
    if not isinstance(workspace, dict) or not isinstance(worktree, dict):
        raise BootstrapError("event does not contain worktree workspace data")
    workspace_worktree = workspace.get("worktree")
    if not isinstance(workspace_worktree, dict):
        raise BootstrapError("event workspace does not contain worktree provenance")

    source_repo = as_non_empty_string(
        workspace_worktree.get("repo_root"), "event workspace.worktree.repo_root"
    )
    destination = as_non_empty_string(worktree.get("path"), "event worktree.path")
    return canonical_path(Path(source_repo)), canonical_path(Path(destination))


def matching_rules(rules: Sequence[WorktreeRule], source_repo: Path) -> tuple[WorktreeRule, ...]:
    source_repo = canonical_path(source_repo)
    return tuple(rule for rule in rules if rule.repo == source_repo)


def copy_operation(
    source_repo: Path, destination_root: Path, configured_path: str
) -> CopyOperation:
    expanded_path = Path(os.path.expanduser(configured_path))
    if configured_path.startswith("~") and not expanded_path.is_absolute():
        raise BootstrapError(f"could not expand home-relative path: {configured_path}")

    if expanded_path.is_absolute():
        if not expanded_path.name:
            raise BootstrapError(
                f"absolute copy path must name a file or directory: {configured_path}"
            )
        source = expanded_path
        destination = destination_root / expanded_path.name
    else:
        if configured_path in {"", "."} or ".." in expanded_path.parts:
            raise BootstrapError(
                f"relative copy path must stay below the repository root: {configured_path}"
            )
        source = source_repo / expanded_path
        destination = destination_root / expanded_path

    destination_root = canonical_path(destination_root)
    destination_parent = canonical_path(destination.parent)
    try:
        destination_parent.relative_to(destination_root)
    except ValueError as error:
        raise BootstrapError(
            f"copy destination escapes the new worktree: {configured_path}"
        ) from error
    return CopyOperation(
        source=source,
        destination=destination,
        destination_root=destination_root,
    )


def copy_path(operation: CopyOperation, log: Callable[[str], None]) -> bool:
    source = operation.source
    destination = operation.destination
    if not path_exists(source):
        log(f"skipped missing source: {source}")
        return True
    if path_exists(destination):
        log(f"failed copy; destination already exists: {destination}")
        return False

    try:
        # Resolve the actual parent before writing so a tracked symlink cannot redirect a copy.
        destination_parent = canonical_path(destination.parent)
        destination_parent.relative_to(operation.destination_root)
        destination_parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            destination.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
            shutil.copystat(source, destination, follow_symlinks=False)
        elif source.is_dir():
            shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
        elif source.is_file():
            shutil.copy2(source, destination, follow_symlinks=False)
        else:
            log(f"failed copy; unsupported source type: {source}")
            return False
    except (OSError, ValueError, shutil.Error) as error:
        log(f"failed copy {source} -> {destination}: {error}")
        return False

    log(f"copied {source} -> {destination}")
    return True


def git_dir(destination_root: Path, log: Callable[[str], None]) -> Path | None:
    command = ("git", "rev-parse", "--git-dir")
    log(f"running in {destination_root}: {shlex.join(command)}")
    try:
        completed = subprocess.run(
            command,
            cwd=destination_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        log(f"failed to start {shlex.join(command)}: {error}")
        return None
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        suffix = f": {detail}" if detail else ""
        log(
            f"command failed with exit status {completed.returncode}: {shlex.join(command)}{suffix}"
        )
        return None

    raw_git_dir = completed.stdout.strip()
    if not raw_git_dir:
        log("git rev-parse returned an empty Git directory")
        return None
    path = Path(raw_git_dir)
    if not path.is_absolute():
        path = destination_root / path
    resolved = canonical_path(path)
    log(f"resolved Git directory: {resolved}")
    return resolved


def configure_git_hooks(destination_root: Path, log: Callable[[str], None]) -> bool:
    """Give the new worktree a hook directory scoped to its own Git directory."""
    failed = not run_command(
        ("git", "config", "extensions.worktreeConfig", "true"),
        destination_root,
        log,
    )
    resolved_git_dir = git_dir(destination_root, log)
    if resolved_git_dir is None:
        return False
    if not run_command(
        (
            "git",
            "config",
            "--worktree",
            "core.hooksPath",
            str(resolved_git_dir / "hooks"),
        ),
        destination_root,
        log,
    ):
        failed = True
    return not failed


def configure_git_submodules(destination_root: Path, log: Callable[[str], None]) -> bool:
    """Initialize nested submodules at the commits pinned by the superproject."""
    return run_command(
        ("git", "submodule", "update", "--init", "--recursive"),
        destination_root,
        log,
    )


def run_command(
    command: Sequence[str], destination_root: Path, log: Callable[[str], None]
) -> bool:
    log(f"running in {destination_root}: {shlex.join(command)}")
    try:
        completed = subprocess.run(command, cwd=destination_root, check=False)
    except OSError as error:
        log(f"failed to start {shlex.join(command)}: {error}")
        return False
    if completed.returncode != 0:
        log(f"command failed with exit status {completed.returncode}: {shlex.join(command)}")
        return False
    log(f"command succeeded: {shlex.join(command)}")
    return True


def run_bootstrap(
    rules: Sequence[WorktreeRule],
    source_repo: Path,
    destination_root: Path,
    log: Callable[[str], None],
) -> bool:
    """Run matching rules. Return false after any failure, but attempt every step."""
    failed = False
    for rule in rules:
        for configured_path in rule.paths:
            try:
                operation = copy_operation(source_repo, destination_root, configured_path)
            except BootstrapError as error:
                log(f"failed copy configuration {configured_path!r}: {error}")
                failed = True
                continue
            if not copy_path(operation, log):
                failed = True
    if any(rule.git_hooks for rule in rules):
        if not configure_git_hooks(destination_root, log):
            failed = True
    if any(rule.git_submodules for rule in rules):
        if not configure_git_submodules(destination_root, log):
            failed = True
    for rule in rules:
        for command in rule.commands:
            if not run_command(command, destination_root, log):
                failed = True
    return not failed


def notification_delivery(response_json: str) -> tuple[bool, str] | None:
    """Extract whether Herdr showed a notification and its reported reason."""
    try:
        response = json.loads(response_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict) or result.get("type") != "notification_show":
        return None
    shown = result.get("shown")
    reason = result.get("reason")
    if not isinstance(shown, bool) or not isinstance(reason, str):
        return None
    return shown, reason


def notify_bootstrap_completion(
    destination_root: Path,
    succeeded: bool,
    herdr_binary: str,
    log: Callable[[str], None],
) -> None:
    """Notify the Herdr session without changing the bootstrap outcome."""
    title = "Worktree ready" if succeeded else "Worktree setup failed"
    body = str(destination_root) if succeeded else f"Check the plugin log for {destination_root}"
    sound = "done" if succeeded else "request"
    command = (
        herdr_binary,
        "notification",
        "show",
        title,
        "--body",
        body,
        "--sound",
        sound,
    )
    log(f"sending completion notification: {shlex.join(command)}")
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as error:
        log(f"failed to send completion notification: {error}")
        return
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        suffix = f": {detail}" if detail else ""
        log(
            f"failed to send completion notification with exit status {completed.returncode}{suffix}"
        )
        return

    response = notification_delivery(completed.stdout)
    if response is None:
        log(f"failed to parse completion notification response: {completed.stdout.strip()!r}")
        return
    shown, reason = response
    log(f"completion notification response: {completed.stdout.strip()}")
    if shown:
        log(f"completion notification shown: {reason}")
    else:
        log(f"completion notification was not shown: {reason}")


def log(message: str) -> None:
    print(f"[worktree-bootstrap] {message}", flush=True)


def main(environment: Mapping[str, str] = os.environ) -> int:
    config_dir = environment.get("HERDR_PLUGIN_CONFIG_DIR")
    if not config_dir:
        print("[worktree-bootstrap] HERDR_PLUGIN_CONFIG_DIR is not set", file=sys.stderr)
        return 2

    config_path = Path(config_dir) / CONFIG_FILENAME
    if not config_path.is_file():
        log(f"no configuration at {config_path}; nothing to do")
        return 0

    event_json = environment.get("HERDR_PLUGIN_EVENT_JSON")
    if not event_json:
        print("[worktree-bootstrap] HERDR_PLUGIN_EVENT_JSON is not set", file=sys.stderr)
        return 2

    try:
        event = json.loads(event_json)
        source_repo, destination_root = worktree_paths_from_event(event)
        rules = matching_rules(load_rules(config_path), source_repo)
    except (BootstrapError, json.JSONDecodeError) as error:
        print(f"[worktree-bootstrap] configuration error: {error}", file=sys.stderr)
        return 2

    if not rules:
        log(f"no rule matches source repository {source_repo}; nothing to do")
        return 0

    log(f"bootstrapping {destination_root} from {source_repo}")
    succeeded = run_bootstrap(rules, source_repo, destination_root, log)
    notify_bootstrap_completion(
        destination_root,
        succeeded,
        environment.get("HERDR_BIN_PATH") or "herdr",
        log,
    )
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
