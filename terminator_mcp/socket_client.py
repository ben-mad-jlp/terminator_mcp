"""Length-prefixed JSON client for the Terminator MCP bridge socket.

Mirrors the wire protocol and socket-path convention defined in
terminatorlib/plugins/mcp_bridge.py: a per-user Unix-domain socket, 4-byte
big-endian length prefix + UTF-8 JSON body, one request frame -> one response
frame per connection.

The client is connection-per-request (cheap on a local socket) and stateless.
It never launches Terminator; if the socket is absent or refused it returns a
structured {"error": "terminator_not_running"} so tools degrade cleanly.
"""

import os
import json
import socket
import struct

HEADER = struct.Struct('>I')
MAX_FRAME = 16 * 1024 * 1024
DEFAULT_TIMEOUT = 10.0


def socket_path():
    """Absolute path to the bridge socket — identical convention to the plugin."""
    base = os.environ.get('XDG_RUNTIME_DIR') or os.environ.get('TMPDIR') \
        or '/tmp'
    return os.path.join(base, 'terminator-mcp-%d' % os.getuid(), 'bridge.sock')


class BridgeError(Exception):
    """Raised when the bridge returns {ok:false} or is unreachable."""

    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def _recv_exact(sock, n):
    chunks = []
    remaining = n
    while remaining > 0:
        data = sock.recv(remaining)
        if not data:
            return None
        chunks.append(data)
        remaining -= len(data)
    return b''.join(chunks)


def call(method, args=None, timeout=DEFAULT_TIMEOUT):
    """Send one request, return the parsed `result`. Raises BridgeError.

    The notable error code is "terminator_not_running" (socket missing or the
    connection was refused) — callers should surface it rather than crash.
    """
    path = socket_path()
    payload = json.dumps({'method': method, 'args': args or {}}).encode('utf-8')
    if len(payload) > MAX_FRAME:
        raise BridgeError('request_too_large')

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(path)
        except (FileNotFoundError, ConnectionRefusedError):
            raise BridgeError('terminator_not_running',
                              'bridge socket not available at %s' % path)
        except socket.timeout:
            raise BridgeError('timeout', 'connecting to bridge timed out')

        try:
            sock.sendall(HEADER.pack(len(payload)))
            sock.sendall(payload)

            header = _recv_exact(sock, HEADER.size)
            if header is None:
                raise BridgeError('connection_closed')
            (length,) = HEADER.unpack(header)
            if length <= 0 or length > MAX_FRAME:
                raise BridgeError('bad_frame_length')
            body = _recv_exact(sock, length)
            if body is None:
                raise BridgeError('connection_closed')
        except socket.timeout:
            raise BridgeError('timeout',
                              'no reply from bridge within %.1fs' % timeout)
    finally:
        sock.close()

    try:
        response = json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise BridgeError('invalid_response')

    if not response.get('ok'):
        raise BridgeError(response.get('error', 'unknown_error'))
    return response.get('result')


def ping(timeout=DEFAULT_TIMEOUT):
    """Liveness check. Returns the ping result dict, or raises BridgeError."""
    return call('ping', {}, timeout=timeout)
