<script>
    import {onDestroy} from 'svelte';
    // REMOVED: import { flip } from 'svelte/animate'; -- No longer needed

    let {
        layout = 'SINGLE',
        assignments = {},
        resources = {},
        dynamicResourceProps = {},
        globeInstances = {}
    } = $props();

    const VIEWPORTS = ['main', 'pip', 'box1', 'box2', 'box3', 'box4'];

    // --- RESIZING LOGIC (UNCHANGED) ---
    let viewportElements = {};
    let viewportSizes = $state({});
    let resizeObserver = null;
    let layoutChangeAnimationId;

    $effect(() => {
        if (resizeObserver) resizeObserver.disconnect();
        const handleResize = (entries) => {
            for (const entry of entries) {
                const viewportId = Object.keys(viewportElements).find(
                    (key) => viewportElements[key] === entry.target
                );
                if (viewportId) {
                    const {width, height} = entry.contentRect;
                    if (width > 0 && height > 0) {
                        viewportSizes[viewportId] = {width, height};
                    }
                }
            }
        };
        resizeObserver = new ResizeObserver(handleResize);
        for (const viewportId in viewportElements) {
            const element = viewportElements[viewportId];
            if (element && assignments[viewportId]?.type === 'Earth') {
                resizeObserver.observe(element);
                const {width, height} = element.getBoundingClientRect();
                if (width > 0 && height > 0) {
                    viewportSizes[viewportId] = {width, height};
                }
            }
        }
        return () => {
            if (resizeObserver) resizeObserver.disconnect();
        };
    });

    $effect(() => {
        let frameCount = 0;
        const maxFrames = 30;
        const checkResize = () => {
            for (const viewportId in viewportElements) {
                const element = viewportElements[viewportId];
                if (element && assignments[viewportId]?.type === 'Earth') {
                    const {width, height} = element.getBoundingClientRect();
                    if (width > 0 && height > 0) {
                        viewportSizes[viewportId] = {width, height};
                    }
                }
            }
            frameCount++;
            if (frameCount < maxFrames) {
                layoutChangeAnimationId = requestAnimationFrame(checkResize);
            }
        };
        checkResize();
        return () => {
            if (layoutChangeAnimationId) cancelAnimationFrame(layoutChangeAnimationId);
        };
    });

    onDestroy(() => {
        if (resizeObserver) resizeObserver.disconnect();
        if (layoutChangeAnimationId) cancelAnimationFrame(layoutChangeAnimationId);
    });
</script>

<div class="display-container layout-{layout.toLowerCase().replace(/_/g, '-')}">
    {#each VIEWPORTS as viewport}
        {@const assignment = assignments[viewport]}
        <div class="viewport {viewport}" bind:this={viewportElements[viewport]}>
            {#if assignment && resources[assignment.type]}
                {@const component = resources[assignment.type]}
                {@const size = viewportSizes[viewport] || {width: 0, height: 0}}
                <svelte:component
                        this={component}
                        {...assignment.type === 'Earth'
                            ? {
                                globeInstance: globeInstances[assignment.resourceId],
                                width: size.width,
                                height: size.height
                            }
                            : dynamicResourceProps[assignment.resourceId] || {}}
                />
            {/if}
        </div>
    {/each}
</div>

<style>
    .display-container {
        width: 100%;
        height: 100%;
        display: grid;
        gap: 6px;
        background-color: #111;
        padding: 6px;
        box-sizing: border-box;
        /* RE-ADDED: This is the correct way to animate the grid layout */
        transition: grid-template-columns 0.4s ease-in-out,
        grid-template-rows 0.4s ease-in-out;
    }

    .viewport {
        display: grid;
        overflow: hidden;
        position: relative;
        border-radius: 4px;
        box-shadow: 0 0 15px rgba(0, 0, 0, 0.5);
        width: 100%;
        height: 100%;
        min-width: 0;
        min-height: 0;
    }

    .viewport:empty {
        display: none;
    }

    .layout-single {
        grid-template-columns: 1fr;
        grid-template-rows: 1fr;
        grid-template-areas: 'main main main main';
    }

    .layout-pip-bottom-right {
        grid-template-columns: 2fr 1fr; /* 2 columns (e.g., 66% and 33%) */
        grid-template-rows: 2fr 1fr; /* 2 rows (e.g., 66% and 33%) */
        /* REMOVED: The invalid grid-template-areas property */
    }

    /* Use a more specific selector to override the default grid-area */
    .layout-pip-bottom-right > .main {
        grid-column: 1 / -1; /* Span all columns */
        grid-row: 1 / -1; /* Span all rows */
    }

    .layout-pip-bottom-right > .pip {
        grid-column: 2 / 3; /* Place in the second column */
        grid-row: 2 / 3; /* Place in the second row */
    }

    .main {
        grid-area: main;
    }

    .pip {
        /* The general .pip rule now only contains styles, not grid placement */
        border: 2px solid rgba(255, 255, 255, 0.5);
        z-index: 10;
    }

    .layout-split-vertical {
        grid-template-columns: 1fr 1fr;
        grid-template-rows: 1fr;
        grid-template-areas: 'box1 box2';
    }

    .layout-triple-left-heavy {
        grid-template-columns: 2fr 1fr;
        grid-template-rows: 1fr 1fr;
        grid-template-areas: 'box1 box2' 'box1 box3';
    }

    .layout-quad {
        grid-template-columns: 1fr 1fr;
        grid-template-rows: 1fr 1fr;
        grid-template-areas: 'box1 box2' 'box3 box4';
    }

    .box1 {
        grid-area: box1;
    }

    .box2 {
        grid-area: box2;
    }

    .box3 {
        grid-area: box3;
    }

    .box4 {
        grid-area: box4;
    }
</style>