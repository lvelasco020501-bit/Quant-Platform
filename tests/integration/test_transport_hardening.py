"""Proof, not inference: the transport cannot block indefinitely on this machine.

Every assertion in :mod:`quantplatform.marketdata.feed`'s docstrings about being "hardened
against an indefinite block" is a claim about what the vendored ``websockets`` library does
internally, reasoned from reading its source. Reasoning about a dependency's internals is
not proof that a fix works on a real socket, on this real operating system, against a peer
that genuinely never sends another byte and never sends a FIN or an RST — the precise
condition believed to have caused an eight-hour stall. This file reproduces that condition
with a real TCP server the test controls, and measures real wall-clock time.

**The double is a raw socket, not another instance of the client under test.** A
``websockets`` server double would gracefully participate in the closing handshake if its
handler ever returned, which is exactly the cooperative behaviour a black hole does not
have. The server here completes the WebSocket opening handshake by hand — computing the
``Sec-WebSocket-Accept`` header itself, the one piece of protocol logic actually needed —
and then does nothing else at all: never reads again, never writes again, never closes.
That is what makes the silence real rather than simulated.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import suppress

import pytest

from quantplatform.core.errors import MarketDataConnectionError
from quantplatform.marketdata.feed import WebSocketCandleTransport

_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
"""RFC 6455's fixed magic string, mixed into the client's key to prove the response came
from a real WebSocket-aware peer rather than an ordinary HTTP server echoing the request."""

_ACCEPT_TIMEOUT_SECONDS = 5.0
"""How long the test server waits for the one connection it expects. Generous for a
loopback accept, and irrelevant to anything this file is trying to prove."""


class _BlackHoleServer:
    """A real TCP peer that completes the WebSocket handshake once, then goes silent.

    No FIN, no RST, no further bytes in either direction after the handshake response —
    exactly the condition that leaves a raw ``socket.recv()`` blocked with nothing to
    return, forever, on a socket with no timeout of its own.
    """

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(_ACCEPT_TIMEOUT_SECONDS)
        self._accepted: socket.socket | None = None
        self._thread = threading.Thread(target=self._serve_once, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """Return the ``ws://`` URL the transport under test should connect to."""
        host, port = self._listener.getsockname()
        return f"ws://{host}:{port}"

    def _serve_once(self) -> None:
        """Accept the one connection this server expects and answer its handshake.

        Deliberately nothing further after that: no read, no write, no close. The
        connection is held open and silent for the rest of this server's life.
        """
        try:
            connection, _ = self._listener.accept()
        except OSError:
            return
        self._accepted = connection
        try:
            request = self._read_http_request(connection)
            key = _extract_websocket_key(request)
            if key is not None:
                connection.sendall(_handshake_response(key))
            # Deliberately nothing further: no read, no write, no close. The connection
            # is simply held open and abandoned, which is the whole point.
        except OSError:
            pass

    @staticmethod
    def _read_http_request(connection: socket.socket) -> bytes:
        """Read until the blank line that ends an HTTP request's headers."""
        buffer = b""
        connection.settimeout(_ACCEPT_TIMEOUT_SECONDS)
        while b"\r\n\r\n" not in buffer:
            chunk = connection.recv(4096)
            if not chunk:
                break
            buffer += chunk
        return buffer

    def close(self) -> None:
        """Tear down the server side.

        Never called before a test has taken its measurement — closing early would turn
        the black hole into an ordinary, clean disconnect and defeat the entire point of
        this double.
        """
        for sock in (self._accepted, self._listener):
            if sock is None:
                continue
            with suppress(OSError):
                sock.close()


def _extract_websocket_key(request: bytes) -> str | None:
    """Pull ``Sec-WebSocket-Key`` out of a raw HTTP upgrade request."""
    for line in request.split(b"\r\n"):
        if line.lower().startswith(b"sec-websocket-key:"):
            return line.split(b":", 1)[1].strip().decode("ascii")
    return None


def _handshake_response(key: str) -> bytes:
    """Build the ``101 Switching Protocols`` response RFC 6455 requires."""
    accept = base64.b64encode(hashlib.sha1((key + _WEBSOCKET_GUID).encode()).digest())  # noqa: S324
    return (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
    )


@pytest.fixture
def black_hole() -> Iterator[_BlackHoleServer]:
    server = _BlackHoleServer()
    try:
        yield server
    finally:
        server.close()


# --- The proof ------------------------------------------------------------------------------


