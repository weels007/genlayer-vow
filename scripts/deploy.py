"""Deploy Vow once and record the address in deployment.json.

    python scripts/deploy.py            # deploy only when the source changed
    python scripts/deploy.py --force    # redeploy and replace the address

The tester1 wallet comes from tests/ or from VOW_TESTER1_KEYSTORE +
VOW_TESTER1_PASSWORD, the same pair the integration suite uses, so one set of
secrets covers both workflows. Nothing secret is written: `deployment.json`
holds a public contract address and the revision it was built from.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from genlayer_py import create_client, studionet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from wallet import load_account  # noqa: E402

CONTRACT_PATH = ROOT / "contracts" / "Vow.py"
DEPLOYMENT_PATH = ROOT / "deployment.json"

POLL_INTERVAL = 10_000
POLL_RETRIES = 60

ADDRESS_VALUE = re.compile(r"0x[0-9a-fA-F]{40}")


def recorded() -> dict:
    if not DEPLOYMENT_PATH.exists():
        return {}
    try:
        payload = json.loads(DEPLOYMENT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_deployment(address: str, revision: str) -> None:
    DEPLOYMENT_PATH.write_text(
        json.dumps(
            {
                "contract": "Vow",
                "network": "GenLayer Studio (61999)",
                "address": address,
                "revision": revision,
                "explorer": f"https://explorer-studio.genlayer.com/address/{address}",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="redeploy even when this revision is recorded"
    )
    args = parser.parse_args()

    code = CONTRACT_PATH.read_text(encoding="utf-8")
    revision = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]
    previous = recorded()

    if not args.force and previous.get("address") and previous.get("revision") == revision:
        print(
            f"[deploy] deployment.json already points at {previous['address']} "
            f"at revision {revision}; reusing it"
        )
        print(f"[deploy] explorer: {previous.get('explorer', '')}")
        return 0

    account = load_account("tester1")
    client = create_client(studionet, account=account)

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

    # An address in a receipt is not proof of a contract: read the owner back
    # before recording anything, so deployment.json never points at a dud.
    try:
        owner = client.read_contract(address, "get_owner")
    except Exception as error:
        raise SystemExit(f"[deploy] the deployed contract does not answer: {error}")

    if str(owner).lower().replace("0x", "") != account.address.lower().replace("0x", ""):
        raise SystemExit(
            f"[deploy] the contract at {address} reports owner {owner}, "
            f"not {account.address}"
        )

    write_deployment(address, revision)
    print(f"[deploy] Vow is at {address}")
    print(f"[deploy] explorer: https://explorer-studio.genlayer.com/address/{address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
