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

"""This script queries the OMEN subgraph to obtain the trades of a given address."""

import time
from argparse import ArgumentParser
from enum import Enum
from string import Template
from typing import Any

import requests


QUERY_BATCH_SIZE = 1000
DUST_THRESHOLD = 10000000000000
INVALID_ANSWER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF

headers = {
    "Accept": "application/json, multipart/mixed",
    "Content-Type": "application/json",
}


omen_xdai_trades_query = Template(
    """
    {
        fpmmTrades(
            where: {type: Buy, creator: "${creator}"}
            first: ${first}
            skip: ${skip}
            orderBy: creationTimestamp
            orderDirection: asc
        ) {
            id
            title
            collateralToken
            outcomeTokenMarginalPrice
            oldOutcomeTokenMarginalPrice
            type
            creator {
                id
            }
            creationTimestamp
            collateralAmount
            collateralAmountUSD
            feeAmount
            outcomeIndex
            outcomeTokensTraded
            transactionHash
            fpmm {
                id
                outcomes
                title
                answerFinalizedTimestamp
                currentAnswer
                isPendingArbitration
                arbitrationOccurred
                openingTimestamp
                condition {
                    id
                }
            }
        }
    }
    """
)


conditional_tokens_gc_user_query = Template(
    """
    {
        user(id: "${id}") {
            userPositions(
                first: ${first}
                skip: ${skip}
            ) {
                balance
                id
                position {
                    id
                    conditionIds
                }
                totalBalance
                wrappedBalance
            }
        }
    }
    """
)


class MarketStatus(Enum):
    """Market status"""

    UNDEFINED = 0
    OPEN = 1
    PENDING = 2
    FINALIZING = 3
    ARBITRATING = 4
    CLOSED = 5

    def __str__(self) -> str:
        """Prints the market status."""
        return self.name.capitalize()


def parse_arg() -> str:
    """Parse the creator positional argument."""
    parser = ArgumentParser()
    parser.add_argument("creator")
    args = parser.parse_args()
    return args.creator


def to_content(q: str) -> dict[str, Any]:
    """Convert the given query string to payload content, i.e., add it under a `queries` key and convert it to bytes."""
    finalized_query = {
        "query": q,
        "variables": None,
        "extensions": {"headers": None},
    }
    return finalized_query


def query_omen_xdai_subgraph() -> dict[str, Any]:
    """Query the subgraph."""
    url = "https://api.thegraph.com/subgraphs/name/protofire/omen-xdai"

    all_results: dict[str, Any] = {"data": {"fpmmTrades": []}}
    skip = 0
    while True:
        query = omen_xdai_trades_query.substitute(
            creator=creator.lower(), first=QUERY_BATCH_SIZE, skip=skip
        )
        content_json = to_content(query)
        res = requests.post(url, headers=headers, json=content_json)
        result_json = res.json()
        trades = result_json.get("data", {}).get("fpmmTrades", [])

        if not trades:
            break

        all_results["data"]["fpmmTrades"].extend(trades)
        skip += QUERY_BATCH_SIZE

    return all_results


def query_conditional_tokens_gc_subgraph() -> dict[str, Any]:
    """Query the subgraph."""
    url = "https://api.thegraph.com/subgraphs/name/gnosis/conditional-tokens-gc"

    all_results: dict[str, Any] = {"data": {"user": {"userPositions": []}}}
    skip = 0
    while True:
        query = conditional_tokens_gc_user_query.substitute(
            id=creator.lower(), first=QUERY_BATCH_SIZE, skip=skip
        )
        content_json = {"query": query}
        res = requests.post(url, headers=headers, json=content_json)
        result_json = res.json()
        user_data = result_json.get("data", {}).get("user", {})

        if not user_data:
            break

        user_positions = user_data.get("userPositions", [])

        if user_positions:
            all_results["data"]["user"]["userPositions"].extend(user_positions)
            skip += QUERY_BATCH_SIZE
        else:
            break

    if len(all_results["data"]["user"]["userPositions"]) == 0:
        return {"data": {"user": None}}

    return all_results


def _wei_to_dai(wei: int) -> str:
    dai = wei / 10**18
    formatted_dai = "{:.4f}".format(dai)
    return f"{formatted_dai} DAI"


def _is_redeemed(user_json: dict[str, Any], condition_id: str) -> bool:
    user_positions = user_json["data"]["user"]["userPositions"]

    for position in user_positions:
        position_condition_ids = position["position"]["conditionIds"]
        balance = int(position["balance"])

        if condition_id in position_condition_ids and balance == 0:
            return True

    return False


