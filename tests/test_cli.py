import pytest

from ic204amg import cli
from ic204amg import cluster as cl
from ic204amg import profile as pf


def parse(*argv):
    return cli.build_parser().parse_args(list(argv))


def test_settings_cli_overrides_config(tmp_path):
    cfg = tmp_path / "car.toml"
    cfg.write_text(
        '[cluster]\ntxid = 0x60A\nrxid = 0x481\nsw = "2049022903"\n[openport]\ndll = "x.dll"\n'
    )
    s = cli.settings(parse("--config", str(cfg), "--rxid", "0x482", "probe"))
    assert s == cli.Settings(
        sw="2049022903", dll="x.dll", baud=500000, profile=pf.W204_COLOUR, txid=0x60A, rxid=0x482
    )


def test_profile_defaults_to_w204_colour_without_amg_section(tmp_path):
    cfg = tmp_path / "car.toml"
    cfg.write_text("[cluster]\ntxid = 0x60A\nrxid = 0x481\n")
    assert cli.settings(parse("--config", str(cfg), "probe")).profile is pf.W204_COLOUR


def test_profile_from_config(tmp_path):
    cfg = tmp_path / "car.toml"
    cfg.write_text(
        "[cluster]\ntxid = 0x60A\nrxid = 0x481\n"
        "[amg]\noffset = 0x0100\nlength = 4\nbyte = 1\noff = 0x00\non = 0x01\n"
    )
    p = cli.settings(parse("--config", str(cfg), "probe")).profile
    assert (p.amg.offset, p.amg.length, p.rule.byte, p.rule.on) == (0x0100, 4, 1, 0x01)
    assert p.lamp is None


def test_bad_profile_is_a_usage_error(tmp_path):
    cfg = tmp_path / "car.toml"
    cfg.write_text("[amg]\noffset = 0x0100\nlength = 4\nbyte = 9\noff = 0x00\non = 0x01\n")
    with pytest.raises(cli.UsageError, match="profile"):
        cli.settings(parse("--config", str(cfg), "probe"))


def test_missing_ids_is_a_usage_error(tmp_path):
    s = cli.settings(parse("--config", str(tmp_path / "none.toml"), "probe"))
    with pytest.raises(cli.UsageError, match="txid/rxid"):
        s.can_ids()


def test_unknown_sw_fails_before_touching_anything(tmp_path):
    logs = tmp_path / "logs"
    argv = ["--config", str(tmp_path / "none.toml"), "--log-dir", str(logs)]
    assert cli.main([*argv, "--sw", "2049022803", "probe"]) == 1
    assert not logs.exists()


def test_selftest_runs_without_cable(tmp_path):
    argv = ["--config", str(tmp_path / "none.toml"), "--sw", "2049022903"]
    assert cli.main([*argv, "selftest"]) == 0


def test_selftest_fails_without_sw(tmp_path):
    assert cli.main(["--config", str(tmp_path / "none.toml"), "selftest"]) == 1


def test_missing_sw_is_a_usage_error_before_the_cable(tmp_path):
    argv = ["--config", str(tmp_path / "none.toml"), "--log-dir", str(tmp_path / "logs")]
    # without the check this would reach J2534Transport.open() and come back as 3
    assert cli.main([*argv, "--txid", "0x60A", "--rxid", "0x481", "readonly"]) == 1


@pytest.mark.parametrize(
    ("volts", "required", "stops"),
    [(None, True, True), (12.4, True, True), (12.4, False, False), (13.5, True, False)],
)
def test_check_battery(volts, required, stops):
    if stops:
        with pytest.raises(cl.Stop):
            cli.check_battery(volts, required)
    else:
        cli.check_battery(volts, required)


def test_write_level_flag_parses_hex():
    assert parse("write").write_level == "0x0D"
    assert int(parse("write", "--write-level", "0x09").write_level, 16) == 0x09


def test_write_level_flag_rejects_unlisted_value():
    with pytest.raises(SystemExit):
        parse("write", "--write-level", "0x05")
