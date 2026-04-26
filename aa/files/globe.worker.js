// --- Worker-safe shims to mimic browser environment ---
(() => {
    const g = globalThis;
    if (!('window' in g)) Object.defineProperty(g, 'window', { value: g, configurable: true });
    if (!('self' in g)) Object.defineProperty(g, 'self', { value: g, configurable: true });
    if (!('navigator' in g)) Object.defineProperty(g, 'navigator', { value: { userAgent: 'Worker' }, configurable: true });
    if (!('document' in g)) {
        Object.defineProperty(g, 'document', {
            value: {
                createElement: (tag) => {
                    if (tag === 'canvas' && typeof OffscreenCanvas !== 'undefined') {
                        return new OffscreenCanvas(1, 1);
                    }
                    return {
                        style: {},
                        addEventListener: () => { },
                        removeEventListener: () => { },
                        appendChild: () => { },
                        remove: () => { },
                        getContext: () => null,
                    };
                },
            },
            configurable: true,
        });
    }
})();

import * as THREE from 'three';
import Globe from 'globe.gl';

let globe;
let renderer;
let animationFrameId;

function startLoop() {
    if (animationFrameId) cancelAnimationFrame(animationFrameId);
    const animate = () => {
        if (renderer && globe) {
            renderer.render(globe.scene(), globe.camera());
        }
        animationFrameId = requestAnimationFrame(animate);
    };
    animate();
}

function stopLoop() {
    if (animationFrameId) {
        cancelAnimationFrame(animationFrameId);
        animationFrameId = null;
    }
}

onmessage = (e) => {
    const { type, payload } = e.data;

    switch (type) {
        case 'init': {
            const { canvas, width, height, dpr } = payload;
            renderer = new THREE.WebGLRenderer({
                canvas,
                antialias: true,
                alpha: true,
            });
            renderer.setPixelRatio(dpr);
            renderer.setSize(width, height, false);

            const stubEl = {
                clientWidth: width,
                clientHeight: height,
                addEventListener: () => { },
                removeEventListener: () => { },
            };

            globe = Globe()(stubEl);
            postMessage({ type: 'ready' });
            startLoop();
            break;
        }

        case 'call': {
            const { method, args } = payload;
            if (globe && typeof globe[method] === 'function') {
                globe[method](...args);
            }
            break;
        }

        case 'resize': {
            const { width, height } = payload;
            if (renderer && globe) {
                renderer.setSize(width, height, false);
                globe.width(width).height(height);
            }
            break;
        }

        case 'dispose': {
            stopLoop();
            globe?.dispose();
            renderer?.dispose();
            self.close();
            break;
        }
    }
};