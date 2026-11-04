"""Deploy Vow once and print the address it landed on.

    GENLAYER_PRIVATE_KEY=0x... python scripts/deploy.py

The key is read from the environment and never written anywhere: a contract
address is public, a private key is not. `genlayer_py`, the client used here,
is the same one the rest of this workspace deploys with.
"""

import hashlib
import os
import re
from pathlib import Path

from eth_account import Account
from genlayer_py import create_client, studionet

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "contracts" / "Vow.py"

POLL_INTERVAL = 10_000
POLL_RETRIES = 60

ADDRESS_VALUE = re.compile(r"0x[0-9a-fA-F]{40}")


def load_account():
    raw = os.environ.get("GENLAYER_PRIVATE_KEY", "").strip()
    if not raw:
        raise SystemExit(
            "[deploy] set GENLAYER_PRIVATE_KEY to the key that will own the contract"
        )
    if not raw.startswith("0x"):
        raw = "0x" + raw
    try:
        return Account.from_key(raw)
    except Exception as error:
        raise SystemExit(f"[deploy] GENLAYER_PRIVATE_KEY is not a valid key: {error}")


def find_address(receipt) -> str:
    if not isinstance(receipt, dict):
        return ""
    candidates = [receipt.get("to_address"), receipt.get("recipient")]
    for key in ("data", "tx_data_decoded"):
        block = receipt.get(key)
        if isinstance(block, dict):
            candidates.append(block.get("contract_address"))
            candidates.append(block.get("to_address"))
    for value in candidates:
        if isinstance(value, str) and ADDRESS_VALUE.fullmatch(value):
            return value
    return ""


def execution_succeeded(receipt) -> bool:
    if not isinstance(receipt, dict):
        return False
    if receipt.get("status_name") not in ("ACCEPTED", "FINALIZED"):
        return False
    leader = (receipt.get("consensus_data") or {}).get("leader_receipt")
    if isinstance(leader, list):
        leader = leader[0] if leader else None
    if isinstance(leader, dict):
        return leader.get("execution_result") == "SUCCESS"
    return str(receipt.get("tx_execution_result_name", "")) == "FINISHED_WITH_RETURN"


def main() -> int:
    account = load_account()
    client = create_client(studionet, account=account)

    code = CONTRACT_PATH.read_text(encoding="utf-8")
    revision = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]

    print(f"[deploy] deploying Vow from {account.address} (revision {revision})")
    tx_hash = client.deploy_contract(code=code, account=account)
    print(f"[deploy] transaction {tx_hash}")

    receipt = client.wait_for_transaction_receipt(
        tx_hash, retries=POLL_RETRIES, interval=POLL_INTERVAL
    )
    if not execution_succeeded(receipt):
        raise SystemExit(f"[deploy] the deployment did not succeed: {receipt}")

    address = find_address(receipt)
    if not address:
        raise SystemExit(f"[deploy] no contract address in the receipt: {receipt}")

    print(f"[deploy] Vow is at {address}")
    print(f"[deploy] explorer: https://explorer-studio.genlayer.com/address/{address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
