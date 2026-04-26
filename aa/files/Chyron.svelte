<script>
    let {isBreaking = false, text = "Trump is the GOAT"} = $props()

    // --- NEW LOGIC FOR DYNAMIC FONT SIZE ---

    // 1. Define parameters for scaling
    const baseSize = 4.3; // The largest font size (in rem) for short text
    const minSize = 2.0;  // The smallest font size (in rem) for very long text
    const threshold = 25; // Character count at which text starts shrinking

    // 2. Calculate a scaling factor. This ensures that at `threshold` characters, the font size is exactly `baseSize`.
    const scaleFactor = baseSize * threshold;

    // 3. Create a reactive variable for the font size.
    // This code runs automatically whenever the `text` prop changes.
    let dynamicFontSize = $derived(
        // Use Math.max to ensure the font size never drops below `minSize`
        Math.max(minSize, scaleFactor / text.length)
    );

</script>

<div class="cur-event" class:bloomberg-style={!isBreaking} class:breaking-style={isBreaking}>
    <div class="top"></div>
    <div class="glow"></div>
    <div class="bttm"></div>
    <p style="--dynamic-font-size: {dynamicFontSize}rem">{text}</p>
</div>

<style>
    @import url('https://fonts.googleapis.com/css2?family=Oswald:wght@700&display=swap');

    /* Base styles shared by both themes */
    .cur-event {
        font-family: 'Oswald', sans-serif;
        font-weight: 700;
        color: #fff;
        overflow: hidden;
        position: relative;
        width: 100%;
        height: 100%;
        animation: reveal 0.8s cubic-bezier(0.25, 1, 0.5, 1) forwards;
    }

    .cur-event .top,
    .cur-event .glow,
    .cur-event .bttm {
        position: absolute;
        right: 0;
        opacity: 0; /* Faded in by animations */
    }

    .cur-event p {
        position: relative;
        z-index: 2;
        color: #FFFFFF;
        /* Use the CSS variable here instead of a fixed size */
        font-size: var(--dynamic-font-size);
        line-height: 1.1;
        text-transform: uppercase;
        padding: 0 2rem;
        margin: 0;
        height: 100%;
        display: flex;
        align-items: center;
        justify-content: flex-start;
        text-align: left;
        /* Add a transition for smooth font size changes */
        transition: font-size 0.3s ease-out;
    }

    /* ========================================
    BLOOMBERG STYLE (NORMAL NEWS)
    ======================================== */
    .bloomberg-style {
        background: linear-gradient(90deg, #000000 20%, #16191a 70%, #131b1f 100%);
    }

    .bloomberg-style .top {
        background: linear-gradient(to right, transparent, rgba(255, 255, 255, 0.4), transparent);
        top: 0;
        height: 1px;
        width: 95%;
        animation: fadeIn 0.6s 0.5s ease-out forwards;
    }


    .bloomberg-style .bttm {
        background: linear-gradient(90deg, transparent 40%, rgba(15, 77, 33, 0.85));
        box-shadow: 0 0 10px 2px rgba(20, 70, 18, 0.42);
        bottom: 0;
        height: 3px;
        width: 90%;
        animation: fadeIn 0.6s 0.5s ease-out forwards;
    }

    .bloomberg-style p {
        text-shadow: none;
    }

    /* ========================================
    BREAKING NEWS STYLE
    ======================================== */
    .breaking-style {
        background: linear-gradient(90deg, #300000 0%, #800000 40%, #B00000 100%);
    }

    .breaking-style .top {
        background: linear-gradient(to right, transparent, rgba(255, 220, 220, 0.6), transparent);
        top: 0;
        height: 2px;
        width: 95%;
        animation: fadeIn 0.6s 0.5s ease-out forwards;
    }

    .breaking-style .glow {
        background: linear-gradient(90deg, transparent 40%, #c00);
        box-shadow: 0 0 20px 5px #800 inset;
        bottom: 0;
        height: 10px;
        width: 100%;
        opacity: 0.5;
        animation: fadeIn 0.8s 0.6s ease-out forwards;
    }

    .breaking-style .bttm {
        background: #fca;
        box-shadow: 0 0 10px 2px #ff8c8c;
        bottom: 0;
        height: 3px;
        width: 90%;
        animation: fadeIn 0.6s 0.5s ease-out forwards;
    }

    .breaking-style p {
        animation: pulseText 2s infinite;
    }

    /* Keyframe Animations */
    @keyframes reveal {
        from {
            clip-path: inset(0 100% 0 0);
        }
        to {
            clip-path: inset(0 0 0 0);
        }
    }

    @keyframes fadeIn {
        from {
            opacity: 0;
        }
        to {
            opacity: 1;
        }
    }

    @keyframes pulseText {
        0%, 100% {
            transform: scale(1);
            text-shadow: 0 0 5px rgba(255, 255, 255, 0.2);
        }
        50% {
            transform: scale(1.0005);
            text-shadow: 0 0 15px rgba(255, 255, 255, 0.5);
        }
    }

    /* Responsive adjustments */
    @media screen and (max-width: 767px) {
        /* The dynamic sizing might handle this, but you can keep it as a fallback */
        .cur-event p {
            /* font-size is now dynamic, so this fixed value is less important */
            /* You might want to adjust the minSize in the script for mobile instead */
        }
    }
</style>