# Not currently used

This was built while investigating a fix for py-clob-client-v2's inability to
place orders for deposit-wallet accounts ("invalid order version" / "maker
address not allowed"). It compiles (verified with the GNU Rust toolchain on
Windows) but was never run against a real account, because `polymarket-client`
(see the main project's `clob_client.py`) turned out to already implement the
required ERC-7739 signing in Python — no Rust needed after all.

Kept as a fallback in case `polymarket-client`'s order placement doesn't work
for your account for some reason not yet discovered. If you never need it,
it's safe to delete this directory.
