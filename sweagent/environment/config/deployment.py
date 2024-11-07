from typing import Literal

from pydantic import BaseModel
from swerex.deployment import get_deployment
from swerex.deployment.abstract import AbstractDeployment


def _get_deployment(config) -> AbstractDeployment:
    dtype = config.type
    config_dict = config.model_dump()
    del config_dict["type"]
    return get_deployment(dtype, **config_dict)


class DummyDeploymentConfig(BaseModel):
    """This deployment does nothing. Used for testing purposes."""

    type: Literal["dummy"] = "dummy"
    """Discriminator for (de)serialization/CLI. Do not change."""

    def get_deployment(self) -> AbstractDeployment:
        return _get_deployment(self)


class LocalDeploymentConfig(BaseModel):
    type: Literal["local"] = "local"
    """Discriminator for (de)serialization/CLI. Do not change."""

    def get_deployment(self) -> AbstractDeployment:
        return _get_deployment(self)


class DockerDeploymentConfig(BaseModel):
    """Configuration for the deployment of the environment"""

    image: str = "sweagent/swe-agent:latest"

    type: Literal["docker"] = "docker"
    """Discriminator for (de)serialization/CLI. Do not change."""

    def get_deployment(self) -> AbstractDeployment:
        return _get_deployment(self)


class ModalDeploymentConfig(BaseModel):
    image: str = "sweagent/swe-agent:latest"

    type: Literal["modal"] = "modal"
    """Discriminator for (de)serialization/CLI. Do not change."""

    def get_deployment(self) -> AbstractDeployment:
        return _get_deployment(self)


DeploymentConfig = DockerDeploymentConfig | ModalDeploymentConfig | LocalDeploymentConfig | DummyDeploymentConfig
