import logging
from collections import deque
from web3 import Web3
from requests.exceptions import HTTPError

# ─── Configuration ─────────────────────────────────────────────────────────────
RPC_URL          = "https://base-mainnet.infura.io/v3/35fbbf13c9134267aa47e7acd9242abf"
POOL_ADDRESS     = "0x0206B696a410277eF692024C2B64CcF4EaC78589"
CNGN_ADDRESS     = "0x46C85152bFe9f96829aA94755D9f915F9B10EF5F".lower()
START_BLOCK      = 28_028_682
BATCH_SIZE       = 10_000
Q96              = 2 ** 96

# ─── Minimal ABI ───────────────────────────────────────────────────────────────
POOL_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "internalType": "address", "name": "sender",      "type": "address"},
            {"indexed": True,  "internalType": "address", "name": "recipient",   "type": "address"},
            {"indexed": False, "internalType": "int256",  "name": "amount0",     "type": "int256"},
            {"indexed": False, "internalType": "int256",  "name": "amount1",     "type": "int256"},
            {"indexed": False, "internalType": "uint160", "name": "sqrtPriceX96","type": "uint160"},
            {"indexed": False, "internalType": "uint128", "name": "liquidity",   "type": "uint128"},
            {"indexed": False, "internalType": "int24",   "name": "tick",        "type": "int24"},
        ],
        "name": "Swap",
        "type": "event"
    },
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# ─── Setup ─────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    logging.error("Unable to connect to RPC at %s", RPC_URL)
    exit(1)

pool = w3.eth.contract(address=POOL_ADDRESS, abi=POOL_ABI)

# ─── Determine price inversion ─────────────────────────────────────────────────
token0 = pool.functions.token0().call().lower()
token1 = pool.functions.token1().call().lower()

if token0 == CNGN_ADDRESS:
    invert_price = True
    logging.info("cNGN is token0 → will invert price")
elif token1 == CNGN_ADDRESS:
    invert_price = False
    logging.info("cNGN is token1 → using direct price")
else:
    logging.error("Neither token0 nor token1 matches cNGN address %s", CNGN_ADDRESS)
    exit(1)

# ─── Precompute Swap event topic ────────────────────────────────────────────────
sig = "Swap(address,address,int256,int256,uint160,uint128,int24)"
# Compute correct topic0: keccak returns bytes, bytes.hex() yields hex string without "0x"
topic0 = "0x" + w3.keccak(text=sig).hex()

# ─── Fetch + decode logs, queueing rate-limited ranges ─────────────────────────
latest_block = w3.eth.block_number
logging.info("Latest block is %d; scanning from %d in batches of %d", latest_block, START_BLOCK, BATCH_SIZE)

prices = []
retry_queue = deque()

# Helper to process a batch of logs
def fetch_and_process(from_blk, to_blk):
    try:
        raw_logs = w3.eth.get_logs({
            "address": POOL_ADDRESS,
            "fromBlock": from_blk,
            "toBlock": to_blk,
            "topics": [topic0]
        })
    except HTTPError as e:
        if e.response.status_code == 429:
            logging.warning("Rate limited on blocks %d → %d; enqueueing for retry", from_blk, to_blk)
            retry_queue.append((from_blk, to_blk))
        else:
            logging.error("HTTP error fetching %d→%d: %s", from_blk, to_blk, e)
        return
    except Exception as e:
        logging.error("Error fetching logs for %d→%d: %s", from_blk, to_blk, e)
        return

    for log in raw_logs:
        ev = pool.events.Swap().process_log(log)
        sqrtP = ev["args"]["sqrtPriceX96"]
        price = (sqrtP / Q96) ** 2
        if invert_price:
            price = 1.0 / price

        prices.append({
            "block":     log["blockNumber"],
            "tx_index":  log["transactionIndex"],
            "log_index": log["logIndex"],
            "price":     price
        })

# Initial fetch loop
for start in range(START_BLOCK, latest_block + 1, BATCH_SIZE):
    end = min(start + BATCH_SIZE - 1, latest_block)
    logging.info("Fetching blocks %d → %d", start, end)
    fetch_and_process(start, end)

# Retry any rate-limited ranges
while retry_queue:
    start, end = retry_queue.popleft()
    logging.info("Retrying blocks %d → %d", start, end)
    fetch_and_process(start, end)

# ─── Sort and output ────────────────────────────────────────────────────────────
prices.sort(key=lambda x: (x["block"], x["tx_index"], x["log_index"]))
for p in prices:
    print(f"Block {p['block']}: price = {p['price']:.8f}")
