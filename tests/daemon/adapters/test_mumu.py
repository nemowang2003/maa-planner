from pathlib import Path

import pytest

from maa_planner.daemon.adapters.mumu import Instance, match_profiles
from maa_planner.daemon.config import Profile


def test_matching_uses_names_and_rejects_missing_or_duplicate_instances():
    profiles = (Profile("nemo", Path("nemo")), Profile("wyf", Path("wyf")))
    instances = [Instance(3, "wyf", False, None), Instance(1, "nemo", True, "127.0.0.1:16416")]
    assert {p.name: i.index for p, i in match_profiles(profiles, instances).items()} == {
        "nemo": 1,
        "wyf": 3,
    }
    with pytest.raises(ValueError, match="唯一"):
        match_profiles(profiles, instances[:1])
    with pytest.raises(ValueError, match="唯一"):
        match_profiles(profiles, instances + [Instance(4, "NEMO", False, None)])
