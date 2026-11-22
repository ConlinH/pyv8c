"""Network clients must report missing dependencies before opening sockets."""
import pytest

from pyv8c.network.models import NetworkUnavailableError
from pyv8c.network import websockets as transport


def test_websocket_transport_requires_client_at_construction(monkeypatch):
    def missing():
        raise NetworkUnavailableError('websockets client missing')

    monkeypatch.setattr(transport, '_connect_client', missing)
    with pytest.raises(NetworkUnavailableError, match='client missing'):
        transport.WebSocketsTransport()
