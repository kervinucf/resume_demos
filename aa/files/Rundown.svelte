<script>
    import {slide} from 'svelte/transition';

    // The component now only needs the stories and the selected one.
    let {stories, selectedStory, breakingStory} = $props();

    let showArchive = $state(false);
    let itemElements = {};

    const archiveSegments = [
        {label: 'Regional Finances', endpoint: '/api/regional-finances'},
        {label: 'Global Finances', endpoint: '/api/global-finances'},
        {label: 'Regional Weather', endpoint: '/api/regional-weather'},
        {label: 'Global Weather', endpoint: '/api/global-weather'},
        {label: 'Air Traffic', endpoint: '/api/air-traffic'},
        {label: 'Sports', endpoint: '/api/sports'},
        {label: 'Regional News', endpoint: '/api/regional-news'},
        {label: 'Global News', endpoint: '/api/global-news'}
    ];
    // This effect simply finds the selected story's element and scrolls it to the top.
    $effect(() => {
        if (!selectedStory) return;
        const element = itemElements[selectedStory.id];
        const idx = stories.findIndex(s => s.id === selectedStory.id);
        if (idx !== -1 && idx >= 10) {
            element?.scrollIntoView({behavior: 'smooth', block: 'start'});
        }
    });


    async function loadArchiveSegment(segment) {
        try {
            const response = await fetch(`http://0.0.0.0:5100${segment.endpoint}`);
            const data = await response.json();
            console.log('Loaded segment:', data);

            if (data.headline) window.app.state.chyronText = data.headline;
            if (data.ticker) window.app.state.tickerText = data.ticker;
        } catch (err) {
            console.error('Failed to load segment:', err);
        }
    }

    // Time
    let currentTime = $state('');
    setInterval(() => {
        const now = new Date();
        currentTime = now.toLocaleTimeString('en-GB', {timeZone: 'UTC', hour: '2-digit', minute: '2-digit'});
    }, 1000);
</script>