def parse_response(  # pylint: disable=too-many-locals,too-many-statements
    trades_json: dict[str, Any], user_json: dict[str, Any]
) -> str:
    """Parse the trades from the response."""
    output = "------\n"
    output += "Trades\n"
    output += "------\n"

    total_collateral_amount = 0
    total_fee_amount = 0
    total_earnings = 0
    total_redeemed = 0
    total_unclosed = 0
    for fpmmTrade in trades_json["data"]["fpmmTrades"]:
        try:
            collateral_amount = int(fpmmTrade["collateralAmount"])
            total_collateral_amount += collateral_amount
            outcome_index = int(fpmmTrade["outcomeIndex"])
            fee_amount = int(fpmmTrade["feeAmount"])
            total_fee_amount += fee_amount
            outcomes_tokens_traded = int(fpmmTrade["outcomeTokensTraded"])

            fpmm = fpmmTrade["fpmm"]
            answer_finalized_timestamp = fpmm["answerFinalizedTimestamp"]
            is_pending_arbitration = fpmm["isPendingArbitration"]
            opening_timestamp = fpmm["openingTimestamp"]
            condition_id = fpmm["condition"]["id"]

            output += f'      Question: {fpmmTrade["title"]}\n'
            output += f'    Market URL: https://aiomen.eth.limo/#/{fpmm["id"]}\n'

            market_status = MarketStatus.UNDEFINED
            if fpmm["currentAnswer"] is None and time.time() >= float(
                opening_timestamp
            ):
                market_status = MarketStatus.PENDING
                total_unclosed += 1
            elif fpmm["currentAnswer"] is None:
                market_status = MarketStatus.OPEN
                total_unclosed += 1
            elif is_pending_arbitration:
                market_status = MarketStatus.ARBITRATING
                total_unclosed += 1
            elif time.time() < float(answer_finalized_timestamp):
                market_status = MarketStatus.FINALIZING
                total_unclosed += 1
            else:
                market_status = MarketStatus.CLOSED

            output += f" Market status: {market_status}\n"
            output += f"        Bought: {_wei_to_dai(collateral_amount)} for {_wei_to_dai(outcomes_tokens_traded)} {fpmm['outcomes'][outcome_index]!r} tokens\n"
            output += f"           Fee: {_wei_to_dai(fee_amount)}\n"
            output += f"   Your answer: {fpmm['outcomes'][outcome_index]!r}\n"

            if market_status == MarketStatus.FINALIZING:
                current_answer = int(fpmm["currentAnswer"], 16)  # type: ignore
                is_invalid = current_answer == INVALID_ANSWER

                if is_invalid:
                    output += "Current answer: Market has been declared invalid.\n"
                else:
                    output += f"Current answer: {fpmm['outcomes'][current_answer]!r}\n"
            elif market_status == MarketStatus.CLOSED:
                current_answer = int(fpmm["currentAnswer"], 16)  # type: ignore
                is_invalid = current_answer == INVALID_ANSWER

                if is_invalid:
                    earnings = collateral_amount
                    output += "  Final answer: Market has been declared invalid.\n"
                    output += f"      Earnings: {_wei_to_dai(earnings)}\n"
                elif outcome_index == current_answer:
                    earnings = outcomes_tokens_traded
                    output += f"  Final answer: {fpmm['outcomes'][current_answer]!r} - Congrats! The trade was for the correct answer.\n"
                    output += f"      Earnings: {_wei_to_dai(earnings)}\n"
                    redeemed = _is_redeemed(user_json, condition_id)
                    output += f"      Redeemed: {redeemed}\n"
                    if redeemed:
                        total_redeemed += earnings
                else:
                    earnings = 0
                    output += f"  Final answer: {fpmm['outcomes'][current_answer]!r} - The trade was for the incorrect answer.\n"

                total_earnings += earnings

                if 0 < earnings < DUST_THRESHOLD:
                    output += "Earnings are dust.\n"

            output += "\n"
        except TypeError:
            output += "ERROR RETRIEVING TRADE INFORMATION.\n\n"

    output += "-------\n"
    output += "Summary\n"
    output += "-------\n"

    output += f'Num. trades: {len(trades_json["data"]["fpmmTrades"])} ({total_unclosed} on markets not yet closed)\n'
    output += f"Invested:    {_wei_to_dai(total_collateral_amount)}\n"
    output += f"Fees:        {_wei_to_dai(total_fee_amount)}\n"
    output += f"Earnings:    {_wei_to_dai(total_earnings)} (net earnings {_wei_to_dai(total_earnings-total_fee_amount-total_collateral_amount)})\n"
    output += f"Redeemed:    {_wei_to_dai(total_redeemed)}\n"

    return output


if __name__ == "__main__":
    creator = parse_arg()
    _trades_json = query_omen_xdai_subgraph()
    _user_json = query_conditional_tokens_gc_subgraph()
    parsed = parse_response(_trades_json, _user_json)
    print(parsed)
