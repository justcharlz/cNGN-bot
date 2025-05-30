import time
import logging
from web3 import Web3
import bot.config as config
from bot.utils.blockchain_utils import load_contract
from decimal import Decimal

logger = logging.getLogger(__name__)

# --- Read Functions ---
def get_pool_info(pool_contract):
    try:
        
        token0 = pool_contract.functions.token0().call()
        token1 = pool_contract.functions.token1().call()
        tick_spacing = pool_contract.functions.tickSpacing().call()
        # Aerodrome pool contracts returns this in pips (i.e, 1/100th of 1%)
        fee = pool_contract.functions.fee().call()
        fee = Decimal(fee)/Decimal(1e6)
        logger.info(f"Pool Info: Token0={token0}, Token1={token1}, TickSpacing={tick_spacing}, Fee={fee}")
        return {
            "token0": token0, "token1": token1,
            "tickSpacing": tick_spacing, "fee": fee
        }
    except Exception as e:
        logger.error(f"Error fetching pool info for {pool_contract.address}: {e}")
        return None

def get_current_pool_state(pool_contract):
    if not pool_contract:
        logger.error("Pool contract not loaded for get_current_pool_state.")
        return None
    try:
        slot0_data = pool_contract.functions.slot0().call()
        logger.debug(f"Raw Slot0 data: {slot0_data}")
        # Corrected parsing for 6 fields based on user's note
        return {
            "sqrtPriceX96": slot0_data[0],
            "tick": slot0_data[1],
            "observationIndex": slot0_data[2],
            "observationCardinality": slot0_data[3],
            "observationCardinalityNext": slot0_data[4],
            "unlocked": slot0_data[5]
        }
    except Exception as e:
        logger.error(f"Error fetching slot0 from pool {pool_contract.address}: {e}", exc_info=True)
        return None

def get_position_details(nft_manager_contract, token_id):
    if not nft_manager_contract:
        logger.error("NFT manager contract not loaded for get_position_details.")
        return None
    try:
        pos_details = nft_manager_contract.functions.positions(token_id).call()
        logger.info(f"Position details for tokenId {token_id}: {pos_details}")
        return {
            "nonce": pos_details[0], "operator": pos_details[1],
            "token0": pos_details[2], "token1": pos_details[3],
            "tickSpacing": pos_details[4], "tickLower": pos_details[5], # In original it was fee, assuming it is tickSpacing based on context
            "tickUpper": pos_details[6], "liquidity": pos_details[7],
            "feeGrowthInside0LastX128": pos_details[8],
            "feeGrowthInside1LastX128": pos_details[9],
            "tokensOwed0": pos_details[10], "tokensOwed1": pos_details[11]
        }
    except Exception as e:
        logger.error(f"Error fetching position details for tokenId {token_id}: {e}")
        return None

def get_owner_token_ids(nft_manager_contract, owner_address):
    if not nft_manager_contract:
        logger.error("NFT manager contract not loaded for get_owner_token_ids.")
        return []
    token_ids = []
    try:
        balance = nft_manager_contract.functions.balanceOf(owner_address).call()
        logger.info(f"Address {owner_address} owns {balance} position NFTs.")
        for i in range(balance):
            try:
                token_id = nft_manager_contract.functions.tokenOfOwnerByIndex(owner_address, i).call()
                token_ids.append(token_id)
            except Exception as e_idx:
                logger.error(f"Error fetching tokenId at index {i} for owner {owner_address}: {e_idx}")
        logger.info(f"Token IDs for {owner_address}: {token_ids}")
        return token_ids
    except Exception as e:
        logger.error(f"Error fetching token balance for owner {owner_address}: {e}")
        return []

