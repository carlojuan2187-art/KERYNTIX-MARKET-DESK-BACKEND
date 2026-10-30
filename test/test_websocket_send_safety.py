from __future__ import annotations

import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect, WebSocketState

from api.routes.events import _try_send_websocket_json


class FakeWebSocket:
    def __init__(self, error: Exception | None = None) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED
        self.error = error
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(payload)


def run(coro):
    return asyncio.run(coro)


def test_websocket_frame_is_sent_when_transport_is_open():
    socket = FakeWebSocket()
    payload = {"channel": "system", "event": "heartbeat"}

    assert run(_try_send_websocket_json(socket, payload)) is True
    assert socket.sent == [payload]


@pytest.mark.parametrize(
    "error",
    [
        WebSocketDisconnect(code=1000),
        RuntimeError("Unexpected ASGI message 'websocket.send', after sending 'websocket.close'."),
        OSError("transport closed"),
    ],
)
def test_closed_websocket_transport_ends_sender_without_propagating(error: Exception):
    socket = FakeWebSocket(error=error)

    assert run(_try_send_websocket_json(socket, {"event": "tick"})) is False
    assert socket.sent == []


def test_websocket_already_marked_disconnected_is_not_sent_to():
    socket = FakeWebSocket()
    socket.client_state = WebSocketState.DISCONNECTED

    assert run(_try_send_websocket_json(socket, {"event": "tick"})) is False
    assert socket.sent == []
