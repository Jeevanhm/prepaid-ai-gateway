"""Chat client for testing the prepaid AI gateway.

Signs in with Entra ID in the browser, calls models through APIM the same way a team's app would,
and shows each call's token usage, estimated cost and the account's remaining balance.
"""
import base64
import json
import urllib.request
from decimal import ROUND_CEILING, Decimal

import streamlit as st
from azure.identity import InteractiveBrowserCredential, get_bearer_token_provider
from openai import APIStatusError, AzureOpenAI

SCOPE = "https://cognitiveservices.azure.com/.default"


def _setting(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default))
    except FileNotFoundError:
        return default


@st.cache_resource
def _credential(tenant: str) -> InteractiveBrowserCredential:
    # Default client is the Azure CLI app (04b07795-...), so the token's appid matches that clients mapping.
    return InteractiveBrowserCredential(tenant_id=tenant)


def _token_claims(credential) -> dict:
    token = credential.get_token(SCOPE).token
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _balance(func_host: str, func_key: str, client_id: str) -> dict:
    req = urllib.request.Request(
        f"https://{func_host}/api/accounts/by-client/{client_id}",
        headers={"x-functions-key": func_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as err:  # shown in the sidebar, never fatal
        return {"error": str(err)}


def _cost_usd(prompt: int, cached: int, completion: int,
              input_per_m: float, cached_per_m: float, output_per_m: float) -> Decimal:
    micro = (Decimal(prompt - cached) * Decimal(str(input_per_m))
             + Decimal(cached) * Decimal(str(cached_per_m))
             + Decimal(completion) * Decimal(str(output_per_m)))
    # Same rounding as the billing service: whole micro-dollars, rounded up per call.
    return micro.to_integral_value(rounding=ROUND_CEILING) / Decimal(1_000_000)


st.set_page_config(page_title="AI Gateway Chat", layout="wide")

with st.sidebar:
    st.header("Gateway")
    endpoint = st.text_input("Gateway endpoint", _setting("GATEWAY_ENDPOINT", "https://apim-meter-sandbox-eus-01.azure-api.net/ai"))
    deployment = st.text_input("Deployment", _setting("DEPLOYMENT", "gpt-4o"))
    api_version = st.text_input("API version", _setting("API_VERSION", "2024-10-21"))
    tenant = st.text_input("Entra tenant", _setting("TENANT_ID", "organizations"),
                           help="Tenant ID or domain. 'organizations' works for any work account.")
    max_tokens = st.slider("Max output tokens", 16, 1024, 200, step=16)
    stream = st.toggle("Stream responses", value=False,
                       help="Streamed calls are charged as estimates by the gateway policy.")

    st.header("Prices (USD per 1M tokens)")
    st.caption("For the cost estimate only. Match these to the prices container.")
    input_per_m = st.number_input("Input", value=2.50, step=0.05, format="%.3f")
    cached_per_m = st.number_input("Cached input", value=1.25, step=0.05, format="%.3f")
    output_per_m = st.number_input("Output", value=10.00, step=0.05, format="%.3f")

credential = _credential(tenant)
client = AzureOpenAI(
    azure_endpoint=endpoint,
    azure_ad_token_provider=get_bearer_token_provider(credential, SCOPE),
    api_version=api_version,
)

if "messages" not in st.session_state:
    st.session_state.messages = []
    st.session_state.spent = Decimal(0)
    st.session_state.calls = 0

st.title("AI Gateway Chat")

if "claims" not in st.session_state:
    st.info("Sign in to start. A browser window opens for your Microsoft account.")
    if st.button("Sign in", type="primary"):
        with st.spinner("Waiting for browser sign-in..."):
            st.session_state.claims = _token_claims(credential)
        st.rerun()
    st.stop()

claims = st.session_state.claims
client_id = claims.get("appid") or claims.get("azp", "")

with st.sidebar:
    st.header("Session")
    user = claims.get("upn") or claims.get("unique_name") or claims.get("name", "unknown")
    st.write(f"Signed in as **{user}**")
    st.caption(f"App ID the gateway sees: `{client_id}`")
    st.metric("Calls this session", st.session_state.calls)
    st.metric("Estimated spend this session", f"${st.session_state.spent:.6f}")

    func_host = _setting("FUNC_HOST")
    func_key = _setting("FUNC_KEY")
    if func_host and func_key:
        account = _balance(func_host, func_key, client_id)
        if "error" in account:
            st.warning(f"Balance lookup failed: {account['error']}")
        else:
            st.metric(f"Balance · {account['accountId']}", f"${account['balanceUsd']:.6f}")
        if st.button("Refresh balance"):
            st.rerun()
    else:
        st.caption("Add FUNC_HOST and FUNC_KEY to .streamlit/secrets.toml to show the account balance.")

    if st.button("Clear chat"):
        st.session_state.messages = []
        st.rerun()

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message.get("meta"):
            st.caption(message["meta"])

if prompt := st.chat_input("Message the model through the gateway"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
    with st.chat_message("assistant"):
        try:
            if stream:
                chunks = client.chat.completions.create(
                    model=deployment, messages=history, max_tokens=max_tokens, stream=True,
                    stream_options={"include_usage": True},
                )
                seen_usage = []

                def _text():
                    for chunk in chunks:
                        if chunk.usage:
                            seen_usage.append(chunk.usage)
                        if chunk.choices and chunk.choices[0].delta.content:
                            yield chunk.choices[0].delta.content

                reply = st.write_stream(_text())
                usage = seen_usage[-1] if seen_usage else None
            else:
                response = client.chat.completions.create(model=deployment, messages=history, max_tokens=max_tokens)
                reply = response.choices[0].message.content or ""
                usage = response.usage
                st.markdown(reply)

            note = " · streamed (gateway charges an estimate)" if stream else ""
            if usage:
                details = getattr(usage, "prompt_tokens_details", None)
                cached = getattr(details, "cached_tokens", 0) or 0
                cost = _cost_usd(usage.prompt_tokens, cached, usage.completion_tokens,
                                 input_per_m, cached_per_m, output_per_m)
                st.session_state.spent += cost
                meta = f"{usage.prompt_tokens} in · {usage.completion_tokens} out · {cached} cached · ≈ ${cost:.6f}{note}"
            else:
                meta = f"No usage returned{note}"
            st.caption(meta)
            st.session_state.calls += 1
            st.session_state.messages.append({"role": "assistant", "content": reply, "meta": meta})
        except APIStatusError as err:
            st.session_state.messages.pop()  # don't keep the unanswered prompt in history
            try:
                detail = err.response.json().get("error", {})
                text = f"**HTTP {err.status_code} · {detail.get('code', '')}**  \n{detail.get('message', err.message)}"
            except ValueError:
                text = f"**HTTP {err.status_code}**  \n{err.message}"
            st.error(text)
