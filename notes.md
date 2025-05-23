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
======================================================================
FAIL: test_02b_price_provider_fetch_new_prices_with_swaps (test_strategy_anvil.TestStrategyAnvil.test_02b_price_provider_fetch_new_prices_with_swaps)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/johnbeecher/Desktop/cNGN/bot/test_strategy_anvil.py", line 302, in test_02b_price_provider_fetch_new_prices_with_swaps
    self._execute_pool_swap(self.pool_token0_addr, amount_to_swap, execute_as_zero_for_one=True)
  File "/Users/johnbeecher/Desktop/cNGN/bot/test_strategy_anvil.py", line 246, in _execute_pool_swap
    self.assertEqual(receipt.status, 1, f"Swap transaction failed. Hash: {tx_hash.hex()}")
AssertionError: 0 != 1 : Swap transaction failed. Hash: 0xc2af20f9da462db28e23728accb2bda0daaede9d55bacc1bf7d0a1acb2563f83

======================================================================
FAIL: test_07_rebalance_after_price_moves_out (test_strategy_anvil.TestStrategyAnvil.test_07_rebalance_after_price_moves_out)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/johnbeecher/Desktop/cNGN/bot/test_strategy_anvil.py", line 398, in test_07_rebalance_after_price_moves_out
    self._execute_pool_swap(self.pool_token0_addr, amount_to_swap_t0, execute_as_zero_for_one=True)
  File "/Users/johnbeecher/Desktop/cNGN/bot/test_strategy_anvil.py", line 246, in _execute_pool_swap
    self.assertEqual(receipt.status, 1, f"Swap transaction failed. Hash: {tx_hash.hex()}")
AssertionError: 0 != 1 : Swap transaction failed. Hash: 0x343ac36e9752abffb5cecdcd47c1ea23e782095b1689a56a11afc34388e52921

----------------------------------------------------------------------
Ran 10 tests in 333.559s

FAILED (failures=2)