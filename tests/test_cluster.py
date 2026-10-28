import json

import pytest

from ic204amg import cluster as cl
from ic204amg import uds
from ic204amg.profile import W204_COLOUR as P
from tests.fakes import AMG, KEY, LAMP, SW, FakeEcu, FakeIc204

H = bytes.fromhex
AMG0, LAMP0 = "FC00112233445566", "00112233445566778899"


@pytest.fixture
def backup_path(tmp_path):
    return tmp_path / "backup.json"


def yes(_prompt, _token):
    return True


def no(_prompt, _token):
    return False


def connect(ecu, **kwargs):
    t = FakeEcu(ecu)
    c = cl.Cluster(t, SW, **kwargs)
    c.extended_session()
    return t, c


def sids(t):
    return [r[0] for r in t.sent]


def test_readonly_reads_both_windows_and_writes_nothing(backup_path):
    t, c = connect(FakeIc204())
    b = cl.run_readonly(c, backup_path)
    assert (b.amg, b.lamp) == (H(AMG0), H(LAMP0))
    assert sids(t) == [0x10, 0x27, 0x27, 0x23, 0x23]
    assert t.sent[2] == H("270A") + KEY
    loaded = cl.Backup.load(backup_path, P)
    assert (loaded.amg, loaded.lamp, loaded.meta["sw"]) == (b.amg, b.lamp, SW)


def test_session_reply_can_only_raise_timeouts():
    _, c = connect(FakeIc204())
    assert (c.p2, c.p2_star) == (cl.P2, 5.0)


def test_response_pending_is_waited_out_with_p2_star(backup_path):
    t, c = connect(FakeIc204(pending_reads=True))
    assert cl.run_readonly(c, backup_path).amg == H(AMG0)
    assert max(t.timeouts) > c.p2


def test_empty_and_unrelated_frames_are_skipped(backup_path):
    _, c = connect(FakeIc204(noise=[b"", H("7E80"), H("7F3E12")]))
    assert cl.run_readonly(c, backup_path).amg == H(AMG0)


def test_silence_is_no_response():
    with pytest.raises(uds.NoResponseError):
        cl.Cluster(FakeEcu(lambda _req: []), SW, p2=0.01).extended_session()


def test_write_fc_to_ff_keeps_other_bytes(backup_path):
    ecu = FakeIc204()
    _, c = connect(ecu)
    cl.run_write(c, backup_path, yes)
    assert bytes(ecu.mem[AMG]) == H("FF00112233445566")
    assert bytes(ecu.mem[LAMP]) == H(LAMP0)
    assert ecu.resets == 1
    assert cl.Backup.load(backup_path, P).amg == H(AMG0)


def test_write_reunlocks_immediately_before_writing(backup_path):
    ecu = FakeIc204()
    t, c = connect(ecu)
    cl.run_write(c, backup_path, yes)
    write_i = next(i for i, r in enumerate(t.sent) if r[0] == uds.SID_WRITE_MEMORY)
    assert t.sent[write_i - 1][:2] == H("270A")  # a fresh key is sent right before the write
    assert t.sent[write_i - 2][:2] == H("2709")


def test_write_ff_to_fc(backup_path):
    ecu = FakeIc204(amg="FF00112233445566")
    _, c = connect(ecu)
    cl.run_write(c, backup_path, yes)
    assert ecu.mem[AMG][0] == 0xFC


def test_write_level_reunlocks_at_the_given_level(backup_path):
    ecu = FakeIc204()
    t, c = connect(ecu)
    cl.run_write(c, backup_path, yes, write_level=0x0D)
    write_i = next(i for i, r in enumerate(t.sent) if r[0] == uds.SID_WRITE_MEMORY)
    assert t.sent[write_i - 2] == uds.request_seed(0x0D)  # fresh seed at the write level
    assert t.sent[write_i - 1][:2] == bytes([uds.SID_SECURITY, 0x0E])
    assert ecu.mem[AMG][0] == 0xFF  # read-unlock still 0x09, write still lands


@pytest.mark.parametrize("amg", ["4000112233445566", "1020000000000000"])
def test_unknown_byte0_stops_before_any_write(backup_path, amg):
    t, c = connect(FakeIc204(amg=amg))
    with pytest.raises(cl.Stop, match="not FC/FF"):
        cl.run_write(c, backup_path, yes)
    assert uds.SID_WRITE_MEMORY not in sids(t)
    assert backup_path.exists()


