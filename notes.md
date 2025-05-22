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
  File "/Users/johnbeecher/Desktop/cNGN/bot/test_strategy_anvil.py", line 282, in test_02b_price_provider_fetch_new_prices_with_swaps
    self.assertGreater(len(pp.all_prices_chronological), initial_history_len,
AssertionError: 119 not greater than 119 : Full price history did not grow after executing a swap and fetching new prices.

======================================================================
FAIL: test_07_rebalance_after_price_moves_out (test_strategy_anvil.TestStrategyAnvil.test_07_rebalance_after_price_moves_out)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/johnbeecher/Desktop/cNGN/bot/test_strategy_anvil.py", line 381, in test_07_rebalance_after_price_moves_out
    self.assertTrue(price_moved_out, f"Failed to move price out of initial range {initial_pos_range} after {max_swap_attempts} swaps. Final tick: {current_tick_after_swap}")
AssertionError: False is not true : Failed to move price out of initial range (-74030, -73690) after 10 swaps. Final tick: -73858

----------------------------------------------------------------------
Ran 10 tests in 406.048s

FAILED (failures=2)