import json

from ayon_core.pipeline import load


class ApplyCameraData(load.LoaderPlugin):
    """Apply camera data to the selected Camera Node"""
    representations = {"*"}
    product_types = {"cameradata"}

    label = "Apply Camera Data"
    order = 25
    icon = "camera"
    color = "#4CAF50"

    def load(self, context, name=None, namespace=None, data=None):
        # Load and parse the camera data
        try:
            camera_data = json.loads(data) if isinstance(data, str) else data
            winsizex = camera_data.get("winsizex")
            winsizey = camera_data.get("winsizey")

            if winsizex is None or winsizey is None:
                self.log.error("Missing 'winsizex' or 'winsizey' in camera data.")
                return

            # Get the selected Camera Node
            selected_nodes = self.get_selected_camera_node()
            if not selected_nodes:
                self.log.error("No Camera Node is selected.")
                return

            camera_node = selected_nodes[0]
            self.log.info(f"Applying camera data to node: {camera_node.name()}")

            # Update the win_scale knobs
            self.set_win_scale(camera_node, winsizex, winsizey)

        except json.JSONDecodeError as e:
            self.log.error(f"Invalid JSON data: {e}")

    def get_selected_camera_node(self):
        from nuke import selectedNodes

        nodes = selectedNodes()
        camera_nodes = [node for node in nodes if node.Class() == "Camera"]
        return camera_nodes

    def set_win_scale(self, camera_node, winsizex, winsizey):
        try:
            win_scale = camera_node['win_scale']

            # Assuming win_scale is a UV knob where X corresponds to U and Y to V
            win_scale.setValue(winsizex, 0)  # U value
            win_scale.setValue(winsizey, 1)  # V value

            self.log.info(
                f"Set win_scale to (U: {winsizex}, V: {winsizey}) on node {camera_node.name()}"
            )
        except Exception as e:
            self.log.error(f"Failed to set win_scale: {e}") 