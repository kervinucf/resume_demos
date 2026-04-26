<script>
    import { onMount, onDestroy } from 'svelte';
    import { slide } from 'svelte/transition';
    import Globe from 'globe.gl';

    import Rundown from './components/graphics/Rundown.svelte';
    import Chyron from './components/graphics/Chyron.svelte';
    import Logo from './components/graphics/Logo.svelte';
    import Ticker from './components/graphics/Ticker.svelte';
    import GreenScreen from './components/media/GreenScreen.svelte';
    import DisplayManager from './components/media/DisplayManager.svelte';
    import Earth from './components/media/Earth.svelte';
    import DynamicHTML from './components/media/DynamicHTML.svelte';
    import { orbitMode } from './js/resources/orbit/create_orbit.js';

    import { executeRemoteScript } from './js/utils.js';
    import { createMarkerElement } from './js/feed/markers/factory.js';
    import { injectMarkerStyles as injectStyling } from './js/feed/markers/styles.js';

    let serverUrl = 'ws://127.0.0.1:5100/ws';
    let socket = null;
    let status = $state('Disconnected');
    let lastInteraction = 0;
    let pauseBackend = $state(false);
    let resumeInterval;

    let lastCommandTime = $state(Date.now());
    let isStandby = $state(false);
    let standbyDelay = $state(15000);
    const EXTENDED_DELAY = 600000;
    let standbyInterval;

    let savedLayout = $state('SINGLE');
    let savedAssignments = $state({ main: { type: 'Earth', resourceId: 'earth1' } });

    const globeIds = ['earth1', 'earth2', 'earth3', 'earth4'];
    let globeInstances = $state({});
    let allEarthsReady = $state(false);
    const systemsReady = $derived(allEarthsReady);

    let rundownStories = $state([]);
    let selectedStory = $state(null);
    let breakingStory = $state(null);
    let currentMode = $state('RUNDOWN');

    let tickerText = $state('');
    let isBreakingChyron = $state(false);
    let chyronText = $state('');

    let hideSideBar = $state(false);
    let hideChyron = $state(false);
    let hideTicker = $state(false);

    let orbitingStatus = $state(Object.fromEntries(globeIds.map(id => [id, false])));
    let activeOrbits = {};

    let pointsData = $state([]);
    let arcsData = $state([]);
    let polygonsData = $state([]);
    let labelsData = $state([]);
    let pathsData = $state([]);
    let heatmapsData = $state([]);
    let hexBinData = $state([]);
    let ringsData = $state([]);
    let htmlData = $state([]);
    let commandQueue = $state([]);
    let dynamicResourceProps = $state({});

    let layout = $state('SINGLE');
    let assignments = $state({ main: { type: 'Earth', resourceId: 'earth1' } });

    const markerReferenceFrame = new Map();

    function hideUIForStandby() {
        hideSideBar = true;
        hideTicker = true;
    }

    function showUI() {
        hideSideBar = false;
        hideTicker = false;
    }

    function enterStandby() {
        if (isStandby) return;

        savedLayout = layout;
        savedAssignments = JSON.parse(JSON.stringify(assignments));

        isStandby = true;
        hideUIForStandby();

        layout = 'TRIPLE_LEFT_HEAVY';
        assignments = {
            box1: { type: 'Earth', resourceId: 'earth1' },
            box2: { type: 'Earth', resourceId: 'earth2' },
            box3: { type: 'Earth', resourceId: 'earth3' }
        };

        globeIds.forEach(id => (orbitingStatus[id] = true));
    }

    function forceStopAllOrbits() {
        globeIds.forEach(id => {
            orbitingStatus[id] = false;
            if (activeOrbits[id]) activeOrbits[id].pause();
        });
    }

    function wakeUpAndCleanState() {
        forceStopAllOrbits();

        showUI();

        standbyDelay = EXTENDED_DELAY;

        if (isStandby) {
            layout = savedLayout;
            assignments = savedAssignments;
            isStandby = false;
        }
    }

    function connect() {
        if (socket) return;
        status = `Connecting to ${serverUrl}...`;

        try {
            socket = new WebSocket(serverUrl);
            socket.onopen = () => (status = 'Connected');

            socket.onmessage = (e) => {
                lastCommandTime = Date.now();
                wakeUpAndCleanState();

                if (!pauseBackend) executeRemoteScript(e.data);
            };

            socket.onclose = () => {
                status = 'Disconnected';
                socket = null;
            };

            socket.onerror = (e) => {
                status = 'Error';
                console.error(e);
            };
        } catch (e) {
            status = 'Error';
            console.error(e);
        }
    }

    function disconnect() {
        if (socket) socket.close();
    }

    function userInteracted() {
        lastInteraction = Date.now();
        pauseBackend = true;
        lastCommandTime = Date.now();
        wakeUpAndCleanState();
    }

    window.app = {
        state: {
            get rundownStories() { return rundownStories }, set rundownStories(v) { rundownStories = v },
            get selectedStory() { return selectedStory }, set selectedStory(v) { selectedStory = v },
            get breakingStory() { return breakingStory }, set breakingStory(v) { breakingStory = v },
            get currentMode() { return currentMode }, set currentMode(v) { currentMode = v },
            get tickerText() { return tickerText }, set tickerText(v) { tickerText = v },
            get isBreakingChyron() { return isBreakingChyron }, set isBreakingChyron(v) { isBreakingChyron = v },
            get chyronText() { return chyronText }, set chyronText(v) { chyronText = v },
            get hideChyron() { return hideChyron }, set hideChyron(v) { hideChyron = v },
            get hideTicker() { return hideTicker }, set hideTicker(v) { hideTicker = v },
            get hideSideBar() { return hideSideBar }, set hideSideBar(v) { hideSideBar = v },
            get orbitingStatus() { return orbitingStatus }, set orbitingStatus(v) { orbitingStatus = v },
            get pointsData() { return pointsData }, set pointsData(v) { pointsData = v },
            get arcsData() { return arcsData }, set arcsData(v) { arcsData = v },
            get polygonsData() { return polygonsData }, set polygonsData(v) { polygonsData = v },
            get labelsData() { return labelsData }, set labelsData(v) { labelsData = v },
            get pathsData() { return pathsData }, set pathsData(v) { pathsData = v },
            get heatmapsData() { return heatmapsData }, set heatmapsData(v) { heatmapsData = v },
            get hexBinData() { return hexBinData }, set hexBinData(v) { hexBinData = v },
            get ringsData() { return ringsData }, set ringsData(v) { ringsData = v },
            get htmlData() { return htmlData }, set htmlData(v) { htmlData = v },
            get commandQueue() { return commandQueue }, set commandQueue(v) { commandQueue = v },
            get dynamicResourceProps() { return dynamicResourceProps }, set dynamicResourceProps(v) { dynamicResourceProps = v },
            get layout() { return layout }, set layout(v) { layout = v },
            get assignments() { return assignments }, set assignments(v) { assignments = v }
        },
        globes: globeInstances,
        helpers: { createMarkerElement, markerReferenceFrame }
    };

    let focusTimeoutId = null;

    onMount(() => {
        const newInstances = {};
        globeIds.forEach(id => (newInstances[id] = Globe()));
        globeInstances = newInstances;
        allEarthsReady = true;

        globeIds.forEach(id => {
            const globe = globeInstances[id];
            activeOrbits[id] = orbitMode(globe);
        });

        connect();

        resumeInterval = setInterval(() => {
            if (pauseBackend && Date.now() - lastInteraction > 5000) pauseBackend = false;
        }, 1000);

        standbyInterval = setInterval(() => {
            if (!isStandby && Date.now() - lastCommandTime > standbyDelay) enterStandby();
        }, 1000);
    });

    onDestroy(() => {
        if (focusTimeoutId) clearTimeout(focusTimeoutId);
        if (resumeInterval) clearInterval(resumeInterval);
        if (standbyInterval) clearInterval(standbyInterval);
        if (socket) socket.close();

        Object.values(activeOrbits).forEach(o => o.stop());
    });

    for (const id of globeIds) {
        $effect(() => {
            const state = orbitingStatus[id];
            const ctrl = activeOrbits[id];
            if (!ctrl) return;

            state ? ctrl.resume() : ctrl.pause();
        });
    }

    $effect(() => systemsReady && injectStyling());

    $effect(() => {
        if (focusTimeoutId) clearTimeout(focusTimeoutId);
        if (selectedStory) userInteracted();
        else if (currentMode !== 'BREAKING') currentMode = 'RUNDOWN';
    });

    $effect(() => {
        if (!commandQueue.length) return;

        const command = commandQueue[0];
        const globe = globeInstances[command.target];

        if (globe) {
            switch (command.action) {
                case 'pan':
                    globe.pointOfView(command.payload.pov, command.payload.duration);
                    break;

                case 'init_layer':
                    let chain = globe;
                    const { layerName, properties } = command.payload;

                    for (const [key, value] of Object.entries(properties)) {
                        let processed = value;
                        if (typeof value === 'string' && (value.includes('=>') || value.startsWith('d =>'))) {
                            try { processed = new Function(`return ${value}`)(); }
                            catch { continue; }
                        }
                        if (typeof chain[key] === 'function') chain = chain[key](processed);
                    }

                    const map = { html: 'htmlElements', hex: 'hexBinPoints', rings: 'rings' };
                    const method = `${map[layerName] || layerName}Data`;
                    const dataName = `${layerName}Data`;
                    if (typeof chain[method] === 'function') chain[method](window.app.state[dataName]);
                    break;

                case 'refresh_layer':
                    const m = `${command.payload.layerName}Data`;
                    const fn = globe[m];
                    const d = window.app.state[m];
                    if (fn && d) fn(d);
                    break;
            }
        }

        commandQueue = commandQueue.slice(1);
    });

    function updateAllGlobes(method, data) {
        Object.values(globeInstances).forEach(globe => {
            if (globe && typeof globe[method] === 'function') globe[method](data);
        });
    }

    $effect(() => updateAllGlobes('htmlElementsData', htmlData));
    $effect(() => updateAllGlobes('hexBinPointsData', hexBinData));
    $effect(() => updateAllGlobes('ringsData', ringsData));
    $effect(() => updateAllGlobes('arcsData', arcsData));

    const resources = { Earth, GreenScreen, DynamicHTML };
