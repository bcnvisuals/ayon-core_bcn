# -*- coding: utf-8 -*-
"""Creator plugin for creating pointcache alembics."""
from ayon_core.hosts.houdini.api import plugin
from ayon_core.pipeline import CreatedInstance
from ayon_core.lib import BoolDef


class CreateMantraIFD(plugin.HoudiniCreator):
    """Mantra .ifd Archive"""
    identifier = "io.openpype.creators.houdini.mantraifd"
    label = "Mantra IFD"
    product_type = "mantraifd"
    icon = "gears"
    ext = "ifd"
    staging_dir = "$HIP/ayon/{product[name]}/{product[name]}.$F4.{ext}"

    def create(self, product_name, instance_data, pre_create_data):
        import hou
        instance_data.pop("active", None)
        instance_data.update({"node_type": "ifd"})
        creator_attributes = instance_data.setdefault(
            "creator_attributes", dict())
        creator_attributes["farm"] = pre_create_data["farm"]
        instance = super(CreateMantraIFD, self).create(
            product_name,
            instance_data,
            pre_create_data)  # type: CreatedInstance

        instance_node = hou.node(instance.get("instance_node"))
        
        filepath = self.staging_dir.format(
            product={"name": "`chs(\"AYON_productName\")`"},
            ext=self.ext
        )

        parms = {
            # Render frame range
            "trange": 1,
            # Arnold ROP settings
            "soho_diskfile": filepath,
            "soho_outputmode": 1
        }

        instance_node.setParms(parms)

        # Lock any parameters in this list
        to_lock = ["soho_outputmode", "productType", "id"]
        self.lock_parameters(instance_node, to_lock)

    def get_instance_attr_defs(self):
        return [
            BoolDef("farm",
                    label="Submitting to Farm",
                    default=False)
        ]

    def get_pre_create_attr_defs(self):
        attrs = super().get_pre_create_attr_defs()
        # Use same attributes as for instance attributes
        return attrs + self.get_instance_attr_defs()
