import * as THREE from 'https://esm.sh/three';

export function set_screen_size(g, width, height) {
    return g.height(height).width(width);
}

export function set_mapping_paths(
    g,
    satellite_image_path = '//cdn.jsdelivr.net/npm/three-globe/example/img/earth-blue-marble.jpg',
    bump_map_path = '//cdn.jsdelivr.net/npm/three-globe/example/img/earth-topology.png',
    background_image_path = find_path('Background2.png')
) {
    return g
        .globeImageUrl(satellite_image_path)
        .bumpImageUrl(bump_map_path)
        .backgroundImageUrl(background_image_path);
}

export function set_distance(g, distance = 100) {
    g.controls().minDistance = distance;
    return g;
}

export function set_globe_material(m) {
    m.color = new THREE.Color(0xffffff);
    m.bumpScale = 2.5;
    m.emissive = new THREE.Color(0x001833);
    m.emissiveIntensity = 0.08;
    m.specular = new THREE.Color(0x444444);
    m.shininess = 45;
    m.roughness = 0.8;
    m.metalness = 0.1;
    return m;
}

export function set_globe_rotation(g, speed = 0) {
    g.controls().autoRotate = speed !== 0;
    g.controls().autoRotateSpeed = speed;
    return g;
}

export function set_camera_coordinates(g, x, y, z) {
    g.camera().position.set(x, y, z);
    return g;
}

export function set_camera_zoom(g, zoom) {
    g.camera().zoom = zoom;
    g.camera().updateProjectionMatrix();
    return g;
}
export function find_path(end) {
    return `src/${end.toLowerCase()}`;
}

// Functions related to managing focus areas

let earth_instance; // This should be passed in or managed externally

export function setEarthController(controller) {
    earth_instance = controller;
}

export function updateFocusAreasOnGlobe(focusAreas) {
    if (earth_instance && earth_instance.globe_instance) {
        earth_instance.updateRingsData(focusAreas);
    }
}

export function onCameraChange(cameraLat, cameraLng, cameraAltitude, cameraSpeed, earth_instance) {
    if (earth_instance) {
        earth_instance.updateCameraPosition(cameraLat, cameraLng, cameraAltitude, cameraSpeed);
    }
}

export function addNewFocusArea(e, focusAreas, setFocusAreas, showNewFocusFormSetter) {
    e.preventDefault();
    const formData = new FormData(e.target);
    const radius = parseFloat(formData.get('radius'));
    const unit = formData.get('unit');
    const radiusKm = unit === 'miles' ? radius * 1.60934 : radius;

    const newArea = {
        name: formData.get('name'),
        lat: parseFloat(formData.get('lat')),
        lng: parseFloat(formData.get('lng')),
        radius: radiusKm,
        propagationSpeed: (Math.random() - 0.5) * 3 + 1,
        color: formData.get('color')
    };
    const updatedAreas = [...focusAreas, newArea];
    setFocusAreas(updatedAreas);
    showNewFocusFormSetter(false);
    updateFocusAreasOnGlobe(updatedAreas);
}

export function deleteFocusArea(i, focusAreas, setFocusAreas) {
    const updated = focusAreas.filter((_, idx) => idx !== i);
    setFocusAreas(updated);
    updateFocusAreasOnGlobe(updated);
}

export function updateFocusArea(i, field, value, focusAreas, setFocusAreas) {
    const updated = [...focusAreas];
    updated[i] = {...updated[i], [field]: value};
    setFocusAreas(updated);
    updateFocusAreasOnGlobe(updated);
}