def test_declined_write_writes_nothing(backup_path):
    ecu = FakeIc204()
    t, c = connect(ecu)
    with pytest.raises(cl.Stop, match="declined"):
        cl.run_write(c, backup_path, no)
    assert uds.SID_WRITE_MEMORY not in sids(t)
    assert ecu.resets == 0


def test_expect_mismatch_stops(backup_path):
    t, c = connect(FakeIc204())
    earlier = cl.Backup(H("FC99999999999999"), bytes(10))
    with pytest.raises(cl.Stop, match="earlier backup"):
        cl.run_write(c, backup_path, yes, expect=earlier)
    assert uds.SID_WRITE_MEMORY not in sids(t)


def test_write_window_refuses_a_window_outside_the_profile():
    _, c = connect(FakeIc204())
    with pytest.raises(ValueError, match="only"):
        c.write_window(uds.Window(0x0212, 2), b"\xff\xff")


def test_readback_mismatch_raises_without_reset(backup_path):
    ecu = FakeIc204(drop_writes=True)
    _, c = connect(ecu)
    with pytest.raises(uds.UnexpectedResponseError, match="MISMATCH"):
        cl.run_write(c, backup_path, yes)
    assert ecu.resets == 0


def test_rejected_key_raises_and_reads_nothing(backup_path):
    t, c = connect(FakeIc204(accept_key=False))
    with pytest.raises(uds.NegativeResponseError) as info:
        cl.run_readonly(c, backup_path)
    assert info.value.nrc == 0x35
    assert uds.SID_READ_MEMORY not in sids(t)


def test_zero_seed_means_already_unlocked(backup_path):
    t, c = connect(FakeIc204(seed=bytes(8), unlocked=True))
    cl.run_readonly(c, backup_path)
    assert H("270A") not in [r[:2] for r in t.sent]


def test_missing_session_is_a_negative(backup_path):
    with pytest.raises(uds.NegativeResponseError) as info:
        cl.run_readonly(cl.Cluster(FakeEcu(FakeIc204()), SW), backup_path)
    assert info.value.nrc == 0x7F


def test_lampfix_sets_only_last_byte(backup_path):
    ecu = FakeIc204()
    _, c = connect(ecu)
    cl.run_lampfix(c, backup_path, yes)
    assert bytes(ecu.mem[LAMP]) == H("00112233445566778860")
    assert bytes(ecu.mem[AMG]) == H(AMG0)
    assert ecu.resets == 1


def test_lampfix_already_applied_writes_nothing(backup_path):
    ecu = FakeIc204(lamp="00112233445566778860")
    t, c = connect(ecu)
    cl.run_lampfix(c, backup_path, yes)
    assert uds.SID_WRITE_MEMORY not in sids(t)
    assert ecu.resets == 0


def test_restore_puts_backup_bytes_back(backup_path):
    ecu = FakeIc204(amg="FF00112233445566", lamp="00112233445566778860")
    _, c = connect(ecu)
    saved = cl.Backup(H(AMG0), H(LAMP0))
    cl.run_restore(c, saved, yes, lamp=True, backup_path=backup_path)
    assert (bytes(ecu.mem[AMG]), bytes(ecu.mem[LAMP])) == (saved.amg, saved.lamp)
    assert ecu.resets == 1


def test_restore_skips_windows_that_already_match(backup_path):
    ecu = FakeIc204(lamp="00112233445566778860")
    t, c = connect(ecu)
    cl.run_restore(c, cl.Backup(H(AMG0), H(LAMP0)), yes, lamp=True, backup_path=backup_path)
    writes = [r for r in t.sent if r[0] == uds.SID_WRITE_MEMORY]
    assert writes == [uds.write_memory(LAMP, H(LAMP0))]


def test_backup_rejects_wrong_window_length(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"0x01D0": "FC" * 9, "0x0260": "00" * 10}))
    with pytest.raises(ValueError, match="0x01D0 must be 8 bytes"):
        cl.Backup.load(bad, P)


def test_backup_rejects_missing_windows(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    with pytest.raises(ValueError, match="not a backup file"):
        cl.Backup.load(empty, P)
