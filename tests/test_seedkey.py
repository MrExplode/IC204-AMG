import pytest

from ic204amg import seedkey


@pytest.mark.parametrize(("sw", "level", "seed", "key"), seedkey.VECTORS)
def test_vectors(sw, level, seed, key):
    assert seedkey.ic204_generate_key(level, bytes.fromhex(seed), sw).hex().upper() == key


def test_former_aliases_are_separate_entries_with_equal_tables():
    assert seedkey.get_salt_table("2049026403") == seedkey.get_salt_table("2049022903")


def test_every_table_has_the_four_uds_levels():
    sw = seedkey.supported_sw()
    assert len(sw) == 83 and "2049022903" in sw and "2129029806" in sw
    for s in sw:
        table = seedkey.get_salt_table(s)
        assert set(table) == set(seedkey.UDS_TO_INTERNAL)
        assert all(len(salt) == 8 for salt in table.values())


def test_rejects_unknown_sw_level_and_seed_length():
    with pytest.raises(ValueError, match="no seed-key table"):
        seedkey.get_salt_table("2049022803")
    with pytest.raises(ValueError, match="level"):
        seedkey.ic204_generate_key(0x05, bytes(8), "2049022903")
    with pytest.raises(ValueError, match="8 bytes"):
        seedkey.ic204_generate_key(0x09, bytes(7), "2049022903")
