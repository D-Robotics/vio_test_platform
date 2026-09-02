"""Unit tests for boards.is_valid_ip."""
import sys, os

# make `server` importable (tests live in server/tests, repo root is server/..)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from server import boards


def test_valid_ipv4():
    assert boards.is_valid_ip("192.168.1.10") is True
    assert boards.is_valid_ip("10.0.0.1") is True
    assert boards.is_valid_ip("255.255.255.255") is True
    assert boards.is_valid_ip("0.0.0.0") is True


def test_valid_ipv6():
    assert boards.is_valid_ip("::1") is True
    assert boards.is_valid_ip("2001:db8::1") is True


def test_invalid_ipv4():
    assert boards.is_valid_ip("111111.1111.11111.111") is False
    assert boards.is_valid_ip("256.1.1.1") is False
    assert boards.is_valid_ip("1.2.3") is False
    assert boards.is_valid_ip("1.2.3.4.5") is False
    assert boards.is_valid_ip("foo") is False
    assert boards.is_valid_ip("") is False
