from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from datasets import load_dataset, load_from_disk

from sweagent.utils.config import keys_config
from sweagent.utils.log import get_logger

DOCKER_START_UP_DELAY = float(keys_config.get("SWE_AGENT_DOCKER_START_UP_DELAY", 1))
DOCKER_COMPOSE_TERMINATION_DELAY = float(keys_config.get("SWE_AGENT_DOCKER_START_UP_DELAY", 100))
DOCKER_COMPOSE_STARTUP_DELAY = float(keys_config.get("SWE_AGENT_DOCKER_START_UP_DELAY", 600))
GITHUB_ISSUE_URL_PATTERN = re.compile(r"github\.com\/(.*?)\/(.*?)\/issues\/(\d+)")
GITHUB_REPO_URL_PATTERN = re.compile(r".*[/@]?github\.com\/([^/]+)\/([^/]+)")

CTF_CHALLENGES_CATEGORIES = {
    "rev": "reverse engineering",
    "pwn": "binary exploitation",
    "web": "web security",
    "crypto": "cryptography",
    "misc": "miscellaneous",
    "forensics": "forensics",
}

logger = get_logger("env_utils")


class NoOutputTimeoutError(TimeoutError): ...


def get_data_path_name(data_path: str) -> str:
    """if data_path is a file, return the file stem
    elif it's a github url, return the owner__repo_name
    """
    if data_path.startswith("text://"):
        return hashlib.sha256(data_path.removeprefix("text://").encode()).hexdigest()[:6]
    match = GITHUB_ISSUE_URL_PATTERN.search(data_path)
    if match:
        owner, repo, _ = match.groups()
        return f"{owner}__{repo}"
    return Path(data_path).stem


DECODED_BUFFER_FAILURE_THRESHOLD = 0.1


def _check_for_too_many_non_unicode_bytes(buffer: bytes):
    number_of_failures = int(DECODED_BUFFER_FAILURE_THRESHOLD * len(buffer))
    start_byte = 0
    for _ in range(number_of_failures):
        try:
            buffer[start_byte:].decode()
            return
        except UnicodeDecodeError as e:
            start_byte = e.start + 1
    msg = "Too many non-unicode characters in output of command."
    raise UnicodeError(msg)


# def terminate_docker_compose(docker_compose_path: Path) -> None:
#     terminate_cmd = [
#         "docker",
#         "compose",
#         "-f",
#         str(docker_compose_path),
#         "down",
#     ]
#     logger.debug("Terminating docker-compose with command: %s", shlex.join(terminate_cmd))
#     compose = subprocess.Popen(
#         terminate_cmd,
#         stdin=PIPE,
#         stdout=PIPE,
#         stderr=STDOUT,
#         text=True,
#         bufsize=1,  # line buffered
#     )
#     _, error = compose.communicate(timeout=DOCKER_COMPOSE_TERMINATION_DELAY)
#     if error:
#         logger.error(f"Unexpected compose termination error: {error}")


# def attach_network_interface_to_container(container_name: str) -> None:
#     cmd = [
#         "docker",
#         "network",
#         "connect",
#         "ctfnet",
#         container_name,
#     ]
#     logger.debug("Attaching NIC to container with command: %s", shlex.join(cmd))
#     compose = subprocess.Popen(
#         cmd,
#         stdin=PIPE,
#         stdout=PIPE,
#         stderr=STDOUT,
#         text=True,
#         bufsize=1,  # line buffered
#     )
#     _, error = compose.communicate(timeout=DOCKER_START_UP_DELAY)
#     if error:
#         logger.error(f"Unexpected compose setup error: {error}")
#         raise RuntimeError(error)


# def get_docker_compose(docker_compose_path: Path) -> Path:
#     startup_cmd = [
#         "docker",
#         "compose",
#         "-f",
#         str(docker_compose_path),
#         "up",
#         "-d",
#         "--force-recreate",
#     ]
#     logger.debug("Starting docker-compose with command: %s", shlex.join(startup_cmd))
#     compose = subprocess.Popen(
#         startup_cmd,
#         stdin=PIPE,
#         stdout=PIPE,
#         stderr=STDOUT,
#         text=True,
#         bufsize=1,  # line buffered
#     )
#     _, error = compose.communicate(timeout=DOCKER_COMPOSE_STARTUP_DELAY)
#     if error:
#         logger.error(f"Unexpected compose setup error: {error}")
#     return docker_compose_path


class InvalidGithubURL(ValueError): ...


def get_instances(
    file_path: str,
    base_commit: str | None = None,
    split: str | None = None,
    token: str | None = None,
    *,
    repo_path: str = "",
) -> list[dict[str, Any]]:
    """
    Getter function for handling json, jsonl files

    Args:
        file_path (str): Path to file

    Returns:
        List of instances as dictionaries
    """

    def instance_from_dict(instances):
        raise NotImplementedError
        # ib = InstanceBuilder(token=token)
        # ib.set_from_dict(instances)
        # return ib.build()

    def postproc_instance_list(instances):
        if isinstance(instances, dict):
            msg = "Expected a list of instances, got a dictionary."
            raise ValueError(msg)
        return [instance_from_dict(x) for x in instances]

    # The next if statement is very brittle logic to determine if we're processing a single instance
    if base_commit:
        msg = "base_commit must be empty if running over multiple problem statements"
        raise ValueError(msg)

    if repo_path:
        if not Path(repo_path).exists():
            msg = f"Specified repository path {repo_path} does not exist"
            raise FileNotFoundError(msg)
        msg = "repo_path must be empty if running over multiple problem statements"
        raise ValueError(msg)

    # If file_path is a directory, attempt load from disk
    if Path(file_path).is_dir():
        try:
            dataset_or_dict = load_from_disk(file_path)
            if isinstance(dataset_or_dict, dict):
                return postproc_instance_list(dataset_or_dict[split])
            return postproc_instance_list(dataset_or_dict)
        except FileNotFoundError:
            # Raised by load_from_disk if the directory is not a dataset directory
            pass

    if base_commit is not None:
        msg = "base_commit must be None if data_path is not a github issue url"
        raise ValueError(msg)

    # If file_path is a file, load the file
    if file_path.endswith(".json"):
        with open(file_path) as file:
            return postproc_instance_list(json.load(file))
    if file_path.endswith(".jsonl"):
        return postproc_instance_list([json.loads(x) for x in Path(file_path).read_text().splitlines(keepends=True)])

    # Attempt load from HF datasets as a last resort
    try:
        return postproc_instance_list(load_dataset(file_path, split=split))
    except Exception as e:
        msg = (
            f"Could not load instances from {file_path}. "
            "Please ensure --data_path is a GitHub URL, a SWE-bench HuggingFace dataset, or a JSON/JSONL file."
        )
        raise ValueError(msg) from e
