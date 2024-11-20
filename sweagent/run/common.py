"""Common functionality for the run scripts."""

import json
from argparse import ArgumentParser
from pathlib import Path

import yaml
from pydantic import ValidationError
from pydantic_settings import BaseSettings, CliApp
from rich import print as rich_print
from rich.panel import Panel

from sweagent import CONFIG_DIR
from sweagent.types import AgentInfo, AgentRunResult
from sweagent.utils.log import get_logger


def _shorten_strings(data, *, max_length=30):
    """
    Recursively shortens all strings in a nested data structure to a maximum length.

    Args:
        data: The nested data structure (dicts, lists, and strings).
        max_length: The maximum length for strings.

    Returns:
        The modified data structure with shortened strings.
    """
    if isinstance(data, str):
        # Shorten the string if it exceeds the max length
        data = data.replace("\n", "\\n")
        return data[: max_length - 3] + "..."
    elif isinstance(data, list):
        # Recursively process each item in the list
        return [_shorten_strings(item, max_length=max_length) for item in data]
    elif isinstance(data, dict):
        # Recursively process each value in the dictionary
        return {key: _shorten_strings(value, max_length=max_length) for key, value in data.items()}
    else:
        # Return the data as is if it's neither a string, list, nor dict
        return data


# todo: Parameterize type hints
class BasicCLI:
    def __init__(self, arg_type: type[BaseSettings], default_settings: bool = True):
        self.arg_type = arg_type
        self.default_settings = default_settings
        self.logger = get_logger("swea-cli", emoji="🔧")

    def get_args(self, args=None) -> BaseSettings:
        # The defaults if no config file is provided
        # Otherwise, the configs from the respective classes will be used
        parser = ArgumentParser(description=__doc__)
        parser.add_argument(
            "--config",
            type=str,
            action="append",
            default=[],
            help="Load additional config files. Use this option multiple times to load multiple files, e.g., --config config1.yaml --config config2.yaml",
        )
        if self.default_settings:
            parser.add_argument(
                "--no-config-file",
                action="store_true",
                help="Do not load default config file when no config file is provided",
            )
        parser.add_argument(
            "--print-options",
            action="store_true",
            help="Print all additional configuration options that can be set via CLI and exit",
        )
        parser.add_argument("--print-config", action="store_true", help="Print the final config and exit")
        cli_args, remaining_args = parser.parse_known_args(args)

        if cli_args.print_options:
            CliApp.run(self.arg_type, ["--help"])
            exit(0)

        config_merged = {}
        config_files = []
        if cli_args.config:
            config_files.extend(cli_args.config)
            for _f in cli_args.config:
                txt = Path(_f).read_text()
                if not txt.strip():
                    self.logger.warning(f"Config file {_f} is empty")
                    continue
                _loaded = yaml.safe_load(txt)
                config_merged.update(_loaded)
        elif self.default_settings and not cli_args.no_config_file:
            config_file = CONFIG_DIR / "default.yaml"
            config_files.append(config_file)
            msg = (
                f"Loading default config from {config_file}, because no other "
                "config file is specified. Specify --no-config-file to disable this."
            )
            self.logger.info(msg)
            txt = config_file.read_text()
            if not txt.strip():
                self.logger.warning(f"Default config file {config_file} is empty")
                config_merged = {}
            else:
                config_merged = yaml.safe_load(txt)
        else:
            config_merged = {}

        try:
            args = CliApp.run(self.arg_type, remaining_args, **config_merged)  # type: ignore
        except ValidationError as e:
            rich_print(
                Panel.fit(
                    "[red][bold]Merged configuration dictionary\n[/bold]"
                    "This is all the configuration that was provided from defaults, --config, and CLI arguments[/red]\n\n"
                    + yaml.dump(_shorten_strings(config_merged))
                )
            )
            rich_print(
                Panel.fit(
                    "[red][bold]Validation error[/bold]\n"
                    + "The following errors are raised by Pydantic, trying to instantiate the configuration based on \n"
                    + "the merged configuration dictionary (see above).\n\n"
                    + "Every new indented block corresponds to a different error from Pydantic.\n"
                    + "The first line of each block is the attribute that failed validation, the following lines are the error messages.\n\n"
                    + "If you see many lines of errors, there are probably different ways to instantiate the same object (a union type).\n"
                    + "For example, there are different deployments with different options each. Pydantic is then trying \n"
                    + "one after the other and reporting the failures for each of them.\n\n[/red]"
                    + str(e),
                )
            )
            msg = "Invalid configuration. Please check the above output."
            raise RuntimeError(msg) from None
        if cli_args.print_config:  # type: ignore
            print(yaml.dump(args.model_dump()))
            exit(0)

        args._config_files = config_files  # type: ignore
        return args


def save_predictions(traj_dir: Path, instance_id: str, result: AgentRunResult):
    """Save predictions in a file readable by SWE-bench"""
    output_file = traj_dir / (instance_id + ".pred")
    datum = {
        "model_name_or_path": traj_dir.name,
        "instance_id": instance_id,
        "model_patch": result.info.get("submission"),
    }
    output_file.write_text(json.dumps(datum))


def _is_promising_patch(info: AgentInfo) -> bool:
    """Do we actually believe that the patch will solve the issue?
    Or are we just submitting the last patch we generated before hitting an error?
    """
    # The exit status can also be `submitted (exit_cost)` etc.
    return info.get("exit_status") == "submitted" and info.get("submission") is not None
