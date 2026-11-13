import json

import pytest

from maa_planner.daemon.execution.png import MAX_PNG_BYTES, decode_png, encode_png, validate_png
from maa_planner.daemon.execution.tasks import CoreTasks
from tests.helpers.png import PNG


@pytest.mark.parametrize(
    "data", [b"", PNG[:-1], PNG + b"x", PNG[:30] + b"x" + PNG[31:], b"x" * (MAX_PNG_BYTES + 1)]
)
def test_corrupt_and_unbounded_png_rejected(data):
    with pytest.raises(ValueError):
        validate_png(data)


@pytest.mark.parametrize(
    "name,reference,files",
    [
        ("../outside.png", "@file:test.png", {"test.png": encode_png(PNG)}),
        ("Home.png", "@file:test.png", {"test.png": encode_png(PNG)}),
        ("Planner@Test.png", "/tmp/test.png", {}),
        ("Planner@Test.png", "@file:missing.png", {}),
        ("Planner@Test.png", "@file:test.png", {"test.png": "%%%"}),
    ],
)
def test_image_requests_cannot_escape_snapshot_or_replace_shared_names(name, reference, files):
    with pytest.raises(ValueError):
        CoreTasks(
            json.dumps({"tasks": [{"type": "Custom"}], "resource_images": {name: reference}}),
            json.dumps(files),
        )


def test_roundtrip_png():
    assert decode_png(encode_png(PNG)) == PNG
