import asyncio
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal, Self

import pydantic
from git import InvalidGitRepositoryError, Repo
from pydantic import BaseModel
from swerex.deployment.abstract import AbstractDeployment
from swerex.runtime.abstract import Command, UploadRequest

from sweagent.utils.config import keys_config


class AbstractRepoConfig(ABC):
    @abstractmethod
    def copy(self, deployment: AbstractDeployment):
        pass


class LocalRepoConfig(BaseModel, AbstractRepoConfig):
    path: str = ""
    base_commit: str = "HEAD"

    type: Literal["local"] = "local"
    """Discriminator for (de)serialization/CLI. Do not change."""

    @property
    def repo_name(self) -> str:
        return Path(self.path).resolve().name.replace(" ", "-").replace("'", "")

    @pydantic.model_validator(mode="after")
    def check_valid_repo(self) -> Self:
        try:
            repo = Repo(self.path, search_parent_directories=True)
        except InvalidGitRepositoryError as e:
            msg = f"Could not find git repository at {self.path=}."
            raise ValueError(msg) from e
        if repo.is_dirty() and "PYTEST_CURRENT_TEST" not in os.environ:
            msg = f"Local git repository {self.path} is dirty. Please commit or stash changes."
            raise ValueError(msg)
        return self

    def copy(self, deployment: AbstractDeployment):
        asyncio.run(deployment.runtime.upload(UploadRequest(source_path=self.path, target_path=f"/{self.repo_name}")))
        r = asyncio.run(deployment.runtime.execute(Command(command=f"chown -R root:root {self.repo_name}", shell=True)))
        if r.exit_code != 0:
            msg = f"Failed to change permissions on copied repository (exit code: {r.exit_code}, stdout: {r.stdout}, stderr: {r.stderr})"
            raise RuntimeError(msg)


class GithubRepoConfig(BaseModel, AbstractRepoConfig):
    url: str = ""

    base_commit: str = "HEAD"

    type: Literal["github"] = "github"
    """Discriminator for (de)serialization/CLI. Do not change."""

    @property
    def repo_name(self) -> str:
        # fixme: Need to replace ":" etc.
        org, repo = self.url.split("/")[-2:]
        return f"{org}__{repo}"

    def _get_url_with_token(self, token: str) -> str:
        """Prepend github token to URL"""
        if "@" in self.url:
            return self.url
        _, _, url_no_protocol = self.url.partition("://")
        return f"https://{token}@{url_no_protocol}"

    def copy(self, deployment: AbstractDeployment):
        base_commit = self.base_commit
        github_token = keys_config.get("GITHUB_TOKEN", "")
        url = self._get_url_with_token(github_token)
        r = asyncio.run(
            deployment.runtime.execute(
                Command(
                    command=" && ".join(
                        (
                            f"mkdir {self.repo_name}",
                            f"cd {self.repo_name}",
                            "git init",
                            f"git remote add origin {url}",
                            f"git fetch --depth 1 origin {base_commit}",
                            "git checkout FETCH_HEAD",
                            "cd ..",
                        )
                    ),
                    timeout=float(keys_config.get("SWE_AGENT_ENV_LONG_TIMEOUT", 500)),
                    shell=True,
                )
            ),
        )
        if r.exit_code != 0:
            msg = f"Failed to clone repository (exit code: {r.exit_code}, stdout: {r.stdout}, stderr: {r.stderr})"
            raise RuntimeError(msg)


RepoConfig = LocalRepoConfig | GithubRepoConfig
