import json
import textwrap

class FrontendController:
    def __init__(self):
        self.commands = []

    def build(self) -> str:
        return "\n".join(self.commands)

    def clear(self):
        self.commands = []
        return self

    # --- NEW LAYOUT CONTROL METHODS ---

    def set_layout(self, layout_mode: str):
        """Sets the overall layout mode (e.g., 'QUAD', 'SINGLE')."""
        self.commands.append(f"state.layout = {json.dumps(layout_mode)};")
        return self

    def define_dynamic_html(self, resource_id: str, html_content: str, scroll_duration: int = 6000):
        """Defines or updates a dynamic HTML resource."""
        props = {"htmlContent": html_content, "scrollDuration": scroll_duration}
        js_code = f"""
state.dynamicResourceProps = {{
    ...state.dynamicResourceProps,
    {json.dumps(resource_id)}: {json.dumps(props)}
}};
"""
        self.commands.append(textwrap.dedent(js_code))
        return self
    
    def set_assignments(self, assignments: dict):
        """Sets the assignments for all viewports."""
        self.commands.append(f"state.assignments = {json.dumps(assignments)};")
        return self
        
    # --- CORE & UTILITY METHODS ---
    def set_sleep(self, duration: float):
        self.commands.append(f"await new Promise(r => setTimeout(r, {int(duration * 1000)}));")
        return self

    def set_state(self, key, value):
        self.commands.append(f"state.{key} = {json.dumps(value)};")
        return self

    def log_message(self, message: str):
        self.commands.append(f"console.log({json.dumps(message)});")
        return self

    def _get_data_array_name(self, layer_name: str) -> str:
        layer_map = {'html': 'htmlData', 'hex': 'hexBinData', 'hexBin': 'hexBinData'}
        if layer_name in layer_map:
            return f"state.{layer_map[layer_name]}"
        return f"state.{layer_name}Data"

    def pan_to_globe_location(self, target_id: str, lat, lng, duration, altitude=1.4):
        command = {
            "target": target_id,
            "action": "pan",
            "payload": {
                "pov": {"lat": lat, "lng": lng, "altitude": altitude},
                "duration": duration
            }
        }
        self.commands.append(f"state.commandQueue = [...state.commandQueue, {json.dumps(command)}];")
        return self

    def initialize_layer(self, target_id: str, layer_name: str, properties: dict):
        command = {
            "target": target_id,
            "action": "init_layer",
            "payload": {
                "layerName": layer_name,
                "properties": properties
            }
        }
        self.commands.append(f"state.commandQueue = [...state.commandQueue, {json.dumps(command)}];")
        return self

    def refresh_layer(self, target_id: str, layer_name: str):
        method_map = {'html': 'htmlElements', 'hex': 'hexBinPoints', 'hexBin': 'hexBinPoints'}
        method_base = method_map.get(layer_name, layer_name)
        command = {
            "target": target_id,
            "action": "refresh_layer",
            "payload": {"layerName": method_base}
        }
        self.commands.append(f"state.commandQueue = [...state.commandQueue, {json.dumps(command)}];")
        return self

    def add_elements(self, layer_name: str, elements: list):
        array_name = self._get_data_array_name(layer_name)
        elements_json = json.dumps(elements)
        self.commands.append(f"{array_name} = [...{array_name}, ...{elements_json}];")
        return self

    def remove_elements(self, layer_name: str, ids_to_remove: list, id_key: str = 'id'):
        array_name = self._get_data_array_name(layer_name)
        ids_json = json.dumps(ids_to_remove)
        id_key_json = json.dumps(id_key)
        self.commands.append(
            f"{{ const idsToRemove = new Set({ids_json}); {array_name} = {array_name}.filter(el => !idsToRemove.has(el[{id_key_json}])); }}"
        )
        return self

    def clear_layer(self, layer_name: str):
        array_name = self._get_data_array_name(layer_name)
        self.commands.append(f"{array_name} = [];")
        return self