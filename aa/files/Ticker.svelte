<script>
    import { onMount } from 'svelte';

    let { tickerText, scrollDuration = null } = $props();
    const trackedContent = $derived(tickerText);

    let pElement;
    let tickerStyle = $state('');

    // --- STATE ---
    let btcData = $state({
        price: '---',
        change: '---',
        percent: '---',
        isPositive: true,
        lastPrice: 0 // Used to track tick direction
    });

    let flashClass = $state(''); // For visual flash effect
    let socket;
    let reconnectTimer;

    const PIXELS_PER_SECOND = 80;

    // --- WEBSOCKET LOGIC ---
    const connectWebSocket = () => {
        // Coinbase Pro WebSocket (Public, No Key Required, US Friendly)
        socket = new WebSocket('wss://ws-feed.exchange.coinbase.com');

        socket.onopen = () => {
            console.log('Connected to Coinbase WS');
            // Subscribe to the real-time ticker channel
            const msg = {
                type: 'subscribe',
                product_ids: ['BTC-USD'],
                channels: ['ticker']
            };
            socket.send(JSON.stringify(msg));
        };

        socket.onmessage = (event) => {
            const data = JSON.parse(event.data);

            if (data.type === 'ticker' && data.price) {
                const currentPrice = parseFloat(data.price);
                const openPrice = parseFloat(data.open_24h);

                // Calculate 24h stats manually since stream gives raw price
                const changeAmt = currentPrice - openPrice;
                const changePct = (changeAmt / openPrice) * 100;

                // Determine Flash Color (Up/Down tick)
                if (btcData.lastPrice !== 0) {
                    flashClass = currentPrice > btcData.lastPrice ? 'flash-up' : 'flash-down';
                    // Remove flash class after 200ms
                    setTimeout(() => flashClass = '', 200);
                }

                btcData = {
                    price: currentPrice.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
                    change: Math.abs(changeAmt).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
                    percent: Math.abs(changePct).toFixed(2),
                    isPositive: changeAmt >= 0,
                    lastPrice: currentPrice
                };
            }
        };

        socket.onclose = () => {
            console.warn('WebSocket disconnected. Reconnecting in 3s...');
            reconnectTimer = setTimeout(connectWebSocket, 3000);
        };

        socket.onerror = (err) => {
            console.error('WebSocket Error:', err);
            socket.close(); // Force close to trigger reconnect logic
        };
    };

    // --- MARQUEE LOGIC ---
    $effect(() => {
        void trackedContent;
        if (pElement) {
            const textWidth = pElement.scrollWidth;
            const duration = scrollDuration ?? (textWidth / PIXELS_PER_SECOND);
            tickerStyle = `--ticker-width: ${textWidth}px; --marquee-duration: ${duration}s;`;
        }
    });

    onMount(() => {
        connectWebSocket();
        return () => {
            if (socket) socket.close();
            clearTimeout(reconnectTimer);
        };
    });
</script>

<div class='ticker'>
    <p class='ticker-txt' bind:this={pElement} style={tickerStyle}>
        {tickerText}
    </p>
    <div class='stock-info'>
        <div class='stock-symbol'>
            <p>BTC</p>
        </div>
        <div class="stock-price" style="display: flex;column-gap: 1rem">
            <span class={flashClass}>{btcData.price}</span>

            <span class={btcData.isPositive ? 'stock-change-positive' : 'stock-change-negative'}>
                {btcData.isPositive ? '+' : '-'}{btcData.change} ({btcData.percent}%)
            </span>
        </div>
    </div>
</div>

<style>
    /* ... previous styles ... */

    p { margin: 0; }

    .ticker {
        background: #000;
        font-size: 16pt;
        font-family: 'Oswald', sans-serif;
        font-weight: 100;
        width: 100%;
        height: 30px;
        overflow: hidden;
        position: relative;
        color: white;
    }

    .ticker .ticker-txt {
        position: absolute;
        line-height: 1.5em;
        letter-spacing: 1px;
        white-space: nowrap;
        width: var(--ticker-width);
        animation: marquee var(--marquee-duration, 60s) linear infinite;
    }

    .ticker .stock-info {
        background: linear-gradient(to right, rgba(8, 8, 8, 0) 0%, #080808 20%, #080808 100%);
        box-shadow: -20px 0 25px #080808;
        position: absolute;
        top: 0;
        right: 0;
        width: 300px;
        height: 35px;
        display: flex;
        align-items: center;
    }

    .ticker .stock-symbol {
        background: linear-gradient(#555, #111);
        clip-path: polygon(0 0, 100% 0, 85% 100%, 0% 100%);
        border: 1px solid #888;
        width: 75px;
        height: 28px;
        flex-shrink: 0;
        display: flex;
        align-items: center;
        justify-content: center;
    }

    .ticker .stock-symbol p {
        font-weight: 700;
        line-height: 1;
        transform: scale(0.9, 1);
        text-shadow: 0 0 4px black;
        color: #fff;
    }

    .stock-price {
        padding-left: 15px;
        display: flex;
        line-height: 1.1;
    }

    .ticker .stock-price span:first-child {
        font-weight: 700;
        font-size: 14pt;
        text-shadow: 0 1px 2px #000;
        transition: color 0.2s ease; /* Smooth color transition */
    }

    /* COLORS */
    .stock-change-positive { color: #00e676; font-size: 10pt; font-weight: 400; }
    .stock-change-negative { color: #ff1744; font-size: 10pt; font-weight: 400; }

    /* Flash Effects for milliseconds updates */
    .flash-up { color: #00e676 !important; text-shadow: 0 0 10px rgba(0, 230, 118, 0.6); }
    .flash-down { color: #ff1744 !important; text-shadow: 0 0 10px rgba(255, 23, 68, 0.6); }

    @keyframes marquee {
        from { transform: translateX(100vw); }
        to { transform: translateX(calc(-1 * var(--ticker-width))); }
    }
</style>