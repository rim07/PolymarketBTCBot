//! Minimal CLI wrapper around polymarket_client_sdk_v2 for placing a single
//! GTC limit order via the Poly1271/deposit-wallet flow. Python's clob_client.py
//! shells out to this binary because no Python or JS client can currently
//! produce the ERC-7739 signature this flow requires (confirmed against
//! Polymarket/py-clob-client-v2#111 -- open, no fix, Rust SDK is the only
//! one that implements it).
//!
//! Reads secrets from env, not CLI args, so they never show up in process
//! listings or shell history:
//!   PK             - private key of the EOA that signs orders
//!   FUNDER         - deployed deposit wallet address (see setup_deposit_wallet.py)
//!   CLOB_API_URL   - optional, defaults to https://clob-v2.polymarket.com
//!
//! Order parameters come via CLI args. Always prints exactly one JSON object
//! to stdout: {"ok": true, "order_id": "...", "status": "..."} on success,
//! or {"ok": false, "error": "..."} on failure -- exit code mirrors ok.

use std::str::FromStr as _;

use alloy::signers::Signer as _;
use alloy::signers::local::LocalSigner;
use clap::Parser;
use polymarket_client_sdk_v2::POLYGON;
use polymarket_client_sdk_v2::clob::types::{OrderType, Side, SignatureType};
use polymarket_client_sdk_v2::clob::{Client, Config};
use polymarket_client_sdk_v2::types::{Address, Decimal, U256};
use serde::Serialize;

#[derive(Parser)]
struct Args {
    #[arg(long)]
    token_id: String,
    #[arg(long, value_parser = ["buy", "sell"])]
    side: String,
    #[arg(long)]
    price: String,
    #[arg(long)]
    size: String,
}

#[derive(Serialize)]
struct OutOk {
    ok: bool,
    order_id: String,
    status: String,
}

#[derive(Serialize)]
struct OutErr {
    ok: bool,
    error: String,
}

async fn run(args: Args) -> anyhow::Result<(String, String)> {
    let host = std::env::var("CLOB_API_URL").unwrap_or_else(|_| "https://clob-v2.polymarket.com".into());
    let private_key = std::env::var("PK").map_err(|_| anyhow::anyhow!("PK env var not set"))?;
    let deposit_wallet = std::env::var("FUNDER").map_err(|_| anyhow::anyhow!("FUNDER env var not set"))?;

    let token_id = U256::from_str(&args.token_id)?;
    let deposit_wallet = Address::from_str(&deposit_wallet)?;
    let price = Decimal::from_str(&args.price)?;
    let size = Decimal::from_str(&args.size)?;
    let side = if args.side == "buy" { Side::Buy } else { Side::Sell };

    let signer = LocalSigner::from_str(&private_key)?.with_chain_id(Some(POLYGON));

    let client = Client::new(&host, Config::default())?
        .authentication_builder(&signer)
        .funder(deposit_wallet)
        .signature_type(SignatureType::Poly1271)
        .authenticate()
        .await?;

    let resp = client
        .limit_order()
        .token_id(token_id)
        .side(side)
        .price(price)
        .size(size)
        .order_type(OrderType::GTC)
        .build_sign_and_post(&signer)
        .await?;

    Ok((resp.order_id.to_string(), resp.status.to_string()))
}

#[tokio::main]
async fn main() {
    let args = Args::parse();
    match run(args).await {
        Ok((order_id, status)) => {
            println!("{}", serde_json::to_string(&OutOk { ok: true, order_id, status }).unwrap());
        }
        Err(e) => {
            println!("{}", serde_json::to_string(&OutErr { ok: false, error: e.to_string() }).unwrap());
            std::process::exit(1);
        }
    }
}
