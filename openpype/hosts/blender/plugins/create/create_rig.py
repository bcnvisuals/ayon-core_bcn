"""Create a rig asset."""

import bpy

from openpype.pipeline import get_current_task_name, CreatedInstance
from openpype.hosts.blender.api import plugin, lib, ops
from openpype.hosts.blender.api.pipeline import (
    AVALON_INSTANCES,
    AVALON_PROPERTY,
)


class CreateRig(plugin.BaseCreator):
    """Artist-friendly rig with controls to direct motion."""

    identifier = "io.openpype.creators.blender.rig"
    name = "rigMain"
    label = "Rig"
    family = "rig"
    icon = "wheelchair"

    def create(
        self, subset_name: str, instance_data: dict, pre_create_data: dict
    ):
        """Run the creator on Blender main thread."""
        self._add_instance_to_context(
            CreatedInstance(self.family, subset_name, instance_data, self)
        )

        mti = ops.MainThreadItem(
            self._process, subset_name, instance_data, pre_create_data
        )
        ops.execute_in_main_thread(mti)

    def _process(
        self, subset_name: str, instance_data: dict, pre_create_data: dict
    ):
        # Get Instance Container or create it if it does not exist
        instances = bpy.data.collections.get(AVALON_INSTANCES)
        if not instances:
            instances = bpy.data.collections.new(name=AVALON_INSTANCES)
            bpy.context.scene.collection.children.link(instances)

        # Create instance object
        name = plugin.asset_name(instance_data["asset"], subset_name)
        asset_group = bpy.data.objects.new(name=name, object_data=None)
        asset_group.empty_display_type = 'SINGLE_ARROW'
        instances.objects.link(asset_group)

        asset_group[AVALON_PROPERTY] = instance_node = {
            "name": asset_group.name,
        }

        self.set_instance_data(subset_name, instance_data, instance_node)
        lib.imprint(asset_group, instance_data)

        # Add selected objects to instance
        if pre_create_data.get("useSelection"):
            bpy.context.view_layer.objects.active = asset_group
            selected = lib.get_selection()
            for obj in selected:
                obj.select_set(True)
            selected.append(asset_group)
            bpy.ops.object.parent_set(keep_transform=True)

        return asset_group
