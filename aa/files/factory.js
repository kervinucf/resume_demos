import { createCityMarker} from "./types/CityMarker.js";
import { createContinentMarker } from './types/ContinentMarker.js';

export function createMarkerElement(marker) {
    console.log(marker);

    if (marker.isContinent) {
        return createContinentMarker(marker);
    } else {
        return createCityMarker(marker);
    }
}