def test_close_returns_within_the_hard_ceiling_against_a_real_black_hole(
    black_hole: _BlackHoleServer,
) -> None:
    # The actual incident, reproduced: a real socket, on this real machine, connected to a
    # peer that completes the handshake and then never sends another byte, never closes.
    transport = WebSocketCandleTransport(
        open_timeout_seconds=5.0, close_timeout_seconds=2.0, hard_close_timeout_seconds=6.0
    )
    transport.connect(black_hole.url)
    assert transport.is_connected

    started = time.monotonic()
    transport.close()
    elapsed = time.monotonic() - started

    # Bounded, not merely "eventually": the backstop's own ceiling, plus headroom for
    # scheduling jitter on a loaded CI machine -- never anything approaching minutes,
    # let alone the hours the unbounded version of this call could have taken.
    assert elapsed < 6.0 + 5.0
    assert not transport.is_connected


def test_the_graceful_attempt_genuinely_runs_before_the_backstop_fires(
    black_hole: _BlackHoleServer,
) -> None:
    # Proves the backstop is a backstop and not a shortcut that skips straight past the
    # library's own close attempt: elapsed time should be at least in the neighbourhood of
    # close_timeout_seconds, not near-instant.
    transport = WebSocketCandleTransport(
        open_timeout_seconds=5.0, close_timeout_seconds=1.0, hard_close_timeout_seconds=4.0
    )
    transport.connect(black_hole.url)

    started = time.monotonic()
    transport.close()
    elapsed = time.monotonic() - started

    assert elapsed >= 0.9  # a hair under close_timeout_seconds, allowing for jitter
    assert elapsed < 4.0 + 5.0


def test_repeated_receive_calls_survive_a_black_hole_within_bounded_time(
    black_hole: _BlackHoleServer,
) -> None:
    # receive() itself must return -- not raise unboundedly slowly, not hang -- within
    # its own timeout against a peer that will never send a frame.
    transport = WebSocketCandleTransport(open_timeout_seconds=5.0, close_timeout_seconds=2.0)
    transport.connect(black_hole.url)

    started = time.monotonic()
    result = transport.receive(1.0)
    elapsed = time.monotonic() - started

    assert result is None  # a clean timeout, not a frame -- the peer never sent one
    assert elapsed < 3.0

    transport.close()


def test_a_connection_that_dies_after_receive_still_closes_boundedly(
    black_hole: _BlackHoleServer,
) -> None:
    # The exact shape of the original incident: the connection is healthy long enough to
    # be polled at least once, then goes silent for good, and a reconnect attempt (close()
    # first, per _reconnect()'s own sequence) must still return.
    transport = WebSocketCandleTransport(
        open_timeout_seconds=5.0, close_timeout_seconds=2.0, hard_close_timeout_seconds=6.0
    )
    transport.connect(black_hole.url)
    transport.receive(0.5)  # times out cleanly; the peer sends nothing further, ever
    transport.receive(0.5)

    started = time.monotonic()
    transport.close()
    elapsed = time.monotonic() - started

    assert elapsed < 6.0 + 5.0


# --- The mechanism, verified directly ---------------------------------------------------------


def test_the_raw_socket_is_bound_immediately_on_connect(black_hole: _BlackHoleServer) -> None:
    # The fix's own precondition: by the time connect() returns, the socket already carries
    # a finite timeout, closing the gap between connecting and the first receive() call.
    transport = WebSocketCandleTransport(open_timeout_seconds=5.0)

    transport.connect(black_hole.url)

    connection = transport._connection
    assert connection is not None
    bound = connection.socket.gettimeout()
    assert bound is not None
    assert bound > 0

    transport.close()


def test_the_raw_socket_timeout_tracks_each_receive_call(black_hole: _BlackHoleServer) -> None:
    transport = WebSocketCandleTransport(open_timeout_seconds=5.0)
    transport.connect(black_hole.url)

    transport.receive(2.0)
    connection = transport._connection
    assert connection is not None
    first = connection.socket.gettimeout()

    transport.receive(9.0)
    second = connection.socket.gettimeout()

    assert first is not None
    assert second is not None
    assert second > first  # the bound moved with the caller's own requested timeout

    transport.close()


def test_a_connection_error_is_raised_not_swallowed(black_hole: _BlackHoleServer) -> None:
    # Hardening must not come at the cost of hiding a real failure: closing the server's
    # accepted socket out from under the transport must still surface as the documented
    # error, not disappear.
    transport = WebSocketCandleTransport(open_timeout_seconds=5.0, close_timeout_seconds=2.0)
    transport.connect(black_hole.url)
    black_hole.close()  # the peer is gone; the next read must fail, not hang or lie

    with pytest.raises(MarketDataConnectionError):
        _receive_until_error(transport)

    transport.close()


def _receive_until_error(transport: WebSocketCandleTransport) -> None:
    """Poll a transport until it raises, bounded so a broken fix fails fast, not by hanging."""
    for _ in range(50):
        if transport.receive(0.2) is not None:
            return
