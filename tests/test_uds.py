import pytest

from ic204amg import uds

H = bytes.fromhex
AMG = uds.Window(0x01D0, 8)
LAMP = uds.Window(0x0260, 10)


def test_runbook_commands():
    assert uds.read_memory(AMG) == H("2314100001D008")
    assert uds.read_memory(LAMP) == H("2314100002600A")
    assert uds.write_memory(AMG, H("FF00000000000000")) == H("3D14100001D008FF00000000000000")
    assert uds.write_memory(LAMP, bytes(9) + b"\x60") == H("3D14100002600A") + bytes(9) + b"\x60"
    assert uds.extended_session() == H("1003")
    assert uds.request_seed(0x09) == H("2709")
    assert uds.send_key(0x09, H("D8F169D68D5D17B6")) == H("270AD8F169D68D5D17B6")
    assert uds.ecu_reset() == H("1101")
    assert uds.tester_present() == H("3E80")


def test_write_memory_checks_length():
    with pytest.raises(ValueError, match="takes 8"):
        uds.write_memory(AMG, bytes(7))


def test_key_length():
    with pytest.raises(ValueError):
        uds.send_key(0x09, bytes(7))


def test_check_no_response():
    with pytest.raises(uds.NoResponseError):
        uds.check(H("1003"), None)
    with pytest.raises(uds.NoResponseError):
        uds.check(H("1003"), b"")


def test_check_negative():
    with pytest.raises(uds.NegativeResponseError, match="invalid key") as info:
        uds.check(H("270A"), H("7F2735"))
    assert info.value.nrc == 0x35


def test_check_positive_and_wrong_service():
    assert uds.check(H("1003"), H("500300320064")) == H("500300320064")
    with pytest.raises(uds.UnexpectedResponseError):
        uds.check(H("1003"), H("6709"))


def test_is_for_matches_only_this_request():
    req = uds.read_memory(AMG)
    assert uds.is_for(req, H("63FC"))
    assert uds.is_for(req, H("7F2378"))
    assert not uds.is_for(req, H("7E80"))
    assert not uds.is_for(req, H("7F3E12"))
    assert not uds.is_for(req, H("7F"))


def test_pending():
    assert uds.is_pending(H("7F2378"))
    assert not uds.is_pending(H("7F2335"))
    assert not uds.is_pending(H("7F23"))


def test_session_timing():
    assert uds.session_timing(H("5003003201F4")) == (0.05, 5.0)
    assert uds.session_timing(H("5003")) == (0.0, 0.0)


def test_memory_data_strict():
    assert uds.memory_data(AMG, H("63FC01020304050607")) == H("FC01020304050607")
    with pytest.raises(uds.UnexpectedResponseError):
        uds.memory_data(AMG, H("63FC010203040506"))
    with pytest.raises(uds.UnexpectedResponseError):
        uds.memory_data(AMG, H("63FC0102030405060708"))


def test_write_echo():
    req = uds.write_memory(AMG, bytes(8))
    uds.check_write_echo(req, H("7D14100001D008"))
    with pytest.raises(uds.UnexpectedResponseError):
        uds.check_write_echo(req, H("7D14100001D009"))
