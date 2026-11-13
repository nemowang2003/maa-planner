import pytest

from maa_planner.interfaces.wecom.commands import parse_command


@pytest.mark.parametrize(
    "text,method,params",
    [
        ("/status", "daemon.status", {}),
        ("/alerts 测试账号", "alert.list", {"profile": "测试账号"}),
        ("/get abc", "operation.get", {"operation_id": "abc"}),
        ("/cancel abc", "operation.cancel", {"operation_id": "abc"}),
    ],
)
def test_explicit_commands(text, method, params):
    request = parse_command(text, bot_id="bot", message_id="message")
    assert request is not None
    assert (request.method, request.params) == (method, params)


def test_api_generates_stable_operation_id_without_overriding_explicit_ids():
    text = '/api {"method": "activity.fetch", "params": {}}'
    first = parse_command(text, bot_id="bot", message_id="one")
    same = parse_command(text, bot_id="bot", message_id="one")
    other_message = parse_command(text, bot_id="bot", message_id="two")
    other_bot = parse_command(text, bot_id="other", message_id="one")
    assert first is not None and other_message is not None and (other_bot is not None)
    assert first == same
    assert first.params["idempotency_key"] != other_message.params["idempotency_key"]
    assert first.params["idempotency_key"] != other_bot.params["idempotency_key"]
    explicit = parse_command(
        '/api {"method": "activity.fetch", "params": {"idempotency_key": "custom"}}',
        bot_id="bot",
        message_id="one",
    )
    assert explicit is not None
    assert explicit.params["idempotency_key"] == "custom"


@pytest.mark.parametrize(
    "text", ["/run daily", "/status extra", "/cancel", "/api []", "/api invalid"]
)
def test_invalid_commands_never_produce_a_request(text):
    with pytest.raises(ValueError):
        parse_command(text, bot_id="bot", message_id="one")
