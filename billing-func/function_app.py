import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal
from typing import List

import azure.functions as func
from azure.cosmos import CosmosClient, exceptions
from azure.identity import DefaultAzureCredential

# PoC: each HTTP function is protected by its own function key (x-functions-key header).
# APIM holds the account_for_client key; only billing admins hold the top_up key.
app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

MICRO = Decimal(1_000_000)
PRICE_TTL_SECONDS = 300
EVENT_HUB_NAME = "usage-events"

_containers = {}
_price_cache = {"loaded_at": 0.0, "rows": {}}


def _container(name: str):
    # Connect on first use, not at import, so a Cosmos problem never stops the functions from loading.
    if not _containers:
        db = CosmosClient(os.environ["COSMOS_ENDPOINT"], credential=DefaultAzureCredential()).get_database_client("billing")
        for container in ("ledger", "clients", "prices"):
            _containers[container] = db.get_container_client(container)
    return _containers[name]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _usd(micro_usd: int) -> float:
    # Display only; balances and costs are stored and summed as integer micro-USD.
    return float(Decimal(micro_usd) / MICRO)


def _json(body: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body), status_code=status, mimetype="application/json")


def _price_for(model: str) -> dict:
    if time.time() - _price_cache["loaded_at"] > PRICE_TTL_SECONDS:
        _price_cache["rows"] = {p["id"]: p for p in _container("prices").read_all_items()}
        _price_cache["loaded_at"] = time.time()
    rows = _price_cache["rows"]
    if model in rows:
        return rows[model]
    # "gpt-4o-2024-08-06" falls back to the "gpt-4o" entry
    match = max((k for k in rows if model.startswith(k)), key=len, default=None)
    if match is None:
        raise KeyError(f"No price configured for model '{model}'")
    return rows[match]


def _cost_micro_usd(price: dict, prompt: int, cached: int, completion: int) -> int:
    input_rate = Decimal(str(price["inputPerM"]))
    cached_rate = Decimal(str(price.get("cachedInputPerM", price["inputPerM"])))
    output_rate = Decimal(str(price["outputPerM"]))
    usd = (Decimal(prompt - cached) * input_rate
           + Decimal(cached) * cached_rate
           + Decimal(completion) * output_rate) / Decimal(1_000_000)
    return int((usd * MICRO).to_integral_value(rounding=ROUND_CEILING))


def _apply(account_id: str, entry: dict, delta_micro_usd: int) -> bool:
    """Write the entry and move the balance in one transaction. False if the entry already exists."""
    try:
        _container("ledger").execute_item_batch(
            batch_operations=[
                ("create", (entry,)),
                ("patch", (account_id, [{"op": "incr", "path": "/balanceMicroUsd", "value": delta_micro_usd}])),
            ],
            partition_key=account_id,
        )
        return True
    except exceptions.CosmosBatchOperationError as err:
        if err.error_index == 0 and err.operation_responses[0].get("statusCode") == 409:
            return False
        raise


@app.route(route="accounts/by-client/{clientId}", methods=["GET"])
def account_for_client(req: func.HttpRequest) -> func.HttpResponse:
    client_id = req.route_params["clientId"]
    try:
        link = _container("clients").read_item(client_id, partition_key=client_id)
        account = _container("ledger").read_item(link["accountId"], partition_key=link["accountId"])
    except exceptions.CosmosResourceNotFoundError:
        return _json({"error": "Unknown client"}, 404)
    balance = account["balanceMicroUsd"] if account.get("status") == "active" else 0
    return _json({
        "accountId": account["accountId"],
        "backendId": account["backendId"],
        "balanceMicroUsd": balance,
        "balanceUsd": _usd(balance),
    })


@app.event_hub_message_trigger(arg_name="events", event_hub_name=EVENT_HUB_NAME,
                               connection="EventHub", cardinality=func.Cardinality.MANY)
def charge_usage(events: List[func.EventHubEvent]) -> None:
    for event in events:
        usage = json.loads(event.get_body())
        account_id = usage["accountId"]
        cost = _cost_micro_usd(_price_for(usage["model"]), usage["promptTokens"],
                               usage.get("cachedTokens", 0), usage["completionTokens"])
        entry = {
            "id": usage["chargeId"],
            "accountId": account_id,
            "type": "charge",
            "clientId": usage["clientId"],
            "model": usage["model"],
            "promptTokens": usage["promptTokens"],
            "cachedTokens": usage.get("cachedTokens", 0),
            "completionTokens": usage["completionTokens"],
            "costMicroUsd": cost,
            "costUsd": _usd(cost),
            "estimated": usage.get("estimated", False),
            "ts": usage["ts"],
        }
        if not _apply(account_id, entry, -cost):
            logging.info("Charge %s already recorded, skipped", usage["chargeId"])


@app.route(route="accounts/{accountId}/topups", methods=["POST"])
def top_up(req: func.HttpRequest) -> func.HttpResponse:
    account_id = req.route_params["accountId"]
    try:
        body = req.get_json()
        amount = int(Decimal(str(body["amountUsd"])) * MICRO)
    except (ValueError, KeyError, ArithmeticError):
        return _json({"error": "Body must be JSON with amountUsd, reference and by"}, 400)
    if amount <= 0 or not body.get("reference") or not body.get("by"):
        return _json({"error": "amountUsd must be positive; reference and by are required"}, 400)
    entry = {
        "id": f"topup-{body['reference']}",
        "accountId": account_id,
        "type": "topup",
        "amountMicroUsd": amount,
        "amountUsd": _usd(amount),
        "by": body["by"],
        "ts": _now(),
    }
    try:
        applied = _apply(account_id, entry, amount)
    except exceptions.CosmosResourceNotFoundError:
        return _json({"error": f"Account '{account_id}' does not exist"}, 404)
    account = _container("ledger").read_item(account_id, partition_key=account_id)
    return _json({"applied": applied, "balanceUsd": _usd(account["balanceMicroUsd"])}, 201 if applied else 200)
