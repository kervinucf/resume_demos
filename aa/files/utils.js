export function executeRemoteScript(scriptContent) {
    try {
        const context = {
            state: window.app.state,
            // ❌ REMOVE THE OLD 'globe'
            // globe: window.app.globe, 

            // ✅ ADD THE PLURAL 'globes' OBJECT
            globes: window.app.globes,
            helpers: window.app.helpers
        };

        const AsyncFunction = Object.getPrototypeOf(async function () { }).constructor;

        const fn = new AsyncFunction(
            'state',
            'globes', // 👈 Update argument name
            'helpers',
            `
            try {
                ${scriptContent}
            } catch (e) {
                console.error("Remote script execution failed:", e);
            }
        `
        );

        // Pass the correct context properties
        fn(context.state, context.globes, context.helpers);

    } catch (e) {
        console.error("[executeRemoteScript] Error:", e);
    }
}