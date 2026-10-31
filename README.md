# Prepaid AI Gateway

One Azure API Management (APIM) gateway in front of teams' Azure AI Foundry model deployments. Callers sign in with Entra ID, the platform team gets central token metrics, and each team's usage is priced and deducted from a prepaid dollar balance. Calls are refused with `402` when the balance runs out.

Design, build steps and costs: [`docs/ai-gateway-design.html`](docs/ai-gateway-design.html).

## How it works

1. A client calls APIM with an Entra token for `https://cognitiveservices.azure.com`.
2. The APIM policy validates the token, looks up the caller's account and balance through the billing service (cached 30 s), and returns `402` if the balance is zero or below.
3. APIM calls the team's Foundry resource with its own managed identity.
4. After the response, APIM sends a usage event to Event Hubs. The billing function prices it and deducts it from the account in Cosmos DB in one transaction, keyed by the APIM request ID so it's charged once.

## Repository layout

| Path | Contents |
|---|---|
| `apim/inference-policy.xml` | APIM policy for the AI inference API (PoC variant: function-key auth to the billing service) |
| `billing-func/` | Azure Functions app (Python): `account_for_client`, `charge_usage`, `top_up` |
| `chat-app/` | Streamlit chat client for testing the gateway with browser sign-in |
| `docs/ai-gateway-design.html` | Design document, portal build steps, cost estimate, PoC status |

## Billing function

App settings required on the Function app:

| Setting | Value |
|---|---|
| `COSMOS_ENDPOINT` | Cosmos DB account URI |
| `EventHub__fullyQualifiedNamespace` | `<namespace>.servicebus.windows.net` |

Deploy from Cloud Shell after uploading a zip of `function_app.py`, `host.json` and `requirements.txt`:

```bash
az functionapp deployment source config-zip -g <resource-group> -n <function-app> --src ~/billing-func.zip --build-remote true
```

The Function app's managed identity needs Cosmos DB Built-in Data Contributor and Azure Event Hubs Data Receiver.

## Chat app

```bash
cd chat-app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

Sign-in uses Microsoft's built-in Azure CLI client, so no app registration is needed. Its app ID (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`) must be mapped to an account in the `clients` container. To show the account balance, copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and add the `account_for_client` function key. `secrets.toml` is git-ignored.

## Money

Balances and costs are stored as integer micro-USD (1 USD = 1,000,000) so totals stay exact. Prices in the `prices` container are USD per 1M tokens.
