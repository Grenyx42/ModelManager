import socket

import pytest

from modelmanager.errors import PortInUseError
from modelmanager.port_check import is_local_port_free, resolve_port


def test_resolve_port_returns_preferred_when_free():
    assert resolve_port(lambda port: True, 9000, "sleek") == 9000


def test_resolve_port_scans_upward_when_preferred_is_taken():
    taken = {9000, 9001}
    assert resolve_port(lambda port: port not in taken, 9000, "sleek") == 9002


def test_resolve_port_raises_when_scan_range_exhausted():
    with pytest.raises(PortInUseError, match="9000"):
        resolve_port(lambda port: False, 9000, "sleek")


def test_is_local_port_free_detects_a_real_bound_port():
    occupying_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupying_socket.bind(("0.0.0.0", 0))
    occupying_socket.listen(1)
    port = occupying_socket.getsockname()[1]
    try:
        assert is_local_port_free(port) is False
    finally:
        occupying_socket.close()


def test_is_local_port_free_true_for_an_unused_high_port():
    occupying_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupying_socket.bind(("0.0.0.0", 0))
    port = occupying_socket.getsockname()[1]
    occupying_socket.close()  # free it again - nothing should be listening now

    assert is_local_port_free(port) is True
