from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import shlex
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

import gymnasium as gym
import yaml
from ghapi.all import GhApi
from git import Repo
from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
from swebench.harness.utils import get_environment_yml, get_requirements
from swerex.deployment import get_deployment
from swerex.runtime.abstract import Action, CreateSessionRequest, UploadRequest, WriteFileRequest

from sweagent import REPO_ROOT
from sweagent.agent.interactive_commands import (
    INTERACTIVE_SESSIONS_CONFIG,
    InteractiveSession,
    InteractiveSessionConfig,
)
from sweagent.environment.utils import (
    InvalidGithubURL,
    NoOutputTimeoutError,
    PatchFormatter,
    attach_network_interface_to_container,
    copy_file_to_container,
    format_trajectory_markdown,
    get_docker_compose,
    get_gh_issue_data,
    get_instances,
    parse_gh_issue_url,
    terminate_docker_compose,
)
from sweagent.types import AgentInfo
from sweagent.utils.config import keys_config
from sweagent.utils.log import default_logger, get_logger

LONG_TIMEOUT = float(keys_config.get("SWE_AGENT_ENV_LONG_TIMEOUT", 500))
AGENT_ACTION_TIMEOUT = float(keys_config.get("SWE_AGENT_ACTION_TIMEOUT", 25))
AGENT_ACTION_NO_OUTPUT_TIMEOUT = float(keys_config.get("SWE_AGENT_ACTION_NO_OUTPUT_TIMEOUT", AGENT_ACTION_TIMEOUT))
PATH_TO_REQS = "/root/requirements.txt"
PATH_TO_ENV_YML = "/root/environment.yml"


@dataclass
class DeploymentConfig:
    """Configuration for the deployment of the environment"""

    type: str = "docker"
    kwargs: dict[str, Any] = field(default_factory=lambda: {"image": "sweagent/swe-agent:latest"})


@dataclass
class EnvironmentArguments:
    """Configure data sources and setup instructions for the environment in which we solve the tasks."""

    # Source of issue statement/problem statement. To run over a batch of issues: Path to a data file
    # (`json`, `jsonl`) or directory. To run over single issue: github issue url or path to markdown file
    # with problem statement or problem statement as text prefixed with `text://`.
    data_path: str = ""

    deployment: DeploymentConfig = field(default_factory=DeploymentConfig)

    # When running over SWE-bench issues: Specify the split to use.
    split: str = "dev"
    # Specify a branch name or a commit hash to checkout before running the task.
    # Only used when running over a single problem statement/issue.
    base_commit: str | None = None
    # Use a persistent container with this name. After every task, the container will be paused, but not removed.
    # This is useful for speedup when running multiple tasks from the same repositories in a row, as the repositories
    # will have already been cloned and the conda environments will have been installed.
    container_name: str | None = None
    # Try to install the environment before running the task.
    install_environment: bool = True
    # No effect, kept for backwards compatibility.
    timeout: int | None = None
    # Enable environment logger.
    verbose: bool = False
    # Do not use attempt to use a repository mirror from https://github.com/swe-bench.
    no_mirror: bool = False
    # Custom environment setup. Currently only used when data_path points to a single issue.
    # This needs to be either a string pointing to a yaml file (with yaml, yml file extension)
    # or a shell script (with sh extension).
    # See https://princeton-nlp.github.io/SWE-agent/usage/cl_tutorial#environment-setup
    environment_setup: str | None = None
    # Only used when running on single issue. Path to local repository or github repository.
    repo_path: str = ""
    # Interactive command configuration
    interactive_sessions_config: dict[str, InteractiveSessionConfig] = field(
        default_factory=lambda: INTERACTIVE_SESSIONS_CONFIG
    )
    # Container mounts - additional folders to mount into the environment (useful for caching)
    container_mounts: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.timeout is not None:
            default_logger.warning("The 'timeout' argument is deprecated and has no effect.")
        if self.container_name is not None and self.container_name.strip() == "":
            msg = "Set container_name to None if you don't want to use a persistent container."
            raise ValueError(msg)


class EnvHook:
    """Hook to be used in `SWEEnv`.

    Subclass this class, add functionality and add it with `SWEEEnv.add_hook(hook)`.
    This allows to inject custom functionality at different stages of the environment
    lifecycle, in particular to connect SWE-agent to a new interface (like a GUI).
    """

    def on_init(self) -> None:
        """Gets called when the hook is added"""

    def on_copy_repo_started(self, *, repo_type: str, repo_path: str) -> None:
        """Gets called when the repository is being cloned to the container

        Args:
            repo_type: Type of repository. Either 'local' or 'github'
            repo_path: Path to the repository
        """

    def on_install_env_started(self) -> None:
        """Called when we start installing the environment"""

    def on_close(self):
        """Called when the environment is closed"""