<div class="broadcast-panel">
    <header class="panel-header">
        <div class="header-left">
            <span class="live-dot"></span>
            <span>LIVE</span>
        </div>
        <div class="header-right">{currentTime} UTC</div>
    </header>

    <main class="content-window">
        {#if stories.length === 0 && !showArchive}
            <div class="status-text">CONNECTING TO FEED...</div>
        {/if}

        {#if !showArchive}
            <div class="rundown-container">
                <div class="rundown-header-row">
                    <div class="rundown-header">RUNDOWN</div>
                    <button class="archive-btn" onclick={() => showArchive = true}>ARCHIVE</button>
                </div>
                <div class="rundown-list">
                    {#each stories as story, i (story.id)}
                        <div
                                class="rundown-item"
                                class:selected={story.id === selectedStory?.id}
                                style:--delay={i * 0.05 + 's'}
                                bind:this={itemElements[story.id]}
                        >
                            <span class="item-index">{String(i + 1).padStart(2, '0')}</span>
                            <div class="item-details">
                                <span class="item-title">{story.title}</span>
                                <span class="item-sub-title">{story.subTitle}</span>
                            </div>
                        </div>
                    {/each}
                </div>
            </div>
        {/if}

        {#if showArchive}
            <div class="archive-container" transition:slide|local={{ duration: 600, axis: 'y' }}>
                <div class="rundown-header-row">
                    <div class="rundown-header">ARCHIVE</div>
                    <button class="archive-btn" onclick={() => showArchive = false}>BACK</button>
                </div>
                <div class="archive-list">
                    {#each archiveSegments as segment, i}
                        <button
                                class="archive-item"
                                onclick={() => loadArchiveSegment(segment)}
                                style:--delay={i * 0.05 + 's'}
                        >
                            {segment.label}
                        </button>
                    {/each}
                </div>
            </div>
        {/if}

        {#if breakingStory}
            <div class="breaking-container" transition:slide|local={{ duration: 300 }} key={breakingStory.id}>
                <div class="breaking-box">
                    <h2 class="breaking-title">BREAKING</h2>
                    <p class="breaking-location">{breakingStory.title}</p>
                </div>
            </div>
        {/if}
    </main>
</div>

<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;700;900&display=swap');

    :root {
        --background-color: #000;
        --text-color: #FFF;
        --border-color: #333;
        --accent-color: #FFF;
    }

    .broadcast-panel {
        height: 100vh;
        background: linear-gradient(90deg, rgba(0, 0, 0, 0.5) 20%, rgba(22, 25, 26, 0.7) 70%, #131b1f 100%);
        color: var(--text-color);
        display: flex;
        flex-direction: column;
        border-left: 1px solid var(--border-color);
        font-family: 'Inter', sans-serif;
        overflow: hidden;
    }

    .panel-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 16px 20px;
        flex-shrink: 0;
        border-bottom: 1px solid var(--border-color);
        user-select: none;
    }

    .header-left {
        display: flex;
        align-items: center;
        gap: 10px;
        font-weight: 700;
        font-size: 13px;
        letter-spacing: 0.2em;
    }

    .live-dot {
        width: 10px;
        height: 10px;
        background-color: var(--text-color);
        animation: slow-fade 2.5s infinite ease-in-out;
    }

    .header-right {
        font-weight: 500;
        font-size: 13px;
        letter-spacing: 0.2em;
    }

    .content-window {
        flex-grow: 1;
        display: flex;
        flex-direction: column;
        position: relative;
        overflow-y: auto; /* CHANGED: This is now the scrollable container */
    }

    .status-text {
        position: absolute;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%);
        font-size: 14px;
        font-weight: 500;
        letter-spacing: 0.2em;
        text-transform: uppercase;
    }

    .rundown-container, .archive-container {
        padding: 20px;
        display: flex;
        flex-direction: column;
        /* CHANGED: Removed height: 100% */
    }

    .rundown-header-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding-bottom: 10px;
        border-bottom: 1px solid var(--border-color);
        margin-bottom: 10px;
    }

    .rundown-header, .archive-header {
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 0.3em;
        text-transform: uppercase;
    }

    .archive-btn {
        background: transparent;
        border: 1px solid #444;
        color: #888;
        padding: 4px 10px;
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 0.2em;
        cursor: pointer;
        font-family: 'Inter', sans-serif;
        transition: all 0.2s;
    }

    .archive-btn:hover {
        border-color: #fff;
        color: #fff;
    }

    .rundown-list, .archive-list {
        /* CHANGED: Removed overflow-y and flex-grow */
    }

    .rundown-item {
        scroll-margin-top: 14px; /* This creates a 20px gap at the top */
        display: flex;
        align-items: center;
        padding: 10px 0;
        border-bottom: 1px solid #1a1a1a;
        transition: background-color 0.2s;
        animation: fadeIn 0.5s ease forwards;
        animation-delay: var(--delay, 0s);
        opacity: 0;
    }

    .rundown-item:hover {
        background-color: #111;
    }

    .rundown-item.selected {
        animation: fadeIn 0.5s ease forwards, subtle-pulse 2s infinite alternate ease-in-out;
        border-left: 2px solid white;
        padding-left: 10px;
    }

    .archive-item {
        width: 100%;
        text-align: left;
        padding: 12px 10px;
        background: rgba(255, 255, 255, 0.02);
        border: 1px solid #222;
        border-bottom: 1px solid #1a1a1a;
        color: var(--text-color);
        font-size: 14px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        cursor: pointer;
        transition: all 0.2s;
        font-family: 'Inter', sans-serif;
        animation: fadeIn 0.5s ease forwards;
        animation-delay: var(--delay, 0s);
        opacity: 0;
    }

    .archive-item:hover {
        background-color: #111;
        border-color: #444;
    }

    @keyframes subtle-pulse {
        from {
            background-color: rgba(255, 255, 255, 0.04);
        }
        to {
            background-color: rgba(255, 255, 255, 0.08);
        }
    }

    .item-index {
        font-weight: 700;
        font-size: 12px;
        margin-right: 15px;
        color: #888;
    }

    .item-details {
        flex: 1;
    }

    .item-title {
        display: block;
        font-weight: 700;
        font-size: 16px;
        text-transform: uppercase;
        line-height: 1.2;
    }

    .item-sub-title {
        display: block;
        font-weight: 500;
        font-size: 12px;
        text-transform: uppercase;
        color: #888;
    }

    .breaking-container {
        position: absolute;
        inset: 0;
        display: flex;
        justify-content: center;
        align-items: center;
        background: transparent;
    }

    .breaking-box {
        background-color: var(--accent-color);
        color: var(--background-color);
        padding: 25px 30px;
        text-align: center;
        animation: breakIn 0.4s cubic-bezier(0.68, -0.55, 0.27, 1.55) forwards, flashBorder 1s infinite;
    }

    .breaking-title {
        font-size: 36px;
        font-weight: 900;
        text-transform: uppercase;
        letter-spacing: 0.1em;
    }

    .breaking-location {
        font-size: 18px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-top: 5px;
    }

    @keyframes slow-fade {
        0%, 100% {
            opacity: 1;
        }
        50% {
            opacity: 0.2;
        }
    }

    @keyframes fadeIn {
        to {
            opacity: 1;
        }
    }

    @keyframes slideUp {
        from {
            opacity: 0;
            transform: translateY(30px);
        }
        to {
            opacity: 1;
            transform: translateY(0);
        }
    }

    @keyframes breakIn {
        from {
            transform: scale(0.5);
            opacity: 0;
        }
        to {
            transform: scale(1);
            opacity: 1;
        }
    }

    @keyframes flashBorder {
        0%, 100% {
            box-shadow: 0 0 0 0px rgba(255, 255, 255, 0.7);
        }
        50% {
            box-shadow: 0 0 0 8px rgba(255, 255, 255, 0);
        }
    }
</style>