# --- Transaction Helper Functions ---
def build_eip1559_transaction_params(w3, from_address, to_address=None, value_wei=0, data=None):
    try:
        latest_block = w3.eth.get_block('latest')
        base_fee_per_gas = latest_block.get('baseFeePerGas')

        if base_fee_per_gas is None:
            logger.warning("baseFeePerGas not found. Falling back to legacy gasPrice.")
            return {'from': from_address, 'nonce': w3.eth.get_transaction_count(from_address),
                    'gasPrice': w3.eth.gas_price, 'value': value_wei, 'chainId': config.CHAIN_ID, 'data': data}

        suggested_tip = w3.eth.max_priority_fee
        min_priority_fee_gwei = getattr(config, 'EIP1559_MIN_PRIORITY_FEE_GWEI', 1.0)
        min_priority_fee_wei = Web3.to_wei(min_priority_fee_gwei, 'gwei')
        max_priority_fee_per_gas = max(suggested_tip, min_priority_fee_wei)
        
        base_fee_multiplier = getattr(config, 'EIP1559_MAX_FEE_BASE_MULTIPLIER', 1.5)
        max_fee_per_gas = int(base_fee_multiplier * base_fee_per_gas) + max_priority_fee_per_gas
        
        if max_fee_per_gas < (base_fee_per_gas + max_priority_fee_per_gas): # Safety check
             max_fee_per_gas = base_fee_per_gas + max_priority_fee_per_gas
        
        logger.debug(f"EIP-1559: baseFee={base_fee_per_gas}, priorityFee={max_priority_fee_per_gas}, maxFee={max_fee_per_gas}")
        tx_params = {'from': from_address, 'nonce': w3.eth.get_transaction_count(from_address),
                     'maxPriorityFeePerGas': max_priority_fee_per_gas, 'maxFeePerGas': max_fee_per_gas,
                     'value': value_wei, 'chainId': config.CHAIN_ID}
        if to_address: tx_params['to'] = to_address
        if data: tx_params['data'] = data
        return tx_params
    except Exception as e:
        logger.error(f"Error building EIP-1559 params: {e}. Falling back to legacy.", exc_info=True)
        return {'from': from_address, 'nonce': w3.eth.get_transaction_count(from_address),
                'gasPrice': w3.eth.gas_price, 'value': value_wei, 'chainId': config.CHAIN_ID, 'data': data}

def sign_and_send_transaction(w3, transaction, private_key):
    try:
        if 'gas' not in transaction or transaction['gas'] is None or transaction['gas'] == 0 :
            logger.error("Transaction 'gas' limit not set or invalid before signing.")
            return None
        signed_txn = w3.eth.account.sign_transaction(transaction, private_key)
        txn_hash = w3.eth.send_raw_transaction(signed_txn.rawTransaction)
        logger.info(f"Transaction sent: {txn_hash.hex()}")
        txn_receipt = w3.eth.wait_for_transaction_receipt(txn_hash, timeout=300)
        status_msg = 'Success' if txn_receipt.status == 1 else 'Failed'
        logger.info(f"Tx confirmed. Block: {txn_receipt.blockNumber}, Status: {status_msg}, GasUsed: {txn_receipt.gasUsed}")
        if txn_receipt.status == 0:
            logger.error(f"Transaction REVERTED: {txn_receipt}")
            # Attempt to get revert reason if Anvil/Hardhat style provider and debug_traceTransaction is available
            tx_hash_hex = txn_hash.hex()
            try:
                trace = w3.provider.make_request("debug_traceTransaction", [tx_hash_hex, {}])
                if trace and trace.get('failed', False) and trace.get('returnValue'):
                    revert_reason = trace.get('returnValue')
                    # Try to decode known error selectors if it's a hex string
                    if revert_reason.startswith('0x08c379a0'): # Error(string)
                        try:
                            decoded_reason = w3.codec.decode(['string'], bytes.fromhex(revert_reason[10:]))[0]
                            logger.error(f"Revert reason from trace: Error({decoded_reason})")
                        except Exception as e_dec:
                            logger.error(f"Could not decode Error(string) revert reason {revert_reason}: {e_dec}")
                    else:
                        logger.error(f"Revert reason (raw) from trace: {revert_reason}")
            except Exception as e_trace:
                logger.debug(f"Could not get debug_traceTransaction or decode revert reason: {e_trace}")
            return None
        return txn_receipt
    except Exception as e:
        logger.error(f"Error signing/sending transaction: {e}", exc_info=True)
        return None

# --- Write Functions (Refactored Gas Estimation) ---

