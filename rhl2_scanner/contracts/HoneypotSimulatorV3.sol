// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * HoneypotSimulatorV3 — buy tax + sellability + round-trip loss on Uniswap V3.
 *
 * Never deployed: runtime bytecode is injected at a scratch address via
 * `eth_call` stateOverride `code`, and a burner (funded via stateOverride
 * balance) calls `simulateV3` with `value`. In one call it wraps ETH, buys the
 * token via SwapRouter02.exactInputSingle, then sells all of it back.
 *
 * Reliable outputs:
 *   - buyTaxBps: the router reports the pool-side output; the token's transfer
 *     tax is the gap between that and what we actually receive. This isolates
 *     the BUY tax cleanly (pool fee is already excluded from amountOut).
 *   - roundTripBps: total cost to enter+exit (buy tax + sell tax + 2x pool fee +
 *     impact) = (amountIn - soldEth)/amountIn. An honest "what you'd lose"
 *     number; a huge value means honeypot-like / punitive tax.
 *   - ok: bought > 0 && soldEth > 0 (i.e. actually sellable).
 *
 * NOTE: exact SELL tax cannot be isolated on V3 without tx tracing (the pool's
 * received amount isn't observable in-call), so it is intentionally not
 * reported here — use roundTripBps + ok instead.
 *
 * Compile the runtime bytecode into chain.honeypot_simulator_bytecode:
 *   solc --optimize --bin-runtime --evm-version paris HoneypotSimulatorV3.sol
 */

interface IWETH {
    function deposit() external payable;
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
}

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
}

interface ISwapRouter02 {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params)
        external payable returns (uint256 amountOut);
}

contract HoneypotSimulatorV3 {
    receive() external payable {}

    function simulateV3(address token, address router, address weth, uint24 fee, uint256 amountInEth)
        external
        payable
        returns (uint256 buyTaxBps, uint256 roundTripBps, uint256 bought, uint256 soldEth, bool ok)
    {
        // Wrap and approve.
        IWETH(weth).deposit{value: amountInEth}();
        IWETH(weth).approve(router, amountInEth);

        // BUY: WETH -> token.
        uint256 balBefore = IERC20(token).balanceOf(address(this));
        uint256 amountOutBuy = ISwapRouter02(router).exactInputSingle(
            ISwapRouter02.ExactInputSingleParams(weth, token, fee, address(this), amountInEth, 0, 0)
        );
        bought = IERC20(token).balanceOf(address(this)) - balBefore;
        if (amountOutBuy > 0 && bought < amountOutBuy) {
            buyTaxBps = ((amountOutBuy - bought) * 10000) / amountOutBuy;
        }

        // SELL: token -> WETH.
        IERC20(token).approve(router, bought);
        soldEth = bought > 0
            ? ISwapRouter02(router).exactInputSingle(
                ISwapRouter02.ExactInputSingleParams(token, weth, fee, address(this), bought, 0, 0))
            : 0;

        if (amountInEth > 0 && soldEth < amountInEth) {
            roundTripBps = ((amountInEth - soldEth) * 10000) / amountInEth;
        }
        ok = bought > 0 && soldEth > 0;
    }
}
