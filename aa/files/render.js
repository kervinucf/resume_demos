import {createMarkerElement} from './markers/factory.js';
import {updateMarkerElement} from './markers/updater.js';
import {injectMarkerStyles as injectStyling} from './markers/styles.js';
import {fetch_resources} from "./utils/data.js";


export const describeLocation = (data, isContinent = false) => {
    const id = data.primary_identifier || data.continent_id || data.id || `${isContinent ? 'cont' : 'loc'}_${Date.now()}_${Math.random().toString(16).slice(2)}`;
    console.log(data)
    const {latitude, longitude, center = {}, location = 'N/A', countries, country_code, country, timezone} = data;
    return {
        id,
        lat: latitude ?? center.latitude,
        lng: longitude ?? center.longitude,
        name: location,
        ...(isContinent ? {
            countries: countries || [],
            currentCountry: (countries || [])[0] || null
        } : {country: country_code || country, country_code, timezone}),
        isContinent,
        visible: true,
        data: {},
    };
};

export async function configure_globe_location_markers({
                                                           app = {preferences: {markerCount: 100}},
                                                           pdsClient
                                                       } = {}) {
    if (!pdsClient) {
        console.error("PDSClient instance not provided to configure_globe_location_markers.");
        return [];
    }

    const [
        pdsCitiesResponse, pdsContinentsResponse
    ] = await Promise.all([
        fetch_resources({
            pdsClient,
            operation: 'FIND',
            onCollection: 'location',
            returnEmbeddedRecords: true
        }),
        fetch_resources({
            pdsClient,
            operation: 'FIND',
            onCollection: 'continent',
            returnEmbeddedRecords: true
        })
    ]);

    const pdsCities = Array.isArray(pdsCitiesResponse) ? pdsCitiesResponse : (pdsCitiesResponse?._embedded?.records || []);
    const pdsContinents = Array.isArray(pdsContinentsResponse) ? pdsContinentsResponse : (pdsContinentsResponse?._embedded?.records || []);

    const cities = pdsCities.slice(0, app.preferences.markerCount);
    const cityMarkers = cities.map((cityRecord) => describeLocation(cityRecord.data || cityRecord, false));
    const availableCountries = new Set(cities.map((cityRecord) => (cityRecord.data || cityRecord).country));

    const contMarkers = pdsContinents
        .filter((continentRecord) => (continentRecord.data || continentRecord).countries?.some(c => availableCountries.has(c)))
        .map((continentRecord) => describeLocation(continentRecord.data || continentRecord, true));

    return [...cityMarkers, ...contMarkers];
}


export {createMarkerElement, updateMarkerElement, injectStyling};