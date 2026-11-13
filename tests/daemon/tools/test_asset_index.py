import pytest

from maa_planner.daemon.tools.asset_index import AssetIndex, dependency_closure
from tests.helpers.asset_index import index_bytes


def test_dependency_positions_are_bundle_vector_positions_and_cycles_terminate():
    graph = AssetIndex(
        index_bytes(
            [("activity/root.ab", [1, 2]), ("common/base.ab", [0]), ("anon/texture.bin", [])]
        )
    ).dependencies()
    assert graph["activity/root.ab"] == ("common/base.ab", "anon/texture.bin")
    assert dependency_closure(graph, ["activity/root.ab"]) == [
        "activity/root.ab",
        "common/base.ab",
        "anon/texture.bin",
    ]
    with pytest.raises(ValueError, match="未包含"):
        dependency_closure(graph, ["unknown.ab"])


@pytest.mark.parametrize(
    "data",
    [
        b"\0" * 170,
        index_bytes([("../outside.ab", [])]),
        index_bytes([("one.ab", [4])]),
        index_bytes([("one.ab", []), ("ONE.ab", [])]),
    ],
)
def test_corrupt_offsets_paths_and_dependencies_fail_closed(data):
    with pytest.raises(ValueError):
        AssetIndex(data).dependencies()
