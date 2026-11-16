import json
import logging

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
    with pytest.raises(cl.Stop, match="not FF/FC"):
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


def scripted(*answers):
    it = iter(answers)
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return next(it)

    return ask, prompts


def lamp_writes(t):
    return [r[-1] for r in t.sent if r[0] == uds.SID_WRITE_MEMORY]


def lampbits(c, backup_path, *answers):
    ask, prompts = scripted(*answers)
    sleeps = []
    cl.run_lampbits(c, backup_path, ask, sleep=sleeps.append)
    return prompts, sleeps


def test_lampbits_iterates_with_reset_until_stopped(backup_path, caplog):
    caplog.set_level(logging.INFO)
    ecu = FakeIc204()
    t, c = connect(ecu)
    answers = ("0110 0000", "", "preheat ok, fog on", "00100000", "", "", "q")
    prompts, sleeps = lampbits(c, backup_path, *answers)
    assert bytes(ecu.mem[LAMP]) == H("00112233445566778820")
    assert bytes(ecu.mem[AMG]) == H(AMG0)
    assert lamp_writes(t) == [0x60, 0x20]
    assert ecu.resets == 2
    assert sleeps == [cl.RESET_SETTLE, cl.RESET_SETTLE]
    assert "at start: 10011001 (99)" in prompts[-1]
    assert "current:  00100000 (20)" in prompts[-1]
    assert "NOTE 01100000 (60): preheat ok, fog on" in caplog.text
    assert "tried 60 20, now 00100000 (20)" in caplog.text
    assert f"restore {backup_path} --lamp" in caplog.text


def test_lampbits_survives_an_ignition_cycle_while_a_prompt_waits(backup_path):
    ecu = FakeIc204()
    t, c = connect(ecu)
    answers = iter(("01100000", "", "q"))

    def ask(_prompt):
        ecu.session, ecu.unlocked = 1, False
        return next(answers)

    cl.run_lampbits(c, backup_path, ask, sleep=lambda _seconds: None)
    assert lamp_writes(t) == [0x60]


def test_lampbits_correction_replaces_value_before_write(backup_path):
    ecu = FakeIc204()
    t, c = connect(ecu)
    prompts, _ = lampbits(c, backup_path, "01100000", "00100000", "", "q")
    assert lamp_writes(t) == [0x20]
    assert "new:      00100000 (20)" in prompts[2]
    assert ecu.resets == 1


def test_lampbits_bit_number_flips_current_then_the_pending_value(backup_path):
    ecu = FakeIc204()
    t, c = connect(ecu)
    prompts, _ = lampbits(c, backup_path, "5", "0", "", "q")
    assert "new:      10111001 (B9)" in prompts[1]
    assert lamp_writes(t) == [0xB8]


@pytest.mark.parametrize(
    "answers",
    [("012", "8", "1001100", "q"), ("01100000", "q"), ("10011001", "q"), ("", "", "q")],
)
def test_lampbits_writes_nothing(backup_path, caplog, answers):
    caplog.set_level(logging.INFO)
    ecu = FakeIc204()
    t, c = connect(ecu)
    lampbits(c, backup_path, *answers)
    assert uds.SID_WRITE_MEMORY not in sids(t)
    assert ecu.resets == 0
    assert "nothing written" in caplog.text


def test_lampbits_stop_at_confirm_after_a_write_keeps_it(backup_path):
    ecu = FakeIc204()
    t, c = connect(ecu)
    lampbits(c, backup_path, "01100000", "", "", "00100000", "q")
    assert lamp_writes(t) == [0x60]
    assert ecu.mem[LAMP][-1] == 0x60


def test_lampbits_back_at_start_gives_no_restore_hint(backup_path, caplog):
    caplog.set_level(logging.INFO)
    _, c = connect(FakeIc204())
    lampbits(c, backup_path, "01100000", "", "", "10011001", "", "q")
    assert "tried 60 99, now 10011001 (99)" in caplog.text
    assert "restore" not in caplog.text


def test_lampbits_reports_a_value_the_cluster_did_not_keep(backup_path, caplog):
    ecu = FakeIc204()

    def reverting(req):
        out = ecu(req)
        if req[0] == uds.SID_RESET:
            ecu.mem[LAMP][-1] = 0x99
        return out

    c = cl.Cluster(FakeEcu(reverting), SW)
    c.extended_session()
    prompts, _ = lampbits(c, backup_path, "01100000", "", "", "q")
    assert "did not keep it" in caplog.text
    assert "current:  10011001 (99)" in prompts[-1]


def test_lampbits_summary_survives_an_error_after_a_write(backup_path, caplog):
    caplog.set_level(logging.INFO)
    ecu = FakeIc204()

    def reset_reply_lost(req):
        out = ecu(req)
        return [] if req[0] == uds.SID_RESET else out

    c = cl.Cluster(FakeEcu(reset_reply_lost), SW, p2=0.01)
    c.extended_session()
    with pytest.raises(uds.NoResponseError):
        lampbits(c, backup_path, "01100000", "")
    assert "tried 60, now 01100000 (60)" in caplog.text
    assert f"restore {backup_path} --lamp" in caplog.text


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
