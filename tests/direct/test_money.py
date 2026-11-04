"""Stakes: money enters only through stake_vow, and only behind your own vow.

Two rules drive this suite:

1. The promiser is the only account that may stake, because the same stake is
   what comes back on KEPT.
2. The one payable call never reverts once value is attached, so a refused
   target (a closed vow, somebody else's vow, a paused contract) becomes a
   withdrawable credit owned by the sender instead of stranded GEN.

Direct mode cannot execute external EthSend messages, so payouts themselves are
not exercised here. What is tested is what protects the money: who may stake,
where a refused deposit goes, and that the ledger stays balanced.
"""

PROMISE = "Ship v1.0 of the console to production."
EVIDENCE_URL = "https://evidence.example.com/releases/v1"
CRITERIA = "A published release tag on the page counts as shipped."


def _vow(direct_vm, direct_deploy, direct_owner, direct_alice, days=7):
    contract = direct_deploy("contracts/Vow.py")
    direct_vm.sender = direct_owner
    vow_id = contract.make_vow(
        PROMISE, EVIDENCE_URL, CRITERIA, days, direct_alice, "ref-1"
    )
    return contract, vow_id


def _balanced(contract):
    ledger = contract.get_ledger()
    return ledger["total_in"] == ledger["total_out"] + ledger["locked_total"]


def test_promiser_can_stake_behind_their_vow(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)

    direct_vm.value = 500
    assert contract.stake_vow(vow_id) == vow_id
    direct_vm.value = 0

    assert contract.get_vow(vow_id)["stake"] == 500

    ledger = contract.get_ledger()
    assert ledger["total_in"] == 500
    assert ledger["locked_total"] == 500
    assert ledger["total_out"] == 0
    assert _balanced(contract)

    assert contract.get_standing(direct_owner)["staked"] == 500


def test_stake_can_be_raised_before_settlement(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)

    direct_vm.value = 500
    contract.stake_vow(vow_id)
    direct_vm.value = 250
    contract.stake_vow(vow_id)
    direct_vm.value = 0

    assert contract.get_vow(vow_id)["stake"] == 750
    assert contract.get_ledger()["locked_total"] == 750
    assert _balanced(contract)


def test_zero_stake_is_refused(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)

    # No value is attached, so reverting here cannot strand anything.
    with direct_vm.expect_revert("[EXPECTED]:zero_stake"):
        contract.stake_vow(vow_id)

    assert contract.get_ledger()["total_in"] == 0


def test_stranger_stake_becomes_a_withdrawable_credit(
    direct_vm, direct_deploy, direct_owner, direct_alice, direct_bob
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)

    direct_vm.sender = direct_bob
    direct_vm.value = 400
    assert contract.stake_vow(vow_id) == "credit"
    direct_vm.value = 0

    # Bob's GEN is on the books, against Bob, and the vow is untouched.
    assert contract.get_vow(vow_id)["stake"] == 0
    assert contract.get_standing(direct_bob)["credit"] == 400
    assert contract.get_standing(direct_owner)["staked"] == 0

    ledger = contract.get_ledger()
    assert ledger["total_in"] == 400
    assert ledger["locked_total"] == 400
    assert ledger["total_out"] == 0
    assert _balanced(contract)


def test_credit_is_refused_when_there_is_none(
    direct_vm, direct_deploy, direct_owner
):
    contract = direct_deploy("contracts/Vow.py")
    direct_vm.sender = direct_owner

    with direct_vm.expect_revert("[EXPECTED]:no_credit"):
        contract.withdraw_credit()


def test_stake_on_a_closed_vow_becomes_a_credit(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)

    contract.abandon(vow_id)

    direct_vm.value = 300
    assert contract.stake_vow(vow_id) == "credit"
    direct_vm.value = 0

    assert contract.get_vow(vow_id)["stake"] == 0
    assert contract.get_standing(direct_owner)["credit"] == 300
    assert _balanced(contract)


def test_stake_on_an_unknown_vow_becomes_a_credit(
    direct_vm, direct_deploy, direct_owner
):
    contract = direct_deploy("contracts/Vow.py")
    direct_vm.sender = direct_owner

    direct_vm.value = 100
    assert contract.stake_vow("vw-99") == "credit"
    direct_vm.value = 0

    assert contract.get_standing(direct_owner)["credit"] == 100
    assert contract.get_ledger()["locked_total"] == 100


def test_stake_is_refused_while_paused(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)

    contract.set_paused(True)

    direct_vm.value = 700
    assert contract.stake_vow(vow_id) == "credit"
    direct_vm.value = 0

    assert contract.get_vow(vow_id)["stake"] == 0
    assert contract.get_standing(direct_owner)["credit"] == 700


def test_ledger_stays_balanced_through_a_vow_lifecycle(
    direct_vm, direct_deploy, direct_owner, direct_alice, direct_bob
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)

    direct_vm.value = 900
    contract.stake_vow(vow_id)
    direct_vm.value = 0
    assert _balanced(contract)

    # A refused deposit is a liability too, never free money.
    direct_vm.sender = direct_bob
    direct_vm.value = 150
    contract.stake_vow(vow_id)
    direct_vm.value = 0
    assert _balanced(contract)

    ledger = contract.get_ledger()
    assert ledger["total_in"] == 1050
    assert ledger["locked_total"] == 1050
    assert ledger["total_out"] == 0
