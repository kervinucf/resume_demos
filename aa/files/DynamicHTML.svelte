<script>
    // Svelte 5 runes mode
    let { htmlContent = '', scrollDuration = 6000 } = $props();
    let containerElement;

    // 👇 Create a derived reactive signal so we can safely depend on htmlContent
    const trackedContent = $derived(htmlContent);

    function smoothScrollTo(element, to, scrollDuration) {
        if (!element) return;
        const start = element.scrollTop;
        const change = to - start;
        let startTime = null;

        function animateScroll(currentTime) {
            if (startTime === null) startTime = currentTime;
            const elapsed = currentTime - startTime;
            const progress = Math.min(elapsed / scrollDuration, 1);
            const ease =
                progress < 0.5
                    ? 2 * progress * progress
                    : -1 + (4 - 2 * progress) * progress;
            element.scrollTop = start + change * ease;
            if (elapsed < scrollDuration) requestAnimationFrame(animateScroll);
        }

        requestAnimationFrame(animateScroll);
    }

    function findScrollableElement(root) {
        if (!root) return null;
        if (root.scrollHeight > root.clientHeight + 10) return root;
        const elements = root.querySelectorAll('*');
        for (const el of elements) {
            const style = getComputedStyle(el);
            if (
                (style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                el.scrollHeight > el.clientHeight + 10
            ) {
                return el;
            }
        }
        return null;
    }

    // ✅ Reactive effect — runs whenever trackedContent changes
    $effect(() => {
        // Access tracked value to make this effect reactive
        void trackedContent;

        if (!containerElement) return;

        const delay = 200 + Math.random() * 2000; // 2–4 seconds
        let timeoutId = setTimeout(() => {
            requestAnimationFrame(() => {
                const scrollTarget =
                    findScrollableElement(containerElement) || containerElement;
                smoothScrollTo(scrollTarget, scrollTarget.scrollHeight, scrollDuration);
            });
        }, delay);

        // Cleanup to prevent overlapping timers
        return () => clearTimeout(timeoutId);
    });
</script>

<div class="dynamic-html-container" bind:this={containerElement}>
    {@html htmlContent}
</div>

<style>
    .dynamic-html-container {
        width: 100%;
        height: 100%;
        box-sizing: border-box;
        overflow: hidden;
        background-color: rgba(20, 20, 25, 0.8);
        border-radius: 4px;
        color: #e0e0e0;
        font-family: 'Courier New', Courier, monospace;
    }
</style>