</script>

<div class="controls" style="position: absolute; z-index:9999; visibility:collapse">
    <input bind:value={serverUrl} placeholder="ws://server/ws" />
    {#if status !== 'Connected'}
        <button on:click={connect}>Connect</button>
    {:else}
        <button on:click={disconnect}>Disconnect</button>
    {/if}
    <span>Status: {status}</span>

    {#if isStandby}
        <span style="color:cyan;font-weight:bold;">STANDBY</span>
    {:else if pauseBackend}
        <span style="color:orange;font-weight:bold;">⏸ PAUSED</span>
    {:else}
        <span style="color:lime;font-weight:bold;">▶ AUTO</span>
    {/if}

    {#each globeIds as id}
        <button on:click={() => orbitingStatus[id] = !orbitingStatus[id]}>
            {orbitingStatus[id] ? `Stop ${id}` : `Start ${id}`}
        </button>
    {/each}
</div>

<div class="scene-container" style:visibility={systemsReady ? 'visible':'hidden'}>
    <div class="graphics">
        <div class="main-column">
            <div class="view-container">
                <DisplayManager {layout} {assignments} {resources} {dynamicResourceProps} {globeInstances} />
            </div>

            <div class="lower-third">
                {#if !hideChyron}
                    <div class="chyron-container" transition:slide>
                        <div class="logo-container"><Logo/></div>
                        <div class="chyron-content">
                            <Chyron isBreaking={isBreakingChyron} text={chyronText}/>
                        </div>
                    </div>
                {/if}

                {#if !hideTicker}
                    <div class="ticker-container" transition:slide>
                        <Ticker {tickerText}/>
                    </div>
                {/if}
            </div>
        </div>

        {#if !hideSideBar}
            <div class="sidebar-graphic" transition:slide>
                <Rundown stories={rundownStories} bind:selectedStory {breakingStory} mode={currentMode}/>
            </div>
        {/if}
    </div>
</div>

<style>
    :global(body){
        margin:0;
        padding:0;
        box-sizing:border-box;
        background:#000;
        color:#f1f1f1;
        font-family:Arial,sans-serif;
        overflow:hidden;
        height:100vh;
    }

    .scene-container{width:100%;height:100vh;}
    .graphics{display:flex;width:100%;height:100%;}
    .main-column{display:flex;flex-direction:column;flex:1;min-width:0;height:100vh;overflow:hidden;}
    .view-container{flex:1;min-height:0;display:flex;position:relative;}

    .lower-third{height:auto;display:flex;flex-direction:column;}
    .chyron-container{display:flex;width:100%;flex-shrink:0;height:21.25vh;}
    .logo-container{width:22%;height:100%;display:flex;justify-content:center;align-items:center;}
    .chyron-content{width:83%;display:flex;justify-content:center;align-items:center;}
    .ticker-container{height:3.75vh;width:100%;}
    .sidebar-graphic{width:25%;flex-shrink:0;}
</style>
