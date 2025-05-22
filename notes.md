- tick spacing = 10 (i.e, +/-.1%)
- Stable Pool: x³y + y³x ≥ k
- 0.05% fee
- https://blog.chain.link/introducing-chainlink-runtime-environment/
    - ^ integrate Chainlink feed into on-chain rebalancing bots?
- Calculating current holdings for a position requires some nuance due to the pool's curve not being constant product
- ^Need this in order to calculate IL + backtest current strategy
    - Derive from first principles by using prior work below:
    - https://atiselsts.github.io/pdfs/uniswap-v3-liquidity-math.pdf
    - https://blog.uniswap.org/uniswap-v3-math-primer-2


TODO:
- Fix swap gas estimation
- Fix PriceProvider methods which get all historical prices