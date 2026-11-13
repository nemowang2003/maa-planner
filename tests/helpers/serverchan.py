"""Non-production ServerChan3 route fixture, used only for offline validation."""

import json


def config_file(tmp_path):
    path = tmp_path / "serverchan.json"
    path.write_text(
        json.dumps({"nemo": ["https://123.push.ft07.com/send/sctp123tPRIVATE_KEY.send"]})
    )
    return path