def _estimate_and_build_tx(w3, contract_function_prepared_with_args, from_address, to_contract_address, value_wei=0, encoded_data_override=None):
    """
    Internal helper to estimate gas and build full tx params.
    Expects contract_function_prepared_with_args to be the result of contract.functions.MyFunction(args)
    OR for encoded_data_override to be provided.
    """
    encoded_data = encoded_data_override
    fn_name_for_log = "unknown_function (data_override)" # Default if only override is used

    if not encoded_data:
        if not contract_function_prepared_with_args:
            logger.error("Either contract_function_prepared_with_args or encoded_data_override must be provided to _estimate_and_build_tx.")
            raise ValueError("No function or data to encode for transaction.")
        try:
            if hasattr(contract_function_prepared_with_args, 'fn_name'):
                fn_name_for_log = contract_function_prepared_with_args.fn_name
            else:
                fn_name_for_log = 'unknown_function_obj'
            
            logger.debug(f"Attempting to call encode_abi() on object of type: {type(contract_function_prepared_with_args)} for function: {fn_name_for_log}")
            encoded_data = contract_function_prepared_with_args.encode_abi()
            logger.debug(f"Successfully called encode_abi() for {fn_name_for_log}")

        except AttributeError as ae:
            logger.error(f"AttributeError on encode_abi for {fn_name_for_log}: {ae}. Object type: {type(contract_function_prepared_with_args)}", exc_info=True)
            raise 
        except Exception as e:
            logger.error(f"Error encoding ABI for {fn_name_for_log} using .encode_abi(): {e}", exc_info=True)
            raise
    elif contract_function_prepared_with_args and hasattr(contract_function_prepared_with_args, 'fn_name'):
        fn_name_for_log = contract_function_prepared_with_args.fn_name
    
    if not encoded_data:
        logger.error(f"Failed to obtain encoded_data for {fn_name_for_log}.")
        return None 

    gas_estimation_dict = {'from': from_address, 'to': to_contract_address, 'value': value_wei, 'data': encoded_data}
    
    try:
        estimated_gas = w3.eth.estimate_gas(gas_estimation_dict)
        logger.info(f"Raw estimated gas for {fn_name_for_log}: {estimated_gas}")
    except Exception as e:
        logger.error(f"Gas estimation failed for {fn_name_for_log}: {e}", exc_info=True)
        # Log common revert reasons if available in exception (depends on Web3.py version and provider)
        if hasattr(e, 'message') and e.message: logger.error(f"GasEst Revert Msg: {e.message}")
        if hasattr(e, 'data') and e.data: logger.error(f"GasEst Revert Data: {e.data}")
        return None 

    tx_params = build_eip1559_transaction_params(w3, from_address, 
                                                 to_address=to_contract_address, 
                                                 value_wei=value_wei) 
    
    tx_params['gas'] = int(estimated_gas * getattr(config, 'GAS_LIMIT_MULTIPLIER', 1.2))
    tx_params['data'] = encoded_data
    return tx_params


