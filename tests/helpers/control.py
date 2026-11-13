"""Test-only protocol peer. Production integrations have their own connection lifecycle."""

from __future__ import annotations

import uuid
from pathlib import Path

from maa_planner.endpoint import default_endpoint
from maa_planner.schema import Request, Response


async def send_message(path, message):
    from maa_planner.client import ControlClient

    async with ControlClient(path) as client:
        return await client.request(message)


class ApiPeer:
    def __init__(self, endpoint_path: Path | None = None):
        self.endpoint_path = endpoint_path or default_endpoint()

    async def call(self, method: str, **params):
        value = await send_message(self.endpoint_path, Request(method, params).to_dict())
        return Response.from_dict(value).unwrap()

    async def start(
        self, method, *, profile=None, parameters=None, idempotency_key=None, latest_start_at=None
    ):
        params = {"idempotency_key": idempotency_key or uuid.uuid4().hex, **(parameters or {})}
        if profile is not None:
            params["profile"] = profile
        if latest_start_at is not None:
            params["latest_start_at"] = latest_start_at
        return await self.call(method, **params)

    async def replace_config(self, toml: str, *, expected_revision: str | None = None):
        return await self.call("config.replace", toml=toml, expected_revision=expected_revision)
