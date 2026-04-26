<script>
	// --- Import the setup functions you need ---
	import {
		set_mapping_paths,
		set_distance,
		set_globe_material,
		set_camera_zoom,
		set_camera_coordinates,
		set_globe_rotation
	} from '../../js/helpers.js'; // Adjust path if needed
	import { create_earth_scene } from '../../js/resources/scene/create_scene.js'; // Adjust path if needed
	import { create_earth_lighting } from '../../js/resources/lighting/create_lighting.js'; // Adjust path if needed

	// Props from parent
	let { globeInstance, width = 0, height = 0 } = $props();

	// Local state for the container div
	let container = $state(null);
	let hasInitialized = false; // Flag to prevent re-running setup

	// This effect now handles ONE-TIME mounting and setup
	$effect(() => {
		// Only run if we have the container, the instance, AND we haven't run before
		if (container && globeInstance && !hasInitialized) {
			// 1. Mount the globe to the DOM element
			globeInstance(container);

			// 2. Run all one-time setup functions
			const material = globeInstance.globeMaterial();
			set_mapping_paths(globeInstance);
			set_distance(globeInstance);
			set_globe_material(material);
			set_camera_coordinates(globeInstance, -240, -50, 210);
			set_camera_zoom(globeInstance, 0.5);
			set_globe_rotation(globeInstance, 0);
			create_earth_scene(globeInstance);
			create_earth_lighting(globeInstance); // This will now run only once
			// 3. Set the flag to true so this block never runs again for this instance
			hasInitialized = true;
		}
	});

	// This separate effect ONLY handles resizing
	$effect(() => {
		if (globeInstance && hasInitialized && width > 0 && height > 0) {
			globeInstance.width(width).height(height);
			const camera = globeInstance.camera();
			if (camera) {
				camera.aspect = width / height;
				camera.updateProjectionMatrix();
			}
		}
	});
</script>

<div bind:this={container} class="earth-container"></div>

<style>
	.earth-container {
		width: 100%;
		height: 100%;
		overflow: hidden;
	}
</style>
