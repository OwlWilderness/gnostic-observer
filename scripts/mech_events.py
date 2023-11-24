#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2022-2023 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""Utilities to retrieve on-chain Mech events."""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import requests
from eth_utils import to_checksum_address
from tqdm import tqdm
from web3 import HTTPProvider, Web3
from web3.datastructures import AttributeDict
from web3.types import BlockParams


SCRIPT_PATH = Path(__file__).resolve().parent
STORE_PATH = Path(SCRIPT_PATH, "..", ".trader_runner")
MECH_EVENTS_JSON_PATH = Path(STORE_PATH, "mech_events.json")
AGENT_MECH_JSON_PATH = Path(SCRIPT_PATH, "..", "contracts", "AgentMech.json")
HTTP = "http://"
HTTPS = HTTP[:4] + "s" + HTTP[4:]
CID_PREFIX = "f01701220"
IPFS_ADDRESS = f"{HTTPS}gateway.autonolas.tech/ipfs/"
LATEST_BLOCK_NAME: BlockParams = "latest"
BLOCK_DATA_NUMBER = "number"
BLOCKS_CHUNK_SIZE = 5000
EXCLUDED_BLOCKS_THRESHOLD = 2 * BLOCKS_CHUNK_SIZE
NUM_EXCLUDED_BLOCKS = 10
MECH_EVENTS_DB_VERSION = 2
DEFAULT_MECH_FEE = 10000000000000000

# Pair of (Mech contract address, Mech contract deployed on block number).
MECH_CONTRACT_ADDRESSES = [
    # Old Mech contract
    (
        to_checksum_address("0xff82123dfb52ab75c417195c5fdb87630145ae81"),
        27939217,
    ),
    # New Mech contract
    (
        to_checksum_address("0x77af31de935740567cf4ff1986d04b2c964a786a"),
        30663133,
    ),
]


@dataclass
class MechBaseEvent:
    """Base class for mech's on-chain event representation."""

    event_id: str
    data: str
    sender: str
    transaction_hash: str
    block_number: int
    utc_timestamp: int
    ipfs_link: str
    ipfs_contents: Dict[str, Any]

    def __init__(
        self,
        event_id: str,
        data: str,
        sender: str,
        transaction_hash: str,
        block_number: int,
        utc_timestamp: int,
    ):  # pylint: disable=too-many-arguments
        """Initializes the MechBaseEvent"""
        self.event_id = event_id
        self.data = data
        self.sender = sender
        self.transaction_hash = transaction_hash
        self.block_number = block_number
        self.utc_timestamp = utc_timestamp
        self.ipfs_link = ""
        self.ipfs_contents = {}
        self._populate_ipfs_contents(data)

    def _populate_ipfs_contents(self, data: str) -> None:
        url = f"{IPFS_ADDRESS}{CID_PREFIX}{data}"
        for _url in [f"{url}/metadata.json", url]:
            try:
                response = requests.get(_url)
                response.raise_for_status()
                self.ipfs_contents = response.json()
                self.ipfs_link = _url
            except Exception:  # pylint: disable=broad-except
                continue


@dataclass
class MechRequest(MechBaseEvent):
    """A mech's on-chain response representation."""

    request_id: str
    fee: int
    event_name: str = "Request"

    def __init__(self, event: AttributeDict, utc_timestamp: int):
        """Initializes the MechRequest"""

        if self.event_name != event["event"]:
            raise ValueError("Invalid event to initialize MechRequest")

        args = event["args"]
        super().__init__(
            event_id=args["requestId"],
            data=args["data"].hex(),
            sender=args["sender"],
            transaction_hash=event["transactionHash"].hex(),
            block_number=event["blockNumber"],
            utc_timestamp=utc_timestamp,
        )

        self.request_id = self.event_id
        # TODO This should be updated to extract the fee from the transaction.
        self.fee = DEFAULT_MECH_FEE


def _read_mech_events_data_from_file() -> Dict[str, Any]:
    """Read Mech events data from the JSON file."""
    try:
        with open(MECH_EVENTS_JSON_PATH, "r", encoding="utf-8") as file:
            mech_events_data = json.load(file)

        # Check if it is an old DB version
        if mech_events_data.get("db_version", 0) < MECH_EVENTS_DB_VERSION:
            current_time = time.strftime("%Y-%m-%d_%H-%M-%S")
            old_db_filename = f"mech_events.{current_time}.old.json"
            os.rename(MECH_EVENTS_JSON_PATH, Path(STORE_PATH, old_db_filename))
            mech_events_data = {}
            mech_events_data["db_version"] = MECH_EVENTS_DB_VERSION
    except FileNotFoundError:
        mech_events_data = {}
        mech_events_data["db_version"] = MECH_EVENTS_DB_VERSION
    return mech_events_data


MINIMUM_WRITE_FILE_DELAY = 20
last_write_time = 0.0


