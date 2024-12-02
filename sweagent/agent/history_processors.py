from __future__ import annotations

import re
from abc import abstractmethod
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class FormatError(Exception):
    pass


class AbstractHistoryProcessor(Protocol):
    @abstractmethod
    def __call__(self, history: list[str]) -> list[str]:
        raise NotImplementedError


class DefaultHistoryProcessor(BaseModel):
    type: Literal["default"] = "default"
    """Do not change. Used for (de)serialization."""

    # pydantic config
    model_config = ConfigDict(extra="forbid")

    def __call__(self, history):
        return history


class LastNObservations(BaseModel):
    """Keep the last n observations"""

    n: int
    type: Literal["last_n_observations"] = "last_n_observations"
    """Do not change. Used for (de)serialization."""

    # pydantic config
    model_config = ConfigDict(extra="forbid")

    def __call__(self, history):
        if self.n <= 0:
            msg = "n must be a positive integer"
            raise ValueError(msg)
        new_history = list()
        observation_idxs = [
            idx
            for idx, entry in enumerate(history)
            if entry["message_type"] == "observation" and not entry.get("is_demo", False)
        ]
        omit_content_idxs = [idx for idx in observation_idxs[1 : -self.n]]
        for idx, entry in enumerate(history):
            if idx not in omit_content_idxs:
                new_history.append(entry)
            else:
                data = entry.copy()
                assert data["message_type"] == "observation", "Expected observation for dropped entry"
                data["content"] = f'Old environment output: ({len(entry["content"].splitlines())} lines omitted)'
                new_history.append(data)
        return new_history


class ClosedWindowHistoryProcessor(BaseModel):
    type: Literal["closed_window"] = "closed_window"
    """Do not change. Used for (de)serialization."""

    _pattern = re.compile(r"^(\d+)\:.*?(\n|$)", re.MULTILINE)
    _file_pattern = re.compile(r"\[File:\s+(.*)\s+\(\d+\s+lines\ total\)\]")

    # pydantic config
    model_config = ConfigDict(extra="forbid")

    def __call__(self, history):
        new_history = list()
        # For each value in history, keep track of which windows have been shown.
        # We want to mark windows that should stay open (they're the last window for a particular file)
        # Then we'll replace all other windows with a simple summary of the window (i.e. number of lines)
        windows = set()
        for entry in reversed(history):
            data = entry.copy()
            if data["role"] != "user":
                new_history.append(entry)
                continue
            if data.get("is_demo", False):
                new_history.append(entry)
                continue
            matches = list(self._pattern.finditer(entry["content"]))
            if len(matches) >= 1:
                file_match = self._file_pattern.search(entry["content"])
                if file_match:
                    file = file_match.group(1)
                else:
                    continue
                if file in windows:
                    start = matches[0].start()
                    end = matches[-1].end()
                    data["content"] = (
                        entry["content"][:start]
                        + f"Outdated window with {len(matches)} lines omitted...\n"
                        + entry["content"][end:]
                    )
                windows.add(file)
            new_history.append(data)
        return list(reversed(new_history))


HistoryProcessor = Annotated[
    DefaultHistoryProcessor | LastNObservations | ClosedWindowHistoryProcessor, Field(discriminator="type")
]
