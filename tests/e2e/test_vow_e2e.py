"""End-to-end: the deployed contract, both wallets, the whole loop.

The Studio suite deploys its own copy and the smoke stops at abandon. This suite
is the third angle: it runs against the address in `deployment.json`, drives a
vow from publish to stake to settle to payout, and checks the adversarial
refusals on the same published contract.

Every assertion is a delta — a count, a balance, a ledger total compared before
and after — so the suite can be re-run against the same contract without the
history of earlier runs leaking into it.

    pytest tests/e2e -v -s
"""

import json
import re
import sys
import time
from pathlib import Path

import pytest
from genlayer_py import create_client, studionet

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from wallet import load_account  # noqa: E402

DEPLOYMENT_PATH = ROOT / "deployment.json"
ADDRESS_VALUE = re.compile(r"0x[0-9a-fA-F]{40}")

POLL_INTERVAL = 10_000
POLL_RETRIES = 60
RETRY_PAUSE = 20

DAY = 86400
STAKE = 100
SETTLE_DEADLINE_SECONDS = 60

ALLOWED_VERDICTS = ("KEPT", "BROKEN", "UNRESOLVED")

PROMISE = (
    "Publish the full text of RFC 2119, the requirement-keyword standard, at "
    "https://www.rfc-editor.org/rfc/rfc2119.txt."
)
EVIDENCE_URL = "https://www.rfc-editor.org/rfc/rfc2119.txt"
CRITERIA = "KEPT only when the page carries the RFC 2119 text and its requirement keywords."


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _deployed_address() -> str:
    if not DEPLOYMENT_PATH.exists():
        pytest.exit("no deployment.json; run the Deploy Contract workflow first")
    payload = json.loads(DEPLOYMENT_PATH.read_text(encoding="utf-8"))
    address = str(payload.get("address", "")).strip()
    if not ADDRESS_VALUE.fullmatch(address):
        pytest.exit(f"deployment.json has no usable address: {address!r}")
    return address


@pytest.fixture(scope="module")
def contract() -> str:
    """The address the README points at, never a fresh deployment."""
    address = _deployed_address()
    print(f"\n[e2e] contract {address}")
    return address


@pytest.fixture(scope="module")
def account():
    return load_account("tester1")


@pytest.fixture(scope="module")
def stranger():
    return load_account("tester2")


@pytest.fixture(scope="module")
def client(account):
    return create_client(studionet, account=account)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hex(value) -> str:
    return str(value).lower().replace("0x", "")


def _succeeded(result) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("status_name") not in ("ACCEPTED", "FINALIZED"):
        return False
    leader = (result.get("consensus_data") or {}).get("leader_receipt")
    if isinstance(leader, list):
        leader = leader[0] if leader else None
    if isinstance(leader, dict):
        return leader.get("execution_result") == "SUCCESS"
    return str(result.get("tx_execution_result_name", "")) == "FINISHED_WITH_RETURN"


def _definite_rollback(result) -> bool:
    if not isinstance(result, dict):
        return False
    leader = (result.get("consensus_data") or {}).get("leader_receipt")
    if isinstance(leader, list):
        leader = leader[0] if leader else None
    return isinstance(leader, dict) and leader.get("execution_result") == "ERROR"


def _attempt(client, account, contract, method, args=None, value=0):
    try:
        tx_hash = client.write_contract(
            contract, method, args=args or [], value=value, account=account
        )
        return client.wait_for_transaction_receipt(
            tx_hash, retries=POLL_RETRIES, interval=POLL_INTERVAL
        )
    except Exception as error:
        return error


def _expect_ok(client, account, contract, method, args=None, value=0, verify=None):
    result = None
    for attempt in range(3):
        result = _attempt(client, account, contract, method, args, value)
        if _succeeded(result):
            return result
        if verify is not None and verify():
            return result
        if _definite_rollback(result):
            raise AssertionError(f"{method} was refused: {result}")
        if attempt < 2:
            time.sleep(RETRY_PAUSE)
    raise AssertionError(f"{method} did not reach consensus: {result}")


def _expect_denied(client, account, contract, method, args=None, value=0):
    result = _attempt(client, account, contract, method, args, value)
    assert not _succeeded(result), f"{method} should have been refused: {result}"
    return result


def _read(client, contract, method, args=None):
    """Read a view, retrying a provider hiccup.

    A 502 from the RPC endpoint is not an answer from the contract. Retrying
    keeps the suite from going red on weather; a real failure still raises.
    """
    last = None
    for attempt in range(3):
        try:
            return client.read_contract(contract, method, args=args or [])
        except Exception as error:
            last = error
            if attempt < 2:
                print(f"  {method} read failed, retrying: {error}")
                time.sleep(RETRY_PAUSE)
    raise last


def _vow_ids(client, contract):
    return _read(client, contract, "get_vow_ids")["ids"]


def _vow(client, contract, vow_id):
    return _read(client, contract, "get_vow", [vow_id])


def _ledger(client, contract):
    return _read(client, contract, "get_ledger")


def _balance(client, contract):
    return int(_read(client, contract, "get_balance"))


def _make_vow(client, account, contract, ref, seconds, beneficiary):
    before = len(_vow_ids(client, contract))
    _expect_ok(
        client,
        account,
        contract,
        "make_vow",
        [PROMISE, EVIDENCE_URL, CRITERIA, seconds, beneficiary.address, ref],
        verify=lambda: len(_vow_ids(client, contract)) > before,
    )
    return _vow_ids(client, contract)[-1]


def _stake(client, account, contract, vow_id, amount):
    _expect_ok(client, account, contract, "stake_vow", [vow_id], value=amount)
    return int(_vow(client, contract, vow_id)["stake"])