def _write_mech_events_data(
    mech_events_data: Dict[str, Any], force_write=False
) -> None:
    global last_write_time
    now = time.time()

    if force_write or (now - last_write_time) >= MINIMUM_WRITE_FILE_DELAY:
        with open(MECH_EVENTS_JSON_PATH, "w", encoding="utf-8") as file:
            json.dump(mech_events_data, file, indent=2)
        last_write_time = now


# pylint: disable=too-many-locals
def _update_mech_events_db(
    rpc: str,
    mech_contract_address: str,
    event_name: str,
    earliest_block: int,
    sender: str,
) -> None:
    """Get the mech Request events."""

    print(
        f"Updating the local Mech events database. This may take a while.\n"
        f"           Event: {event_name}\n"
        f"   Mech contract: {mech_contract_address}\n"
        f"  Sender address: {sender}"
    )

    # Read the current Mech events database
    mech_events_data = _read_mech_events_data_from_file()

    # Search for Mech events in the blockchain
    try:
        w3 = Web3(HTTPProvider(rpc))
        with open(AGENT_MECH_JSON_PATH, "r", encoding="utf-8") as file:
            contract_data = json.load(file)

        abi = contract_data.get("abi", [])
        contract_instance = w3.eth.contract(address=mech_contract_address, abi=abi)

        last_processed_block = (
            mech_events_data.get(sender, {})
            .get(mech_contract_address, {})
            .get(event_name, {})
            .get("last_processed_block", 0)
        )
        starting_block = max(earliest_block, last_processed_block + 1)
        ending_block = w3.eth.get_block(LATEST_BLOCK_NAME)[BLOCK_DATA_NUMBER]

        print(f"  Starting block: {starting_block}")
        print(f"    Ending block: {ending_block}")

        # If the script has to process relatively recent blocks,
        # this will allow the RPC synchronize them and prevent
        # throwing an exception.
        if ending_block - starting_block < EXCLUDED_BLOCKS_THRESHOLD:
            ending_block -= NUM_EXCLUDED_BLOCKS
            time.sleep(10)

        for from_block in tqdm(
            range(starting_block, ending_block, BLOCKS_CHUNK_SIZE),
            desc="        Progress: ",
        ):
            to_block = min(from_block + BLOCKS_CHUNK_SIZE, ending_block)
            event_filter = contract_instance.events[event_name].create_filter(
                fromBlock=from_block, toBlock=to_block
            )
            chunk = event_filter.get_all_entries()
            w3.eth.uninstall_filter(event_filter.filter_id)
            filtered_events = [
                event for event in chunk if event["args"]["sender"] == sender
            ]

            # Update the Mech events data with the latest examined chunk
            sender_data = mech_events_data.setdefault(sender, {})
            contract_data = sender_data.setdefault(mech_contract_address, {})
            event_data = contract_data.setdefault(event_name, {})
            event_data["last_processed_block"] = to_block

            mech_events = event_data.setdefault("mech_events", {})
            for event in filtered_events:
                block_number = event["blockNumber"]
                utc_timestamp = w3.eth.get_block(block_number).timestamp

                if event_name == MechRequest.event_name:
                    mech_event = MechRequest(event, utc_timestamp)
                    mech_events[mech_event.event_id] = mech_event.__dict__

            # Store the (updated) Mech events database
            _write_mech_events_data(mech_events_data)

    except KeyboardInterrupt:
        print(
            "\n"
            f"WARNING: The update of the local Mech events database was cancelled (contract {mech_contract_address}). "
            "Therefore, the Mech calls and costs might not be reflected accurately. "
            "You may attempt to rerun this script to retry synchronizing the database."
        )
        input("Press Enter to continue...")
    except Exception:  # pylint: disable=broad-except
        print(
            f"WARNING: An error occurred while updating the local Mech events database (contract {mech_contract_address}). "
            "Therefore, the Mech calls and costs might not be reflected accurately. "
            "You may attempt to rerun this script to retry synchronizing the database."
        )
        input("Press Enter to continue...")

    _write_mech_events_data(mech_events_data, True)
    print("")


def _get_mech_events(rpc: str, sender: str, event_name: str) -> Dict[str, Any]:
    """Updates the local database of Mech events and returns the Mech events."""

    for (
        mech_contract_address,
        mech_contract_deployed_block,
    ) in MECH_CONTRACT_ADDRESSES:
        _update_mech_events_db(
            rpc, mech_contract_address, event_name, mech_contract_deployed_block, sender
        )

    mech_events_data = _read_mech_events_data_from_file()
    sender_data = mech_events_data.get(sender, {})

    all_mech_events = {}
    for mech_contract_data in sender_data.values():
        event_data = mech_contract_data.get(event_name, {})
        mech_events = event_data.get("mech_events", {})
        all_mech_events.update(mech_events)

    return all_mech_events


def get_mech_requests(rpc: str, sender: str) -> Dict[str, Any]:
    """Returns the Mech requests."""

    return _get_mech_events(rpc, sender, MechRequest.event_name)
