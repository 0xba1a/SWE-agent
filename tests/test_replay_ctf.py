from __future__ import annotations

import pytest

from run_replay import get_args, main


@pytest.mark.slow
@pytest.mark.parametrize("traj_rel_path", ["pwn/warmup.traj"])
def test_ctf_traj_replay(test_ctf_trajectories_path, traj_rel_path):
    traj_path = test_ctf_trajectories_path / traj_rel_path
    assert traj_path.is_file()
    args = [
        "--traj_path",
        str(traj_path),
        "--config_file",
        "config/default_ctf.yaml",
        "--raise_exceptions",
    ]
    args, remaining_args = get_args(args)
    main(**vars(args), forward_args=remaining_args)