class SWEEnv(gym.Env):
    """Gym environment for SWE-bench. This class should handle all communication with the docker container."""

    name = "swe_main"
    # This prefix will be prepended to the image name when caching task images
    cached_image_prefix = "swe-agent-task-env-"

    def __init__(self, args: EnvironmentArguments):
        super().__init__()
        t0 = time.perf_counter()
        self.args = args
        self.base_commit: str | None = None
        self.container_name: str | None = args.container_name
        self.install_environment = args.install_environment
        self.logger = get_logger("SWEEnv")
        self.persistent = args.container_name is not None
        self.container_mounts = args.container_mounts
        self.returncode: None | int = None
        if not self.args.verbose:
            # fixme: This creates problems if we have multiple instances of this class
            self.logger.disabled = True

        #: The commit hash of the swe-agent repository
        self.commit_sha = None
        try:
            repo = Repo(REPO_ROOT, search_parent_directories=True)
            self.commit_sha = repo.head.object.hexsha
        except KeyboardInterrupt:
            raise
        except Exception as e:
            self.logger.exception("Failed to get commit hash for this repo: %s", str(e))

        self._github_token: str = keys_config.get("GITHUB_TOKEN", "")  # type: ignore

        # Load Task Instances
        self.data_path = self.args.data_path
        self.data = get_instances(
            self.data_path,
            self.args.base_commit,
            self.args.split,
            token=self._github_token,
            repo_path=self.args.repo_path,
        )
        #: Instance we're currently processing. Gets set in self.reset.
        self.record: dict[str, Any] | None = None
        self.logger.info(f"💽 Loaded dataset from {self.data_path}")

        # Establish connection with execution container
        self.docker_compose: Path | None = None
        self.challenge: dict[str, Any] | None = None
        self._reset_container()

        self.interactive_session: InteractiveSession | None = None

        self.idx = 0
        self.clean_multi_line_functions = lambda x: x
        self.hooks: list[EnvHook] = []

        self.logger.debug("Environment initialization took %.2f seconds", time.perf_counter() - t0)

    def _get_cached_task_image_name(self) -> str:
        assert self.record is not None
        inputs: list[str] = [
            self.record["repo"],
            self.record["base_commit"],
            self.args.environment_setup or "no_setup",
        ]
        tag = hashlib.sha256("".join(inputs).encode()).hexdigest()[:50]
        return f"{self.cached_image_prefix}{tag}"

    def add_hook(self, hook: EnvHook):
        """Add `EnvHook` to the environment.

        This allows to inject custom functionality at different stages of the environment
        lifecycle, in particular to connect SWE-agent to a new interface (like a GUI).
        """
        hook.on_init()
        self.hooks.append(hook)

    @property
    def _repo_name(self) -> str:
        """Name of the local copy of the repository"""
        assert self.record is not None
        return self.record["repo"].replace("/", "__").replace(" ", "-").replace("'", "")

    def _copy_repo(self) -> str:
        """Clone/copy repository/codebase in container

        Returns:
            folder name of clone
        """
        # assert self.container_obj is not None
        assert self.record is not None  # mypy
        for hook in self.hooks:
            hook.on_copy_repo_started(repo_type=self.record["repo_type"], repo_path=self.record["repo"])
        if self.record["repo_type"] == "local":
            if "challenge" in self.record:
                self.communicate_with_handling(
                    input=f"mkdir {self._repo_name}", error_msg=f"Failed to create {self._repo_name} in container"
                )
                for file_name in self.record["challenge"]["files"]:
                    self.logger.debug(f"Copying file {file_name} to container")
                    source_file_name = Path(self.record["repo"].removeprefix("local://")) / file_name
                    target_file_name = Path("/") / self._repo_name / file_name
                    asyncio.run(
                        self.deployment.runtime.upload(
                            UploadRequest(source_path=str(source_file_name), target_path=str(target_file_name))
                        )
                    )
            else:
                asyncio.run(
                    self.deployment.runtime.upload(
                        UploadRequest(
                            source_path=self.record["repo"].removeprefix("local://"), target_path="/" + self._repo_name
                        )
                    )
                )
            self.communicate_with_handling(
                input=f"chown -R root:root {self._repo_name}",
                error_msg="Failed to change permissions on copied repository",
            )
            return self._repo_name
        assert self.record["repo_type"] == "github"
        token_prefix = ""
        if self._github_token:
            token_prefix = f"{self._github_token}@"
        # fixme: This if statement is brittle and should probably be replaced with better logic
        if not self.args.no_mirror and self.record["problem_statement_source"] == "swe-bench":
            self.logger.info(f"{self._repo_name} not found in container, cloning...")
            clone_url = f"https://{token_prefix}github.com/swe-bench/{self._repo_name}.git"
        else:
            self.logger.info("Trying to clone from non-mirror...")
            clone_url = f"https://{token_prefix}github.com/{self.record['repo']}.git"
        clone_method = keys_config.get("SWE_AGENT_CLONE_METHOD", default="shallow", choices=["shallow", "full"])
        if len(self.data) > 1 or self.persistent:
            msg = "Falling back to full cloning method due to multiple instances or persistent container"
            clone_method = "full"
            self.logger.debug(msg)
        if clone_method == "full":
            self.communicate_with_handling(
                input=f"git clone {clone_url} {self._repo_name}",
                error_msg="Failed to clone repository from conservative method",
                timeout_duration=LONG_TIMEOUT,
            )
        else:
            base_commit = self.record["base_commit"]
            self.communicate_with_handling(
                input="&&".join(
                    (
                        f"mkdir {self._repo_name}",
                        f"cd {self._repo_name}",
                        "git init",
                        f"git remote add origin {clone_url}",
                        f"git fetch --depth 1 origin {base_commit}",
                        "git checkout FETCH_HEAD",
                        "cd ..",
                    )
                ),
                error_msg="Failed to clone repository with fast method",
                timeout_duration=LONG_TIMEOUT,
            )
        return self._repo_name

    def reset(self, index: int | None = None, apply_test_patch: bool = False) -> tuple[str | None, dict]:
        """
        Function to reset container between each task instance.

        * Clones instance's repository
        * Cleans repository of prior modifications
        * Resets environment variables
        * Check out base commit

        Args:
            index: index of task instance to reset to

        Returns:
            observation: output from container
            info: additional information (e.g. debugging information)
        """
        info = {}
        info["commit_sha"] = self.commit_sha

        # Get task instance
        self.idx = index if index is not None else self.idx
        self.record = self.data[self.idx]
        self.idx += 1

        # Set query, gold command
        self.base_commit = self.record["base_commit"]
        self.query = self.record["problem_statement"]
        self.challenge = self.record.get("challenge")
        self.reward = None

        ### Reset Container ###
        self._init_docker_compose()

        # Init docker network
        self._init_docker_network()

        # Clone repository if not already cloned
        self.communicate(input="cd /")
        folders = self.communicate(input="ls").split("\n")
        if self._repo_name not in folders:
            print("copying repo")
            self._copy_repo()

        self._reset_repository()
        self._reset_environment_variables()

        # Set up environment
        self.communicate_with_handling(
            "source /root/miniconda3/etc/profile.d/conda.sh",
            error_msg="Failed to source conda",
        )

        system = self.communicate("uname -s").strip().lower()
        arch = self.communicate("uname -m").strip().lower()
        if system == "linux" and arch == "x86_64":
            self.communicate_with_handling(
                "apt update; apt install build-essential -y",
                error_msg="Failed to install build-essential",
                timeout_duration=LONG_TIMEOUT,
            )

        # Call install environment helper function if specified
        if self.install_environment:
            self.install_env()
        # Install mypy for linting purposes
        # self.communicate_with_handling("pip install flake8", error_msg="Failed to install flake8 (lint library)")

        if apply_test_patch:
            self._apply_test_patch()
        # Write any metadata to info if necessary
        return None, info

    def _reset_repository(self) -> None:
        """Clean repository of any modifications + Checkout base commit"""
        startup_commands = [
            "echo -n > /root/files_to_edit.txt",
            f"cd /{self._repo_name}",
            "export ROOT=$(pwd -P)",
        ]
        if self.challenge is None:
            startup_commands += [
                "git status",
                "git restore .",
                f"git reset --hard {self.base_commit}",
                "git clean -fdxq",
            ]
        self.communicate_with_handling(
            input=" && ".join(startup_commands),
            error_msg="Failed to clean repository",
        )

    def _reset_environment_variables(self) -> None:
        """Reset environment variables (`CURRENT_FILE`) etc. within container"""
        cmd = [
            'export CURRENT_FILE=""',
            "export CURRENT_LINE=0",
            "export SEARCH_RESULTS=()",
            "export SEARCH_FILES=()",
            "export SEARCH_INDEX=0",
        ]
        self.communicate_with_handling(
            input=" && ".join(cmd),
            error_msg="Failed to reset environment variables",
        )

    def reset_for_new_attempt(
        self,
    ) -> None:
        """Compared to `reset`, which prepares the container for a new instance,
        this prepares the container for taking another shot at the same instance.
        """
        self._reset_repository()
        self._reset_environment_variables()

    def _apply_test_patch(self):
        """
        Apply test patch for oracle setting
        """
        # needs updating, but do we really need this?
        raise NotImplementedError()
        assert self.record is not None
        path_to_patch = "test.patch"
        with open(path_to_patch, "w") as f:
            f.write(self.record["test_patch"])
        subprocess.run(
            f"docker cp {path_to_patch} {self.container_name}:/root/test.patch",
            shell=True,
            check=False,
        )
        self.communicate_with_handling(
            input="git apply /root/test.patch",
            error_msg="Failed to apply test patch correctly",
        )
        os.remove(path_to_patch)

    def _get_edited_files_with_context(self, patch: str) -> dict[str, str]:
        """Get the edited files with context from the patch"""
        pf = PatchFormatter(patch, read_method=self.read_file) if patch else None
        out = {}
        for context_length in [30, 50, 70]:
            value = "Empty. No edited files found."
            if pf is not None:
                value = pf.get_files_str(original=False, context_length=context_length)
            out[f"edited_files{context_length}"] = value
        return out

    def _terminate_interactive_session(self, session_name: str):
        if not self.interactive_session:
            # Maybe fixing #772
            return
        try:
            self.interactive_session.session_process.terminate()
            self.communicate(self.interactive_session.config.exit_command)
        except Exception as e:
            msg = (
                f"Failed to terminate interactive session {session_name}: {e}."
                "\nHere's the full traceback\n" + traceback.format_exc()
            )
            self.logger.warning(msg)
        self.interactive_session = None

    def _handle_interactive_commands(self, observation: str) -> str:
        """Handle interactive commands in the environment, essentially substituting dummy
        output for the actual output of the interactive commands.

        Args:
            observation: Output from running the interactive command wrappers in the
                environment. They will returns some dummy output that will be caught and then
                we will run the actual commands in the interactive session and return the
                actual output.

        Returns:
            observation: The observation shown to the model. If no interactive commands
                are detected, this is the same as the input observation.
                Else, only the output from the interactive commands is returned.
        """
        return observation
        # session_name, interactive_commands = get_interactive_commands(observation, logger=self.logger)
        # if session_name is None:
        #     return observation
        # if (
        #     session_name is not None
        #     and self.interactive_session is not None
        #     and self.interactive_session.name != session_name
        # ):
        #     return self.interactive_session._get_only_one_interactive_error_message_observation()

        # observation = ""
        # for command in interactive_commands:
        #     if command == "START":
        #         # Start the session if previous session does not exist
        #         if self.interactive_session is not None:
        #             return self.interactive_session._get_only_one_interactive_error_message_observation()
        #         assert self.container_name is not None
        #         _observation, self.interactive_session = get_interactive_session(
        #             ctr_name=self.container_name,
        #             ctr_obj=self.container_obj,
        #             cwd="/" + self._repo_name,
        #             session_name=session_name,
        #             config=self.args.interactive_sessions_config[session_name],
        #             logger=self.logger,
        #         )
        #         observation += _observation
        #     elif command == "STOP":
        #         if self.interactive_session is None:
        #             observation = f"Interactive session {session_name!r} is not running, so it cannot be stopped!"
        #         else:
        #             if self.interactive_session.session_process.poll() is None:
        #                 self.logger.warning("Session did not quit successfully, terminating.")
        #                 self.interactive_session.session_process.terminate()
        #             observation = f"Interactive session {session_name!r} stopped successfully"
        #             self.interactive_session = None
        #     else:
        #         if self.interactive_session is None:
        #             self.logger.warning("Tried to run interactive commands without starting session")
        #             start_command = self.args.interactive_sessions_config[session_name].start_command
        #             observation = f"Interactive session {session_name!r} is not running! please start it first using `{start_command}`"
        #         elif self.interactive_session and self.interactive_session.session_process.poll() is not None:
        #             start_command = self.args.interactive_sessions_config[session_name].start_command
        #             observation = f"Interactive session {session_name!r} was unexpectedly closed! Please start it again using `{start_command}`"
        #             self._terminate_interactive_session(session_name=session_name)
        #         else:
        #             _observation, terminate = self.interactive_session.communicate_with_handling(
        #                 command,
        #                 timeout_duration=AGENT_ACTION_TIMEOUT,
        #                 no_output_timeout_duration=AGENT_ACTION_NO_OUTPUT_TIMEOUT,
        #             )
        #             observation += _observation
        #             if terminate:
        #                 self._terminate_interactive_session(session_name=session_name)
        #             observation += "\n"
        # return observation

    def step(self, action: str) -> tuple[str | None, int, bool, AgentInfo]:
        """
        Runs an action proposed by the agent in the environment and returns the corresponding output.

        Args:
            action: command to run in bash shell

        Returns:
            observation:  output from container
            reward: Always set to 0
            done: whether task is over
            info: additional information (e.g. debugging information)
        """
        info: AgentInfo = {}
        # Make sure to have the right keys even if the submission is missing/empty
        info.update(self._get_edited_files_with_context(patch=""))  # type: ignore

        observation = ""
        # Handle special actions
        action = action.strip()
        if action == "skip":
            observation = "Skipped"
            info["exit_status"] = "skipped"
            return observation, 0, True, info
        if action == "exit_forfeit":
            observation = "Exited"
            info["exit_status"] = action
            return observation, 0, True, info
        if action in {"exit_context", "exit_cost", "exit_error", "exit_format", "exit_api"}:
            try:
                observation = self.communicate(input="submit")
                submission = self.get_submission(observation)
                assert submission is not None and submission.strip() != "", AssertionError("No submission found.")
                self.logger.info(f"Found submission: {submission}")
                info["exit_status"] = f"submitted ({action})"
                info["submission"] = submission
                info.update(self._get_edited_files_with_context(patch=submission))  # type: ignore
                observation = "Exited (autosubmitted)"
                self.logger.info("Exiting with autosubmission")
                return observation, 0, True, info
            except KeyboardInterrupt:
                raise
            except:
                observation = "Exited"
                info["exit_status"] = action
                return observation, 0, True, info

        # Attempt to run action in container
        observation = ""
        try:
            observation = self.communicate(
                input=action,
                timeout_duration=AGENT_ACTION_TIMEOUT,
                no_output_timeout_duration=AGENT_ACTION_NO_OUTPUT_TIMEOUT,
                set_last_action=True,
            )
        except TimeoutError as e:
            try:
                observation += e.args[1] if len(e.args) > 1 else ""
                # observation += self.interrupt()
                observation += "\nEXECUTION TIMED OUT"
                observation += (
                    f" BECAUSE NO OUTPUT WAS PRODUCED FOR MORE THAN {AGENT_ACTION_NO_OUTPUT_TIMEOUT} SECONDS.\nPLEASE REFINE YOUR RUNNING COMMAND SO IT WILL PRODUCE OUTPUT IN THE SPECIFIED TIME FRAME."
                    if isinstance(e, NoOutputTimeoutError)
                    else f" BECAUSE THE COMMAND WAS RUNNING FOR MORE THAN {AGENT_ACTION_TIMEOUT} SECONDS."
                )
            except RuntimeError as e:
                observation += e.args[1] if len(e.args) > 1 else ""
                observation += "\nEXECUTION TIMED OUT AND INTERRUPT FAILED. RESTARTING PROCESS."
                info["exit_status"] = "early_exit"
                self.logger.warning(f"Failed to interrupt container: {e}\nRESTARTING PROCESS.")
                self.reset_container()
                return observation, 0, True, info
        except RuntimeError as e:
            observation += e.args[1] if len(e.args) > 1 else ""
            observation += "\nCOMMAND FAILED TO EXECUTE. RESTARTING PROCESS."
            info["exit_status"] = "early_exit"
            self.logger.warning(f"Failed to execute command: {e}\nRESTARTING PROCESS.")
            self.reset_container()
            return observation, 0, True, info
        except BrokenPipeError as e:
            observation += "\nBROKEN PIPE ERROR. RESTARTING PROCESS."
            info["exit_status"] = "early_exit"
            self.logger.error(f"Broken pipe error: {e}\nRESTARTING PROCESS.")
            self.reset_container()
            return observation, 0, True, info
        except UnicodeError as e:
            observation += "\nCOMMAND PRODUCED TOO MANY NON-UNICODE CHARACTERS. PLEASE TRY ANOTHER COMMAND.\nIF YOU WANT TO VIEW BINARY FILES, PLEASE USE `xxd` OR `hexdump` INSTEAD.\n"
            self.logger.error(f"Unicode error: {e}")
        except Exception:
            observation += "\nEXECUTION FAILED OR COMMAND MALFORMED"
            self.logger.exception("Unknown exception")

        # Record submission and end episode if `submit` keyword found
        submission = self.get_submission(observation)
        if submission is not None:
            if self.validate_submission(submission):
                self.logger.info(f"Found submission: {submission}")
                info["exit_status"] = "submitted"
                info["submission"] = submission if submission.strip() != "" else None
                info.update(self._get_edited_files_with_context(patch=submission))  # type: ignore
                observation = submission if submission.strip() != "" else None
                return observation, 0, True, info
            else:
                # Currently only validating CTF challenges
                assert self.challenge is not None
                self.logger.warning(f"Wrong submission found: {submission} (real flag is {self.challenge['flag']})")
                observation = "Wrong flag!"
                return observation, 0, False, info

        observation = self._handle_interactive_commands(observation)

        return observation, 0, False, info

    def close(self) -> None:
        """
        Handle environment shutdown
        """
        self.logger.info("Beginning environment shutdown...")
        asyncio.run(self.deployment.stop())
        for hook in self.hooks:
            hook.on_close()

    # MARK: Helper functions #

    def _reset_container(self) -> None:
        if self.docker_compose is not None:
            try:
                terminate_docker_compose(self.docker_compose)
            except KeyboardInterrupt:
                raise
            except:
                self.logger.warning("Failed to terminate docker compose", exc_info=True)
            else:
                self.logger.debug("Terminated docker compose")
        self._init_container()
        self._init_scripts()

    def reset_container(self) -> None:
        self.close()
        self._reset_container()

    # ctf
    def _init_docker_network(self) -> None:
        """
        Add the "ctfnet" network interface for all the containers used for CTF challenges
        """
        if self.challenge is not None:
            assert self.container_name is not None
            attach_network_interface_to_container(self.container_name)

    # ctf
    def _init_docker_compose(self) -> None:
        """
        Handles docker compose initialization for challenge with docker compose file.
        """
        if self.challenge is not None and self.challenge.get("docker_compose") is not None:
            self.docker_compose = get_docker_compose(self.challenge["docker_compose"])
            self.logger.info("🌱 Initialized docker compose for challenge")

    def _init_container(self, cached_image: str | None = None) -> None:
        """
        Handles container initialization. Defines container name and creates it.
        If cached_image is provided, it will use that image name instead of the default.
        """
        self.deployment = get_deployment(self.args.deployment.type, **self.args.deployment.kwargs)  # type: ignore
        asyncio.run(self.deployment.start())
        asyncio.run(self.deployment.runtime.create_session(CreateSessionRequest(startup_source=["/root/.bashrc"])))
        self.logger.info("🌱 Environment Initialized")

    def _init_scripts(self):
        """
        Initialize custom commands within container
        """
        self.communicate_with_handling(
            "mkdir -p /root/commands",
            error_msg="Failed to create commands directory",
        )
        self.communicate_with_handling(
            "touch /root/commands/__init__.py",
            error_msg="Failed to create __init__.py",
        )
        self.communicate_with_handling(
            "export PATH=$PATH:/root/commands",
            error_msg="Failed to add commands directory to PATH",
        )

    def _communicate(
        self,
        input: str,
        timeout_duration: int | float = 25,
        no_output_timeout_duration: int | float = 25,
    ) -> str:
        """Runs command in container and returns output

        Args:
            input: command to run in container
            timeout_duration: duration to wait for output
            no_output_timeout_duration: duration to wait when the process stopped produce any output
        """
        self.returncode = None
        r = asyncio.run(self.deployment.runtime.run_in_session(Action(command=input, timeout=timeout_duration)))
        self.returncode = r.exit_code
        return r.output

    def communicate(
        self,
        input: str,
        timeout_duration: int | float = 25,
        no_output_timeout_duration: int | float | None = None,
        *,
        set_last_action: bool = False,
    ) -> str:
        """
        Sends input to container and returns output

        Args:
            input: input to send to container
            timeout_duration: duration to wait for output
            set_last_action: whether to set the LAST_ACTION environment variable

        Returns:
            output: output from container
        """
        if no_output_timeout_duration is None:
            no_output_timeout_duration = timeout_duration
        if input.strip() != "exit":
            self.logger.log(logging.TRACE, "Input:\n%s", input)  # type: ignore
            output = self._communicate(
                input,
                timeout_duration=timeout_duration,
                no_output_timeout_duration=no_output_timeout_duration,
            )
            self.logger.log(logging.TRACE, "Output:\n%s", output)  # type: ignore
            if set_last_action:
                # Cannot merge this with last command, because of multiline command
                # handling.
                last_action_string = shlex.quote(input.strip())
                input = f"export LAST_ACTION={last_action_string}"
                self._communicate(input, timeout_duration=5, no_output_timeout_duration=5)
            return output
        else:
            asyncio.run(self.deployment.stop())
            self.returncode = 0
            return ""

    def communicate_with_handling(self, input: str, error_msg: str, timeout_duration: int | float = 25) -> str:
        """
        Wrapper for communicate function that raises error if return code is non-zero

        Args:
            input: input to send to container
            error_msg: error message to raise if return code is non-zero
            timeout_duration: duration to wait for output

        Returns:
            output: output from container
        """
        logs = self.communicate(input, timeout_duration=timeout_duration)
        if self.returncode != 0:
            self.logger.error(f"{error_msg}: {logs}")
            self.close()
            msg = f"{error_msg}: {logs}"
            raise RuntimeError(msg)
        return logs

    def get_available_actions(self) -> list[str]:
        """
        Returns list of available actions in current environment state

        Currently not in use.
        """
        return []

    # ctf
    def validate_submission(self, submission: str) -> bool:
        """
        Function for validating submission for CTF challenges.

        Args:
            submission: extracted submission

        Returns:
            validation of the submission found against known flag
        """
        submission = submission.strip()

        def wrap(s: str) -> str:
            return f"flag{{{s}}}"

        if self.challenge is not None:
            assert "flag" in self.challenge
            solution = self.challenge["flag"]
            return any(
                (
                    submission == solution,
                    wrap(submission) == solution,
                    submission == wrap(solution),
                )
            )

        return True

    def get_submission(self, output: str) -> str | None:
        """
        Function for extracting diff patch submission at the end of an episode.

        Args:
            output: `submit` observation

        Returns:
            submission: diff patch submission
        """
        pattern = r"\<\<SUBMISSION\|\|(.*)\|\|SUBMISSION\>\>"
        match = re.search(pattern, output, re.DOTALL)
        if match is None:
            return None
        return match.group(1)

    def run_shell_script(self, script_path: Path, *, location: str) -> None:
        """Run custom script supplied by user at `script_path`

        Args:
            script_path: path to script file
            location: location of script file 'host' or 'container'
        """
        if location == "host":
            return self._run_shell_script_host(script_path)
        elif location == "container":
            raise NotImplementedError
        msg = f"Invalid 'location': {location}"
        raise ValueError(msg)

    def _run_shell_script_host(self, script_path: Path) -> None:
        """Run shell script file (located on host) in container"""
        if not script_path.is_file():
            msg = f"Script not found at {script_path}"
            raise FileNotFoundError(msg)
        shell_commands = Path(script_path).read_text().splitlines(keepends=True)
        for i, cmd in enumerate(shell_commands):
            self.communicate_with_handling(
                cmd,
                error_msg=f"Failed to execute line {i}.",
                timeout_duration=LONG_TIMEOUT,
            )

    def _get_install_configs(self) -> dict | None:
        """Return config for environment setup"""
        assert self.record is not None  # mypy
        if (
            self.record["problem_statement_source"] != "swe-bench" or self.record["repo_type"] == "local"
        ) and self.args.environment_setup is None:
            self.logger.warning(
                "install_environment is set to True, but the data path is a GitHub URL "
                "without an environment config file (environment_config key/flag). "
                "Skipping conda environment installation.",
            )
            return None
        if self.args.environment_setup is not None:
            assert isinstance(self.args.environment_setup, (str, os.PathLike))
            if Path(self.args.environment_setup).suffix in [".yml", ".yaml"]:
                try:
                    return yaml.safe_load(Path(self.args.environment_setup).read_text())
                except Exception as e:
                    msg = "Environment config file needs to be a yaml file"
                    raise ValueError(msg) from e
            elif Path(self.args.environment_setup).suffix == ".sh":
                return {
                    "shell_script_path": self.args.environment_setup,
                }
            else:
                msg = "Environment config file needs to be a yaml file or shell script"
                raise ValueError(msg)
        else:
            try:
                return MAP_REPO_VERSION_TO_SPECS[self.record["repo"]][str(self.record["version"])]
            except KeyError as e:
                msg = (
                    "Tried to look up install configs in swe-bench, but failed. "
                    "You can set a custom environment config with the environment_config key/flag."
                )
                raise ValueError(msg) from e

    def _conda_environment_exists(self, env_name: str) -> bool:
        env_check = self.communicate(f"conda env list | grep {env_name}", timeout_duration=LONG_TIMEOUT)
        return env_check.strip() != ""

    def install_env(self) -> None:
        """
        Creates conda environment and installs third party dependencies to allow code execution
        """
        return
        t0 = time.perf_counter()
        for hook in self.hooks:
            hook.on_install_env_started()
        install_configs = self._get_install_configs()
        if not install_configs:
            return
        if "shell_script_path" in install_configs:
            assert len(install_configs) == 1
            self.run_shell_script(Path(install_configs["shell_script_path"]), location="host")
            return
        assert self.record is not None  # mypy
        # Create environment if does not exist yet
        env_name = f"{self._repo_name}__{self.record['version']}"
        if not self._conda_environment_exists(env_name):
            self.logger.info(f"{env_name} conda env not found, creating...")
            packages = install_configs.get("packages", "")
            if packages == "requirements.txt":
                # Create conda environment
                self.communicate_with_handling(
                    f"conda create -n {env_name} python={install_configs['python']} -y",
                    error_msg="Failed to create conda environment",
                    timeout_duration=LONG_TIMEOUT,
                )
                self.logger.debug("Created conda environment")
                # Write reqs to requirements.txt in docker container
                content_reqs = get_requirements(self.record)
                copy_file_to_container(self.container_obj, content_reqs, PATH_TO_REQS)
                # Create conda environment + install reqs
                self.communicate_with_handling(
                    f"conda activate {env_name}",
                    error_msg="Failed to activate conda environment",
                )
                self.communicate_with_handling(
                    f"pip install -r {PATH_TO_REQS}",
                    error_msg="Failed to install requirements.txt",
                    timeout_duration=LONG_TIMEOUT,
                )
                self.logger.debug("Installed requirements from requirements.txt")
                self.communicate(f"rm {PATH_TO_REQS}")
            elif packages == "environment.yml":
                # Write environment.yml to file
                content_env_yml = get_environment_yml(self.record, env_name)
                # Hotfix for
                if not install_configs.get("no_use_env"):
                    content_env_yml += f'\n  - python={install_configs["python"]}\n'
                copy_file_to_container(self.container_obj, content_env_yml, PATH_TO_ENV_YML)
                if install_configs.get("no_use_env"):
                    # Create conda environment
                    self.communicate_with_handling(
                        f"conda create -c conda-forge -n {env_name} python={install_configs['python']} -y",
                        error_msg="Failed to create conda environment",
                        timeout_duration=LONG_TIMEOUT,
                    )
                    self.logger.debug("Created conda environment")
                    # Install packages
                    self.communicate_with_handling(
                        f"conda env update -f {PATH_TO_ENV_YML}",
                        error_msg="Failed to install environment.yml",
                        timeout_duration=LONG_TIMEOUT,
                    )
                    self.logger.debug("Installed packages from environment.yml")
                else:
                    # Create environment + install packages
                    self.communicate_with_handling(
                        f"conda env create --file {PATH_TO_ENV_YML}",
                        error_msg="Failed to create conda environment with environment.yml",
                        timeout_duration=LONG_TIMEOUT,
                    )
                    self.logger.debug("Created conda environment with environment.yml")
                self.communicate(f"rm {PATH_TO_ENV_YML}")
            else:
                python_env = f"python{install_configs['python']}"
                if self._conda_environment_exists(python_env):
                    self.communicate_with_handling(
                        f"conda create --name {env_name} --clone {python_env}",
                        error_msg="Failed to clone conda environment",
                        timeout_duration=LONG_TIMEOUT,
                    )
                    self.logger.debug("Cloned python conda environment")
                else:
                    self.logger.debug(f"Could not find {python_env}, creating new environment")
                    self.communicate_with_handling(
                        f"conda create -n {env_name} python={install_configs['python']} -y",
                        error_msg="Failed to create conda environment",
                        timeout_duration=LONG_TIMEOUT,
                    )
                self.communicate_with_handling(
                    f"conda activate {env_name}",
                    error_msg="Failed to activate conda environment",
                )
                if packages.strip():
                    self.communicate_with_handling(
                        f"conda install {packages} -y",
                        error_msg="Failed to install packages",
                        timeout_duration=LONG_TIMEOUT,
                    )
                    self.logger.debug("Installed conda packages")
            # Install extra pip packages if specified
            if install_configs.get("pip_packages"):
                self.communicate_with_handling(
                    f"source activate {env_name} && pip install {' '.join(install_configs['pip_packages'])}",
                    error_msg="Failed to install pip packages",
                    timeout_duration=LONG_TIMEOUT,
                )
                self.logger.debug("Installed extra pip dependencies")

        # Activate environment
        self.communicate_with_handling(f"conda activate {env_name}", error_msg="Failed to activate conda environment")

        # Install repo at base commit
        if install_configs.get("pre_install"):
            self.logger.info("Running pre-install commands...")
            for pre_install_cmd in install_configs["pre_install"]:
                self.communicate_with_handling(
                    pre_install_cmd,
                    error_msg="Pre-install commands failed to execute successfully",
                    timeout_duration=LONG_TIMEOUT,
                )
            self.logger.debug("Ran pre-install commands")
        self.logger.info(f"Installing {self._repo_name} at base commit...")
        if install_configs.get("install"):
            install_cmd = install_configs["install"]
            self.communicate_with_handling(
                install_cmd,
                error_msg="Install command failed to execute successfully",
                timeout_duration=LONG_TIMEOUT,
            )
            self.logger.debug("Ran install command")
        if install_configs.get("post_install"):
            self.logger.info("Running post-install commands...")
            for post_install_cmd in install_configs["post_install"]:
                self.communicate_with_handling(
                    post_install_cmd,
                    error_msg="Post-install commands failed to execute successfully",
                )
            self.logger.debug("Ran post-install commands")

        self.logger.info("Installation step took %.2f seconds", time.perf_counter() - t0)

    def add_commands(self, commands: list[dict]) -> None:
        """
        Adds custom commands to container
        """
        for command in commands:
            name = command["name"]
            contents = command["contents"]
            asyncio.run(
                self.deployment.runtime.write_file(WriteFileRequest(content=contents, path=f"/root/commands/{name}"))
            )
            if command["type"] == "source_file":
                self.communicate_with_handling(
                    f"source /root/commands/{name}",
                    error_msg=(
                        f"Failed to source {name}. If you meant to make a script,"
                        " start the file with a shebang (e.g. #!/usr/bin/env python)."
                    ),
                )
            elif command["type"] == "script":
                self.communicate_with_handling(
                    f"chmod +x /root/commands/{name}",
                    error_msg=f"Failed to chmod {name}",
                )
            elif command["type"] == "utility":
                # nothing to do for utility scripts
                pass
            else:
                msg = f"Invalid command type: {command['type']}"
                raise ValueError(msg)

    def open_pr(self, *, trajectory, _dry_run: bool = False) -> None:
        """Create PR to repository

        Args:
            trajectory: Trajectory of actions taken by the agent
            _dry_run: Whether to actually push anything or just simulate it
        """
        self.logger.info("Opening PR")
        # TODO: have better way of handling this
        # Adding random string suffix to avoid name conflicts if we had a previously failed run
        issue_url = self.args.data_path
        try:
            issue = get_gh_issue_data(issue_url, token=self._github_token)
        except InvalidGithubURL as e:
            msg = "Data path must be a github issue URL if --open_pr is set."
            raise ValueError(msg) from e
        branch_name = f"swe-agent-fix-#{issue.number}-" + str(random.random())[2:10]

        self.communicate_with_handling(
            input="rm -f model.patch",
            error_msg="Failed to remove model patch",
            timeout_duration=10,
        )
        self.communicate_with_handling(
            input=f"git checkout -b {branch_name}",
            error_msg="Failed to switch to new branch",
            timeout_duration=10,
        )
        self.communicate_with_handling(
            input="git add .",
            error_msg="Failed to add commits",
            timeout_duration=10,
        )
        dry_run_flag = "--allow-empty" if _dry_run else ""
        commit_msg = [
            shlex.quote("Fix: {issue.title}"),
            shlex.quote("Closes #{issue.number}"),
        ]
        self.communicate_with_handling(
            input=f"git commit -m {commit_msg[0]} -m  {commit_msg[1]} {dry_run_flag}",
            error_msg="Failed to commit changes",
            timeout_duration=10,
        )

        owner, repo, _ = parse_gh_issue_url(issue_url)
        # If `--repo_path` was specified with a different github URL, then the record will contain
        # the forking user
        assert self.record is not None
        if self.record["repo_type"] != "github":
            # We already validated that `--data_path` is a github issue URL
            # so this is the only case where we can reach here
            msg = "--repo_path must point to a github URL if --open_pr is set"
            raise ValueError(msg)
        forker, _ = self.record["repo"].split("/")
        head = branch_name
        remote = "origin"
        if forker != owner:
            head = f"{forker}:{branch_name}"
            token_prefix = ""
            if self._github_token:
                token_prefix = f"{self._github_token}@"
            fork_url = f"https://{token_prefix}github.com/{forker}/{repo}.git"
            self.logger.debug(f"Using fork: {fork_url}")
            self.communicate_with_handling(
                input=f"git remote add fork {fork_url}",
                error_msg="Failed to create new git remote",
                timeout_duration=10,
            )
            remote = "fork"
        dry_run_prefix = "echo " if _dry_run else ""
        self.communicate_with_handling(
            input=f"{dry_run_prefix} git push {remote} {branch_name}",
            error_msg=(
                "Failed to push branch to remote. Please check your token and permissions. "
                "You might want to push to a fork with the push_gh_repo_url option."
            ),
            timeout_duration=10,
        )
        body = (
            f"This is a PR opened by AI tool [SWE Agent](https://github.com/princeton-nlp/SWE-agent/) "
            f"to close [#{issue.number}]({issue_url}) ({issue.title}).\n\nCloses #{issue.number}."
        )
        body += "\n\n" + format_trajectory_markdown(trajectory)
        api = GhApi(token=self._github_token)
        if not _dry_run:
            pr_info = api.pulls.create(  # type: ignore
                owner=owner,
                repo=repo,
                title=f"SWE-agent[bot] PR to fix: {issue.title}",
                head=head,
                base="main",
                body=body,
                draft=True,
            )
            self.logger.info(
                f"🎉 PR created as a draft at {pr_info.html_url}. Please review it carefully, push "
                "any required changes onto the branch and then click "
                "'Ready for Review' to bring it to the attention of the maintainers.",
            )

    def read_file(self, path: str | PurePath) -> str:
        """Read file contents from container

        Args:
            path: Path to file relative to repository root

        Returns:
            file_contents: Contents of file as string
        """
        # todo: Just use the runtime for this instead
        path_in_container = f"/{self._repo_name}/{path}"
        return self.communicate(f"cat {str(path_in_container)}")