def execute_router_swap(w3: Web3, router_contract, account_details: dict,
                        token_in_address: str, token_out_address: str, tick_spacing: int,
                        recipient_address: str, amount_in: int, amount_out_minimum: int,
                        sqrt_price_limit_x96: int, deadline_offset_seconds: int = 600):
    """
    Executes a swap on a Uniswap V3-like router using exactInputSingle.
    """
    logger.info(f"Executing router swap: {amount_in} of {token_in_address} for {token_out_address}. Recipient: {recipient_address}")

    deadline = int(time.time()) + deadline_offset_seconds

    # Ensure addresses are checksummed
    token_in_cs = Web3.to_checksum_address(token_in_address)
    token_out_cs = Web3.to_checksum_address(token_out_address)
    recipient_cs = Web3.to_checksum_address(recipient_address)
    
    # ExactInputSingleParams struct:
    # (address tokenIn, address tokenOut, uint24 fee, address recipient, uint256 deadline,
    #  uint256 amountIn, uint256 amountOutMinimum, uint160 sqrtPriceLimitX96)
    params_tuple = (
        token_in_cs,
        token_out_cs,
        tick_spacing,
        recipient_cs,
        deadline,
        amount_in,
        amount_out_minimum,
        sqrt_price_limit_x96
    )

    base_tx_params = {
        'from': account_details['address'],
        'nonce': w3.eth.get_transaction_count(account_details['address']),
        'chainId': w3.eth.chain_id  # Use w3.eth.chain_id for current chain
    }
    latest_block = w3.eth.get_block('latest')
    base_fee = latest_block.get('baseFeePerGas', w3.to_wei(1, 'gwei')) # Fallback for non-EIP1559 chains or testing
    
    # EIP-1559 fee logic (simplified from original, can use build_eip1559_transaction_params for more robustness)
    base_tx_params['maxPriorityFeePerGas'] = w3.to_wei(getattr(config, 'EIP1559_MIN_PRIORITY_FEE_GWEI', 1.5), 'gwei')
    base_tx_params['maxFeePerGas'] = base_fee * 2 + base_tx_params['maxPriorityFeePerGas']

    router_swap_function_call = router_contract.functions.exactInputSingle(params_tuple)
    
    final_tx_params = base_tx_params.copy()
    transaction_to_sign = router_swap_function_call.build_transaction(final_tx_params)
    
    pk_hex = account_details['pk']
    signed_tx = w3.eth.account.sign_transaction(transaction_to_sign, pk_hex)
    
    tx_hash_hex = ""
    try:
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        tx_hash_hex = tx_hash.hex()
        logger.info(f"Swap transaction sent: {tx_hash_hex}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=getattr(config, 'TX_TIMEOUT_SECONDS', 180))
    except Exception as e_tx:
        logger.error(f"Error sending swap transaction or waiting for receipt (Hash if available: {tx_hash_hex}): {e_tx}", exc_info=True)
        raise # Re-raise to allow calling test to fail

    if receipt.status == 0:
        logger.error(f"Router Swap transaction {tx_hash_hex} FAILED. Receipt: {receipt}")
        # Try to get revert reason if Anvil supports it (or other debug-enabled nodes)
        try:
            trace = w3.provider.make_request("debug_traceTransaction", [tx_hash_hex, {}])
            if trace and trace.get('failed', False) and trace.get('returnValue'):
                 logger.error(f"Revert reason from trace: {trace.get('returnValue')}")
        except Exception as e_trace:
            logger.warning(f"Could not get debug_traceTransaction: {e_trace}")
        # It's good practice to raise an exception here if the transaction failed
        raise Exception(f"Router Swap transaction failed. Hash: {tx_hash_hex}. Receipt: {receipt}")
    
    logger.info(f"Router Swap executed successfully. Hash: {tx_hash_hex}, GasUsed: {receipt.gasUsed}")
    return receipt


def approve_token_if_needed(w3, token_address, owner_address, spender_address, amount_to_approve, account_private_key):
    token_contract = load_contract(w3, token_address, config.MINIMAL_ERC20_ABI)
    if not token_contract: return False
    try:
        current_allowance = token_contract.functions.allowance(owner_address, spender_address).call()
        logger.info(f"Allowance for {spender_address} by {owner_address} on {token_address}: {current_allowance}")
        if current_allowance < amount_to_approve:
            logger.info(f"Approving {amount_to_approve} for {token_address}...")
            
            encoded_data_for_approve = None
            approve_function_object_for_logging = None
            try:
                approve_function_object_for_logging = token_contract.functions.approve(spender_address, amount_to_approve)
            except Exception as e_obj:
                logger.warning(f"Could not create approve function object for logging: {e_obj}")

            try:
                encoded_data_for_approve = token_contract.encode_abi(fn_name="approve", args=[spender_address, amount_to_approve])
                logger.info(f"Successfully generated encoded_data for 'approve' via contract.encode_abi().")
            except Exception as e:
                logger.error(f"Critical error: Failed to generate encoded_data for 'approve' using contract.encode_abi(): {e}", exc_info=True)
                if approve_function_object_for_logging:
                    logger.error(f"Type of 'token_contract.functions.approve(args)' object was: {type(approve_function_object_for_logging)}")
                    try: logger.error(f"Attributes of this object: {dir(approve_function_object_for_logging)}")
                    except: pass 
                return False

            final_tx_params = _estimate_and_build_tx(
                w3, 
                approve_function_object_for_logging, 
                owner_address, 
                token_contract.address,
                encoded_data_override=encoded_data_for_approve
            )
            if not final_tx_params: 
                logger.error(f"Failed to build transaction parameters for approve call to {token_address}.")
                return False 

            receipt = sign_and_send_transaction(w3, final_tx_params, account_private_key)
            return receipt is not None and receipt.status == 1
        else:
            logger.info("Sufficient allowance present.")
            return True
    except Exception as e:
        logger.error(f"Error in approve_token_if_needed for {token_address}: {e}", exc_info=True)
        return False

def mint_new_position(w3, nft_manager_contract, pool_contract, params, account, wallet_address):
    if not nft_manager_contract or not pool_contract: 
        logger.error("NFT manager or Pool contract not loaded for minting.")
        return None
    logger.info(f"Attempting to mint with params: {params}")

    MAX_UINT256 = 2**256 - 1
    token0_addr = Web3.to_checksum_address(params[0])
    token1_addr = Web3.to_checksum_address(params[1])

    if not approve_token_if_needed(w3, token0_addr, wallet_address, nft_manager_contract.address, MAX_UINT256, account.key):
        logger.error(f"Approval failed for token0 ({token0_addr}) within mint_new_position")
        return None
    if not approve_token_if_needed(w3, token1_addr, wallet_address, nft_manager_contract.address, MAX_UINT256, account.key):
        logger.error(f"Approval failed for token1 ({token1_addr}) within mint_new_position")
        return None
    logger.info("Token approvals successful for mint_new_position.")

    mint_params_tuple = (
        token0_addr,
        token1_addr,
        params[2], # tickspacing
        params[3], # tickLower
        params[4], # tickUpper
        params[5], # amount0Desired
        params[6], # amount1Desired
        params[7], # amount0Min
        params[8], # amount1Min
        Web3.to_checksum_address(params[9]), # recipient
        params[10],# deadline
        0 # sqrtprice limit (dont care for mints)
    )

    try:
        mint_function_object_for_logging = None
        try:
            # The 'mint' function in the ABI takes a single 'params' struct/tuple
            mint_function_object_for_logging = nft_manager_contract.functions.mint(mint_params_tuple)
        except Exception as e_obj:
            logger.warning(f"Could not create mint function object for logging: {e_obj}")

        encoded_data_for_mint = None
        try:
            encoded_data_for_mint = nft_manager_contract.encode_abi(fn_name="mint", args=[mint_params_tuple]) 
            logger.info(f"Successfully generated encoded_data for 'mint' via contract.encode_abi().")
        except Exception as e:
            logger.error(f"Critical error: Failed to generate encoded_data for 'mint' using contract.encode_abi(): {e}", exc_info=True)
            if mint_function_object_for_logging:
                logger.error(f"Type of 'nft_manager_contract.functions.mint(args)' object was: {type(mint_function_object_for_logging)}")
                try: logger.error(f"Attributes of this object: {dir(mint_function_object_for_logging)}")
                except: pass
            return None
        
        final_tx_params = _estimate_and_build_tx(
            w3, 
            mint_function_object_for_logging,
            wallet_address,
            nft_manager_contract.address,
            encoded_data_override=encoded_data_for_mint
        )
        
        if not final_tx_params:
            logger.error("Failed to build transaction for mint (gas estimation or param build failed).")
            return None

        logger.info(f"Sending mint transaction with calculated gas and EIP-1559 params: {final_tx_params}")
        receipt = sign_and_send_transaction(w3, final_tx_params, account.key)

        if receipt and receipt.status == 1:
            logger.info("Mint transaction successful.")
            return receipt
        else:
            logger.error(f"Mint transaction failed or no receipt. Receipt: {receipt}")
            return None
    except Exception as e:
        logger.error(f"Exception during mint_new_position: {e}", exc_info=True)
        return None

def decrease_liquidity_and_collect_position(w3, nft_manager_contract, token_id, liquidity_to_decrease,
                                            amount0_min, amount1_min, recipient_address,
                                            account, wallet_address):
    if not nft_manager_contract: return None
    deadline = int(time.time()) + 300 
    checksum_recipient = Web3.to_checksum_address(recipient_address)

    logger.info(f"Decreasing liquidity for tokenId {token_id} by {liquidity_to_decrease}")
    decrease_params_struct = { # Struct for decreaseLiquidity
        "tokenId": token_id,
        "liquidity": liquidity_to_decrease,
        "amount0Min": amount0_min,
        "amount1Min": amount1_min,
        "deadline": deadline
    }
    
    try:
        decrease_object_for_logging = None
        try:
            decrease_object_for_logging = nft_manager_contract.functions.decreaseLiquidity(decrease_params_struct)
        except Exception as e_obj:
            logger.warning(f"Could not create decreaseLiquidity function object for logging: {e_obj}")

        encoded_data_for_decrease = None
        try:
            encoded_data_for_decrease = nft_manager_contract.encode_abi(fn_name="decreaseLiquidity", args=[decrease_params_struct])
            logger.info(f"Successfully generated encoded_data for 'decreaseLiquidity' via contract.encode_abi().")
        except Exception as e:
            logger.error(f"Critical error: Failed to generate encoded_data for 'decreaseLiquidity': {e}", exc_info=True)
            if decrease_object_for_logging:
                logger.error(f"Type of 'decreaseLiquidity(args)' object was: {type(decrease_object_for_logging)}")
                try: logger.error(f"Attributes: {dir(decrease_object_for_logging)}")
                except: pass
            return False

        final_tx_params_decrease = _estimate_and_build_tx(
            w3, 
            decrease_object_for_logging, 
            wallet_address, 
            nft_manager_contract.address,
            encoded_data_override=encoded_data_for_decrease
        )
        if not final_tx_params_decrease: 
            logger.error(f"Failed to build transaction for decreaseLiquidity on token {token_id}.")
            return False
        
        receipt_decrease = sign_and_send_transaction(w3, final_tx_params_decrease, account.key)
        if not (receipt_decrease and receipt_decrease.status == 1):
            logger.error("Decrease liquidity transaction failed.")
            return False
        logger.info("Decrease liquidity successful.")
    except Exception as e:
        logger.error(f"Error during decreaseLiquidity for tokenId {token_id}: {e}", exc_info=True)
        return False

    MAX_UINT128 = 2**128 - 1
    logger.info(f"Collecting fees for tokenId {token_id} to {checksum_recipient}")
    collect_params_struct = { # Struct for collect
        "tokenId": token_id,
        "recipient": checksum_recipient,
        "amount0Max": MAX_UINT128,
        "amount1Max": MAX_UINT128
    }
    
    try:
        collect_object_for_logging = None
        try:
            collect_object_for_logging = nft_manager_contract.functions.collect(collect_params_struct)
        except Exception as e_obj:
            logger.warning(f"Could not create collect function object for logging: {e_obj}")

        encoded_data_for_collect = None
        try:
            encoded_data_for_collect = nft_manager_contract.encode_abi(fn_name="collect", args=[collect_params_struct])
            logger.info(f"Successfully generated encoded_data for 'collect' via contract.encode_abi().")
        except Exception as e:
            logger.error(f"Critical error: Failed to generate encoded_data for 'collect': {e}", exc_info=True)
            if collect_object_for_logging:
                logger.error(f"Type of 'collect(args)' object was: {type(collect_object_for_logging)}")
                try: logger.error(f"Attributes: {dir(collect_object_for_logging)}")
                except: pass
            return False 

        final_tx_params_collect = _estimate_and_build_tx(
            w3, 
            collect_object_for_logging, 
            wallet_address, 
            nft_manager_contract.address,
            encoded_data_override=encoded_data_for_collect
        )
        if not final_tx_params_collect: 
            logger.error(f"Failed to build transaction for collect on token {token_id}.")
            return False

        receipt_collect = sign_and_send_transaction(w3, final_tx_params_collect, account.key)
        if not (receipt_collect and receipt_collect.status == 1):
            logger.error("Collect fees transaction failed.")
            return False 
        logger.info("Collect fees successful.")
        return True
    except Exception as e:
        logger.error(f"Error during collect for tokenId {token_id}: {e}", exc_info=True)
        return False

def burn_nft_position(w3, nft_manager_contract, token_id, account, wallet_address):
    if not nft_manager_contract: return False
    logger.info(f"Attempting to burn NFT tokenId {token_id}")
    
    try:
        burn_object_for_logging = None
        try:
            burn_object_for_logging = nft_manager_contract.functions.burn(token_id) 
        except Exception as e_obj:
            logger.warning(f"Could not create burn function object for logging: {e_obj}")

        encoded_data_for_burn = None
        try:
            encoded_data_for_burn = nft_manager_contract.encode_abi(fn_name="burn", args=[token_id])
            logger.info(f"Successfully generated encoded_data for 'burn' via contract.encode_abi().")
        except Exception as e:
            logger.error(f"Critical error: Failed to generate encoded_data for 'burn': {e}", exc_info=True)
            if burn_object_for_logging:
                logger.error(f"Type of 'burn(args)' object was: {type(burn_object_for_logging)}")
                try: logger.error(f"Attributes: {dir(burn_object_for_logging)}")
                except: pass
            return False

        final_tx_params = _estimate_and_build_tx(
            w3, 
            burn_object_for_logging, 
            wallet_address, 
            nft_manager_contract.address,
            encoded_data_override=encoded_data_for_burn
        )
        if not final_tx_params: 
            logger.error(f"Failed to build transaction for burn on token {token_id}.")
            return False

        receipt = sign_and_send_transaction(w3, final_tx_params, account.key)
        if receipt and receipt.status == 1:
            logger.info(f"NFT tokenId {token_id} burned successfully.")
            return True
        else:
            logger.error(f"Burn NFT transaction failed for tokenId {token_id}.")
            return False
    except Exception as e:
        logger.error(f"Error during burn_nft_position for tokenId {token_id}: {e}", exc_info=True)
        return False