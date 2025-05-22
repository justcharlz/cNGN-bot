import logging
from collections import deque
import pandas as pd # Added import
import matplotlib.pyplot as plt # Added import
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
topic0 = w3.keccak(text=sig).hex()

# ─── Fetch + decode logs, queueing rate-limited ranges ─────────────────────────
latest_block = w3.eth.block_number


logging.info("Latest block is %d; scanning from %d in batches of %d", latest_block, START_BLOCK, BATCH_SIZE)

fetched_prices_data = [] # Renamed from 'prices' to avoid conflict with df column name
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
        if e.response.status_code == 429: # pragma: no cover
            logging.warning("Rate limited on blocks %d → %d; enqueueing for retry", from_blk, to_blk)
            retry_queue.append((from_blk, to_blk))
        else: # pragma: no cover
            logging.error("HTTP error fetching %d→%d: %s", from_blk, to_blk, e)
        return
    except Exception as e: # pragma: no cover
        logging.error("Error fetching logs for %d→%d: %s", from_blk, to_blk, e)
        return

    for log in raw_logs:
        try:
            ev = pool.events.Swap().process_log(log)
            sqrtP = ev["args"]["sqrtPriceX96"]
            price = (sqrtP / Q96) ** 2
            if invert_price:
                price = 1.0 / price

            fetched_prices_data.append({
                "block":     log["blockNumber"],
                "tx_index":  log["transactionIndex"],
                "log_index": log["logIndex"],
                "price":     price
            })
        except Exception as e: # pragma: no cover
            logging.error(f"Error processing log: {log} - {e}")


# Initial fetch loop
for start in range(START_BLOCK, latest_block + 1, BATCH_SIZE):
    end = min(start + BATCH_SIZE - 1, latest_block)
    logging.info("Fetching blocks %d → %d", start, end)
    fetch_and_process(start, end)

# Retry any rate-limited ranges
while retry_queue: # pragma: no cover
    start, end = retry_queue.popleft()
    logging.info("Retrying blocks %d → %d", start, end)
    fetch_and_process(start, end)

# ─── Sort data ────────────────────────────────────────────────────────────
fetched_prices_data.sort(key=lambda x: (x["block"], x["tx_index"], x["log_index"]))

# ─── Perform EDA if data was fetched ───────────────────────────────────────
if fetched_prices_data:
    df_data = [(item['block'], item['price']) for item in fetched_prices_data]
    df = pd.DataFrame(df_data, columns=['block', 'price'])

    # Print first few rows
    print("\nFirst 10 observations from fetched data:")
    print(df.head(10))

    # Basic descriptive statistics
    print("\nDescriptive Statistics for Price:")
    print(df['price'].describe())

    # Plot 1: Price vs. Block
    plt.figure(figsize=(10, 6)) # Added figsize for better readability
    plt.plot(df['block'], df['price'], marker='.', linestyle='-') # Added marker and linestyle
    plt.title('Price over Block Numbers')
    plt.xlabel('Block Number')
    plt.ylabel('Price')
    plt.grid(True) # Added grid
    plt.show()

    # Plot 2: Histogram of Prices
    plt.figure(figsize=(10, 6))
    plt.hist(df['price'], bins=30, edgecolor='black') # Increased bins, added edgecolor
    plt.title('Histogram of Prices')
    plt.xlabel('Price')
    plt.ylabel('Frequency')
    plt.grid(axis='y', alpha=0.75) # Added grid
    plt.show()

    # Plot 3: Rolling Mean (window=5)
    # Ensure there are enough data points for the rolling window
    if len(df['price']) >= 5:
        df['rolling_mean'] = df['price'].rolling(window=5).mean()
        plt.figure(figsize=(10, 6))
        plt.plot(df['block'], df['price'], label='Actual Price', alpha=0.5, linestyle=':') # Plot actual price for comparison
        plt.plot(df['block'], df['rolling_mean'], label='5-Block Rolling Mean', color='red', linewidth=2) # Added label and color
        plt.title('5-Block Rolling Mean of Price')
        plt.xlabel('Block Number')
        plt.ylabel('Price / Rolling Mean Price')
        plt.legend() # Added legend
        plt.grid(True)
        plt.show()
    else:
        print("\nNot enough data points to calculate 5-block rolling mean.")

    print("\nEDA plots generated.")
else:
    print("\nNo price data fetched. Skipping EDA.")

logging.info("Script finished.")