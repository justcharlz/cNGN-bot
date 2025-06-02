- tick spacing = 10 (i.e, +/-.1%)
- Stable Pool: x³y + y³x ≥ k
- 0.05% fee
- https://blog.chain.link/introducing-chainlink-runtime-environment/
    - ^ integrate Chainlink feed into on-chain rebalancing bots?
- Calculating current holdings for a position requires some nuance due to the pool's curve not being constant product(?)
- ^Need this in order to calculate IL + backtest current strategy
    - Derive from first principles by using prior work below:
    - https://atiselsts.github.io/pdfs/uniswap-v3-liquidity-math.pdf
    - https://blog.uniswap.org/uniswap-v3-math-primer-2

- Current impl moves x=100% of liquidity during rebalancing
    - Whats the best way to model/think about adjusting x?
    - Suppose LP position @ [a,b]
    - If rebalance occurs w/ rising active tick past b, then position is entirely USDC
    - If rebalance occurs w/ falling active tick below a, then position is entirely cNGN

- Current impl fails to mint a new position if price moves such that cNGN reserves < USDC reserves (i.e, 1 cNGN is worth more than 1 USDC)
    - Will burn the current position since a rebalance is triggered right after the swap which causes ^
    - New mint will fail though
    - Assuming that USDC should always be stronger than cNGN, then this seems more like a feature than a bug



HOW TO RUN TESTS:
1. Run "anvil --fork-url ..." and replace "..." with a Base mainnet RPC URL
2. Open a new terminal window and go to cNGN directory
3. To run all tests: "python3 -m unittest discover -s bot/tests"
    a. To run a specific test, run "python3 -m unittest bot.tests.test_..._anvil"
    b. Make sure to start a new fork after running a specific test; ideally want clean fork state at the start of each test
