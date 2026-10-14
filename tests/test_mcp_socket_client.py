"""Tests for terminator_mcp.socket_client and the capture timeout plumbing."""

import socket
import threading

import pytest

from terminator_mcp import socket_client


def _silent_bridge(tmp_path, monkeypatch):
    """A bridge socket that accepts but never replies."""
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    path = socket_client.socket_path()
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    held = []

    def accept():
        conn, _ = server.accept()
        held.append(conn)

    threading.Thread(target=accept, daemon=True).start()
    return server, held


def test_recv_timeout_is_a_bridge_error(tmp_path, monkeypatch):
    server, held = _silent_bridge(tmp_path, monkeypatch)
    try:
        with pytest.raises(socket_client.BridgeError) as info:
            socket_client.call('ping', {}, timeout=0.2)
        assert info.value.code == 'timeout'
    finally:
        for conn in held:
            conn.close()
        server.close()


def test_capture_socket_outwaits_requested_timeout(monkeypatch):
    server = pytest.importorskip('terminator_mcp.server')
    seen = {}

    def fake_call(method, args=None, timeout=socket_client.DEFAULT_TIMEOUT):
        seen['timeout'] = timeout
        return {}

    monkeypatch.setattr(server.socket_client, 'call', fake_call)
    server._execute_capture('urn:uuid:x', 'true', 60000)
    assert seen['timeout'] == 60.0 + server.CAPTURE_SOCKET_MARGIN_S


def test_layout_tools_take_no_command():
    server = pytest.importorskip('terminator_mcp.server')
    import inspect
    assert 'command' not in inspect.signature(server.new_tab).parameters
    assert 'command' not in inspect.signature(server.split).parameters
    assert not hasattr(server, 'open_workspace')
