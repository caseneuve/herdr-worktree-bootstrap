from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

import bootstrap  # noqa: E402


def notification_response(shown: bool, reason: str) -> str:
    return json.dumps(
        {
            "id": "cli:notification:show",
            "result": {
                "type": "notification_show",
                "shown": shown,
                "reason": reason,
            },
        }
    )


class BootstrapTest(unittest.TestCase):
    def event(self, source_repo: Path, destination: Path) -> dict[str, object]:
        return {
            "event": "worktree_created",
            "data": {
                "type": "worktree_created",
                "workspace": {
                    "worktree": {
                        "repo_root": str(source_repo),
                        "checkout_path": str(destination),
                    }
                },
                "worktree": {"path": str(destination)},
            },
        }

    def test_event_paths_use_parent_repo_and_new_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_repo = root / "parent"
            destination = root / "new-worktree"
            source_repo.mkdir()
            destination.mkdir()

            event_source, event_destination = bootstrap.worktree_paths_from_event(
                self.event(source_repo, destination)
            )

            self.assertEqual(event_source, source_repo.resolve())
            self.assertEqual(event_destination, destination.resolve())

    def test_load_rules_requires_a_repo_for_each_worktree_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.toml"
            config_path.write_text('[[worktree]]\npaths = [".env"]\n')

            with self.assertRaisesRegex(bootstrap.BootstrapError, r"rule 1\.repo"):
                bootstrap.load_rules(config_path)

    def test_load_rules_parses_git_setup_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[[worktree]]",
                        'repo = "/tmp/project"',
                        "git-hooks = true",
                        "git-submodules = true",
                        "",
                    ]
                )
            )

            rules = bootstrap.load_rules(config_path)

            self.assertTrue(rules[0].git_hooks)
            self.assertTrue(rules[0].git_submodules)

    def test_copy_operation_uses_repo_relative_and_absolute_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_repo = root / "parent"
            destination = root / "new-worktree"
            absolute_source = root / "private" / ".env"
            home = root / "home"
            source_repo.mkdir()
            destination.mkdir()
            absolute_source.parent.mkdir()
            absolute_source.touch()
            home.mkdir()

            relative = bootstrap.copy_operation(source_repo, destination, ".pi")
            absolute = bootstrap.copy_operation(source_repo, destination, str(absolute_source))
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                home_relative = bootstrap.copy_operation(
                    source_repo, destination, "~/private/project.env"
                )

            self.assertEqual(relative.source, source_repo / ".pi")
            self.assertEqual(relative.destination, destination / ".pi")
            self.assertEqual(absolute.source, absolute_source)
            self.assertEqual(absolute.destination, destination / ".env")
            self.assertEqual(home_relative.source, home / "private" / "project.env")
            self.assertEqual(home_relative.destination, destination / "project.env")

    def test_bootstrap_copies_directories_and_preserves_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_repo = root / "parent"
            destination = root / "new-worktree"
            source_pi = source_repo / ".pi"
            source_repo.mkdir()
            destination.mkdir()
            source_pi.mkdir()
            (source_pi / "settings.toml").write_text("enabled = true\n")
            (source_pi / "settings-link").symlink_to("settings.toml")
            rule = bootstrap.WorktreeRule(source_repo, (".pi",), False, False, ())
            messages: list[str] = []

            succeeded = bootstrap.run_bootstrap((rule,), source_repo, destination, messages.append)

            self.assertTrue(succeeded)
            self.assertEqual(
                (destination / ".pi" / "settings.toml").read_text(), "enabled = true\n"
            )
            self.assertTrue((destination / ".pi" / "settings-link").is_symlink())
            self.assertEqual(os.readlink(destination / ".pi" / "settings-link"), "settings.toml")

    def test_git_hooks_use_the_new_worktree_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_repo = root / "parent"
            destination = root / "new-worktree"
            source_repo.mkdir()

            def git(*arguments: str, cwd: Path = source_repo) -> str:
                return subprocess.run(
                    ["git", *arguments],
                    cwd=cwd,
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip()

            git("init")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Worktree Bootstrap Test")
            (source_repo / "README.md").write_text("fixture\n")
            git("add", "README.md")
            git("commit", "-m", "fixture")
            git("worktree", "add", "-b", "bootstrap-hooks", str(destination))
            rule = bootstrap.WorktreeRule(source_repo, (), True, False, ())
            messages: list[str] = []

            succeeded = bootstrap.run_bootstrap((rule,), source_repo, destination, messages.append)

            git_dir = Path(git("rev-parse", "--git-dir", cwd=destination))
            if not git_dir.is_absolute():
                git_dir = destination / git_dir
            self.assertTrue(succeeded)
            self.assertEqual(git("config", "--get", "extensions.worktreeConfig"), "true")
            self.assertEqual(
                git("config", "--worktree", "--get", "core.hooksPath", cwd=destination),
                str(git_dir.resolve() / "hooks"),
            )
            self.assertTrue(any("resolved Git directory" in message for message in messages))

    def test_git_submodules_initialize_before_custom_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_repo = root / "parent"
            submodule_repo = root / "submodule"
            destination = root / "new-worktree"
            source_repo.mkdir()
            submodule_repo.mkdir()

            def git(cwd: Path, *arguments: str) -> str:
                return subprocess.run(
                    ["git", *arguments],
                    cwd=cwd,
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip()

            for repo in (source_repo, submodule_repo):
                git(repo, "init")
                git(repo, "config", "user.email", "test@example.invalid")
                git(repo, "config", "user.name", "Worktree Bootstrap Test")
            (source_repo / "README.md").write_text("fixture\n")
            git(source_repo, "add", "README.md")
            git(source_repo, "commit", "-m", "fixture")
            (submodule_repo / "module.txt").write_text("submodule fixture\n")
            git(submodule_repo, "add", "module.txt")
            git(submodule_repo, "commit", "-m", "submodule fixture")
            git(source_repo, "config", "protocol.file.allow", "always")
            git(
                source_repo,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                str(submodule_repo),
                "vendor/module",
            )
            git(source_repo, "commit", "-am", "add submodule")
            git(
                source_repo,
                "worktree",
                "add",
                "-b",
                "bootstrap-submodules",
                str(destination),
            )
            rule = bootstrap.WorktreeRule(
                source_repo,
                (),
                False,
                True,
                (
                    (
                        sys.executable,
                        "-c",
                        "from pathlib import Path; assert Path('vendor/module/module.txt').is_file(); Path('submodule-command-ran').write_text('yes')",
                    ),
                ),
            )
            messages: list[str] = []

            with mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}):
                succeeded = bootstrap.run_bootstrap(
                    (rule,), source_repo, destination, messages.append
                )

            self.assertTrue(succeeded)
            self.assertEqual(
                (destination / "vendor" / "module" / "module.txt").read_text(),
                "submodule fixture\n",
            )
            self.assertEqual((destination / "submodule-command-ran").read_text(), "yes")
            self.assertTrue(
                any("git submodule update --init --recursive" in message for message in messages)
            )

    def test_completion_notification_reports_a_ready_worktree(self) -> None:
        destination = Path("/tmp/new-worktree")
        messages: list[str] = []

        with mock.patch.object(
            bootstrap.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                (), 0, notification_response(True, "shown"), ""
            ),
        ) as run:
            bootstrap.notify_bootstrap_completion(
                destination, True, "/usr/bin/herdr", messages.append
            )

        run.assert_called_once_with(
            (
                "/usr/bin/herdr",
                "notification",
                "show",
                "Worktree ready",
                "--body",
                str(destination),
                "--sound",
                "done",
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertIn("completion notification shown: shown", messages)

    def test_completion_notification_reports_bootstrap_failures(self) -> None:
        destination = Path("/tmp/new-worktree")
        messages: list[str] = []

        with mock.patch.object(
            bootstrap.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                (), 0, notification_response(True, "shown"), ""
            ),
        ) as run:
            bootstrap.notify_bootstrap_completion(destination, False, "herdr", messages.append)

        run.assert_called_once_with(
            (
                "herdr",
                "notification",
                "show",
                "Worktree setup failed",
                "--body",
                f"Check the plugin log for {destination}",
                "--sound",
                "request",
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertIn("completion notification shown: shown", messages)

    def test_completion_notification_logs_when_herdr_does_not_show_it(self) -> None:
        messages: list[str] = []

        with mock.patch.object(
            bootstrap.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                (), 0, notification_response(False, "disabled"), ""
            ),
        ):
            bootstrap.notify_bootstrap_completion(
                Path("/tmp/new-worktree"), True, "herdr", messages.append
            )

        self.assertIn("completion notification was not shown: disabled", messages)
        self.assertNotIn("completion notification shown: shown", messages)

    def test_completion_notification_logs_invalid_response(self) -> None:
        messages: list[str] = []

        with mock.patch.object(
            bootstrap.subprocess,
            "run",
            return_value=subprocess.CompletedProcess((), 0, "not JSON", ""),
        ):
            bootstrap.notify_bootstrap_completion(
                Path("/tmp/new-worktree"), True, "herdr", messages.append
            )

        self.assertIn("failed to parse completion notification response: 'not JSON'", messages)

    def test_notification_failure_is_logged(self) -> None:
        messages: list[str] = []

        with mock.patch.object(bootstrap.subprocess, "run", side_effect=OSError("unavailable")):
            bootstrap.notify_bootstrap_completion(
                Path("/tmp/new-worktree"), True, "herdr", messages.append
            )

        self.assertTrue(
            any("failed to send completion notification" in message for message in messages)
        )

    def test_notification_nonzero_exit_is_logged(self) -> None:
        messages: list[str] = []

        with mock.patch.object(
            bootstrap.subprocess,
            "run",
            return_value=subprocess.CompletedProcess((), 1, "", "server unavailable\n"),
        ):
            bootstrap.notify_bootstrap_completion(
                Path("/tmp/new-worktree"), True, "herdr", messages.append
            )

        self.assertIn(
            "failed to send completion notification with exit status 1: server unavailable",
            messages,
        )

    def test_all_copy_paths_finish_before_any_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_repo = root / "parent"
            destination = root / "new-worktree"
            source_repo.mkdir()
            destination.mkdir()
            (source_repo / "copied-before-commands").write_text("ready")
            rules = (
                bootstrap.WorktreeRule(
                    source_repo,
                    (),
                    False,
                    False,
                    (
                        (
                            sys.executable,
                            "-c",
                            "from pathlib import Path; raise SystemExit(not Path('copied-before-commands').is_file())",
                        ),
                    ),
                ),
                bootstrap.WorktreeRule(source_repo, ("copied-before-commands",), False, False, ()),
            )
            messages: list[str] = []

            succeeded = bootstrap.run_bootstrap(rules, source_repo, destination, messages.append)

            self.assertTrue(succeeded)
            self.assertTrue((destination / "copied-before-commands").is_file())
            self.assertTrue(any("command succeeded" in message for message in messages))

    def test_bootstrap_continues_after_collisions_and_command_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_repo = root / "parent"
            destination = root / "new-worktree"
            source_repo.mkdir()
            destination.mkdir()
            (source_repo / ".env").write_text("source\n")
            (destination / ".env").write_text("existing\n")
            rule = bootstrap.WorktreeRule(
                source_repo,
                (".env", ".missing"),
                False,
                False,
                (
                    (sys.executable, "-c", "raise SystemExit(7)"),
                    (
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('later-command-ran').write_text('ok')",
                    ),
                ),
            )
            messages: list[str] = []

            succeeded = bootstrap.run_bootstrap((rule,), source_repo, destination, messages.append)

            self.assertFalse(succeeded)
            self.assertEqual((destination / ".env").read_text(), "existing\n")
            self.assertEqual((destination / "later-command-ran").read_text(), "ok")
            self.assertTrue(any("destination already exists" in message for message in messages))
            self.assertTrue(any("skipped missing source" in message for message in messages))
            self.assertTrue(any("exit status 7" in message for message in messages))

    def test_main_returns_failure_after_a_failed_step_but_runs_later_commands(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_dir = root / "config"
            source_repo = root / "parent"
            destination = root / "new-worktree"
            config_dir.mkdir()
            source_repo.mkdir()
            destination.mkdir()
            (source_repo / ".env").write_text("source\n")
            (destination / ".env").write_text("existing\n")
            (config_dir / "config.toml").write_text(
                "\n".join(
                    [
                        "[[worktree]]",
                        f'repo = "{source_repo}"',
                        'paths = [".env"]',
                        "commands = [",
                        f'  ["{sys.executable}", "-c", "from pathlib import Path; Path(\\"ran\\").write_text(\\"yes\\")"],',
                        "]",
                        "",
                    ]
                )
            )
            environment = {
                "HERDR_PLUGIN_CONFIG_DIR": str(config_dir),
                "HERDR_PLUGIN_EVENT_JSON": json.dumps(self.event(source_repo, destination)),
                "HERDR_BIN_PATH": "/test/herdr",
            }

            with mock.patch.object(bootstrap, "notify_bootstrap_completion") as notify:
                exit_code = bootstrap.main(environment)

            self.assertEqual(exit_code, 1)
            self.assertEqual((destination / "ran").read_text(), "yes")
            notify.assert_called_once_with(
                destination.resolve(), False, "/test/herdr", bootstrap.log
            )


if __name__ == "__main__":
    unittest.main()