def _wait_due(client, contract, vow_id, attempts=18, pause=10):
    for _ in range(attempts):
        if bool(_read(client, contract, "is_due", [vow_id])):
            return True
        time.sleep(pause)
    return False


def _wait_balance_drop(client, contract, before, amount, attempts=30, pause=10):
    last = _balance(client, contract)
    for _ in range(attempts):
        if before - last == amount:
            return last
        time.sleep(pause)
        last = _balance(client, contract)
    return last


def _ref(label: str) -> str:
    return f"e2e-{label}-{int(time.time())}"


def _invariant_holds(client, contract) -> bool:
    ledger = _ledger(client, contract)
    return int(ledger["total_in"]) == int(ledger["total_out"]) + int(ledger["locked_total"])


# ---------------------------------------------------------------------------
# the published contract answers
# ---------------------------------------------------------------------------


def test_01_the_deployed_contract_is_live(client, account, contract):
    assert _hex(_read(client, contract, "get_owner")) == _hex(account.address)
    assert _read(client, contract, "is_paused") is False

    rules = _read(client, contract, "get_rules")
    assert int(rules["min_deadline_seconds"]) == 60
    assert int(rules["max_deadline_seconds"]) == 365 * DAY
    assert int(rules["settle_bounty_bps"]) == 200

    assert _invariant_holds(client, contract)


# ---------------------------------------------------------------------------
# adversarial: the refusals that protect the money
# ---------------------------------------------------------------------------


def test_02_refusals_on_the_published_contract(client, account, contract, stranger):
    ref = _ref("refusal")
    vow_id = _make_vow(client, account, contract, ref, DAY, stranger)

    # Double submit: the same caller reusing the same reference is refused, so a
    # retried wallet cannot mint a second, identical commitment.
    _expect_denied(
        client,
        account,
        contract,
        "make_vow",
        [PROMISE, EVIDENCE_URL, CRITERIA, DAY, stranger.address, ref],
    )

    # A promise under ten characters is not a promise.
    _expect_denied(
        client,
        account,
        contract,
        "make_vow",
        ["soon", EVIDENCE_URL, "", DAY, stranger.address, _ref("short")],
    )

    # Evidence has to be https and public.
    _expect_denied(
        client,
        account,
        contract,
        "make_vow",
        [PROMISE, "http://www.rfc-editor.org/rfc/rfc2119.txt", "", DAY, stranger.address, _ref("http")],
    )
    _expect_denied(
        client,
        account,
        contract,
        "make_vow",
        [PROMISE, "https://127.0.0.1/rfc2119.txt", "", DAY, stranger.address, _ref("ip")],
    )

    # A zero stake is a plain revert, never a stranded deposit.
    _expect_denied(client, account, contract, "stake_vow", [vow_id])

    # And only the promiser may forfeit their own vow.
    _expect_denied(client, stranger, contract, "abandon", [vow_id])
    assert _vow(client, contract, vow_id)["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# end to end: publish, stake, settle, and the money follows the verdict
# ---------------------------------------------------------------------------


def test_03_publish_stake_settle_pays_out(client, account, contract, stranger):
    vow_id = _make_vow(
        client, account, contract, _ref("settle"), SETTLE_DEADLINE_SECONDS, stranger
    )
    _stake(client, account, contract, vow_id, STAKE)

    record = _vow(client, contract, vow_id)
    assert int(record["stake"]) == STAKE
    assert record["status"] == "ACTIVE"

    assert _wait_due(client, contract, vow_id), "the vow never became due"

    before = _balance(client, contract)
    _expect_ok(client, account, contract, "settle", [vow_id])

    record = _vow(client, contract, vow_id)
    verdict = record["verdict"]
    assert verdict in ALLOWED_VERDICTS, verdict
    assert int(record["attempts"]) == 1
    assert _invariant_holds(client, contract)

    if verdict == "UNRESOLVED":
        # A page that cannot decide the case never confiscates the stake.
        assert record["status"] == "ACTIVE"
        assert record["quote"] == ""
        assert _balance(client, contract) == before
    else:
        # The committee resolved it: the status mirrors the verdict, the quote is
        # grounded, and exactly the stake leaves the contract.
        assert record["status"] == verdict
        assert record["quote"], "a resolving verdict must carry a grounded quote"
        after = _wait_balance_drop(client, contract, before, STAKE)
        assert before - after == STAKE


def test_04_abandon_pays_the_beneficiary_today(client, account, contract, stranger):
    vow_id = _make_vow(client, account, contract, _ref("abandon"), DAY, stranger)
    _stake(client, account, contract, vow_id, STAKE)

    locked_before = int(_ledger(client, contract)["locked_total"])
    out_before = int(_ledger(client, contract)["total_out"])
    balance_before = _balance(client, contract)

    _expect_ok(client, account, contract, "abandon", [vow_id])

    record = _vow(client, contract, vow_id)
    assert record["status"] == "ABANDONED"
    assert record["verdict"] == "ABANDONED"

    after = _wait_balance_drop(client, contract, balance_before, STAKE)
    assert balance_before - after == STAKE

    ledger = _ledger(client, contract)
    assert int(ledger["locked_total"]) == locked_before - STAKE
    assert int(ledger["total_out"]) == out_before + STAKE
    assert _invariant_holds(client, contract)


def test_05_views_agree_with_the_ledger(client, contract):
    summary = _read(client, contract, "get_summary")
    vows = _read(client, contract, "get_vows")

    assert int(summary["vows"]) == len(vows)
    assert int(summary["active"]) == sum(
        1 for record in vows.values() if record["status"] == "ACTIVE"
    )
    assert _balance(client, contract) >= 0
    assert _invariant_holds(client, contract)
