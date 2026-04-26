<script>
    import { onDestroy } from 'svelte';

    let serverUrl = 'ws://127.0.0.1:5100/ws';
    let socket = null;
    let status = 'Disconnected';
    let output = '';

    function connect() {
        if (socket) return;
        status = `Connecting to ${serverUrl}...`;
        output = `[SYSTEM] Connecting...\n`;
        console.log("Attempting to connect to WebSocket...");

        try {
            socket = new WebSocket(serverUrl);

            socket.onopen = () => {
                status = 'Connected';
                console.log("WebSocket connection opened successfully.");
                output += `[SYSTEM] Connection successful.\n`;
            };

            socket.onmessage = (e) => {
                console.log("WebSocket message received:", e.data);
                output += `[SERVER] ${e.data}\n`;
            };

            socket.onclose = () => {
                status = 'Disconnected';
                socket = null;
                console.log("WebSocket connection closed.");
                output += `[SYSTEM] Connection closed.\n`;
            };

            socket.onerror = (e) => {
                status = 'Error';
                console.error("WebSocket error:", e);
                output += `[SYSTEM] A WebSocket error occurred.\n`;
            };
        } catch (e) {
            status = 'Error';
            output += `[SYSTEM] Error: ${e.message}\n`;
            console.error("Failed to create WebSocket:", e);
        }
    }

    function disconnect() {
        if (socket) socket.close();
    }

    onDestroy(() => {
        if (socket) socket.close();
    });

</script>

<div class="controls">
    <input bind:value={serverUrl} placeholder="ws://server/ws"/>
    {#if status !== 'Connected'}
        <button on:click={connect}>Connect</button>
    {:else}
        <button on:click={disconnect}>Disconnect</button>
    {/if}
    <span>Status: {status}</span>
</div>

<textarea readonly bind:value={output}></textarea>

<style>
    :global(body) {
        background-color: #222;
        color: #eee;
        font-family: sans-serif;
        display: flex;
        flex-direction: column;
        height: 95vh;
        gap: 1rem;
    }

    .controls {
        display: flex;
        gap: 1rem;
        align-items: center;
    }

    input, textarea {
        background-color: #333;
        color: #eee;
        border: 1px solid #555;
        padding: 0.5rem;
        border-radius: 4px;
    }

    input {
        flex-grow: 1;
    }

    textarea {
        flex-grow: 1;
        width: 100%;
        box-sizing: border-box;
        resize: none;
        font-family: monospace;
    }
</style>
