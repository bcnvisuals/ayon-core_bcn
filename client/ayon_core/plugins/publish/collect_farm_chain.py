"""Farm job chain collection and linking plugins.

This module provides two pyblish plugins that work together to enable
dependency chaining for farm render jobs:

1. CollectFarmChainDef: Adds UI attributes (chain_order, chain_behavior)
   to farm instances during collection.
2. EstablishFarmChain: Processes collected instances and establishes
   dependencies between jobs based on their chain_order and chain_behavior.
"""

import pyblish.api
from ayon_core.lib import NumberDef, EnumDef
from ayon_core.pipeline import OptionalPyblishPluginMixin

class CollectFarmChainDef(
    pyblish.api.InstancePlugin,
    OptionalPyblishPluginMixin
):
    """Collects farm chain attributes for each instance.

    This plugin adds UI definition attributes to farm instances, allowing
    artists to specify the execution order and behavior of farm jobs.
    It collects chain_order (execution order) and chain_behavior
    (dependency vs parallel) attributes from instance data.

    Attributes:
        order: Plugin execution order (CollectorOrder + 0.490).
        label: Display name for the plugin.
        targets: Plugin targets (only "local").
        families: Instance families this plugin applies to.
    """
    
    order = pyblish.api.CollectorOrder + 0.490
    label = "Farm Job Chain"
    targets = ["local"]
    families = ["render", "render.farm", "prerender"]

    @classmethod
    def get_attribute_defs(cls):
        """Get attribute definitions for farm chain configuration.

        Returns:
            list: List of attribute definitions including:
                - chain_order: NumberDef for execution order (0, 1, 2, ...)
                - chain_behavior: EnumDef for dependency behavior
                  ("dependency" or "passthrough")
        """
        return [
            NumberDef(
                "chain_order",
                label="Chain Order",
                default=0,
                decimals=0,
                tooltip="Order of execution. 0 runs first, then 1, etc."
            ),
            EnumDef(
                "chain_behavior",
                label="Behavior",
                items={
                    "dependency": "Wait for previous (Dependency)",
                    "passthrough": "Do not wait (Parallel)"
                },
                default="dependency",
                tooltip="Should this job wait for the one with the lower order?"
            )
        ]

    def process(self, instance):
        """Process instance and extract chain attributes.

        Extracts chain_order and chain_behavior attributes from instance
        data and stores them for use by EstablishFarmChain plugin.
        Only processes instances that have "farm" flag set.

        Args:
            instance (pyblish.api.Instance): The instance to process.
        """
        if not instance.data.get("farm"):
            return
        attrs = self.get_attr_values_from_data(instance.data)
        instance.data["chain_order"] = attrs.get("chain_order")
        instance.data["chain_behavior"] = attrs.get("chain_behavior")


class EstablishFarmChain(pyblish.api.ContextPlugin):
    """Establishes dependencies between farm jobs based on chain configuration.

    This plugin processes all farm instances that have chain_order and
    chain_behavior attributes set by CollectFarmChainDef. It sorts instances
    by chain_order and creates dependency links between consecutive jobs
    when chain_behavior is set to "dependency".

    The plugin ensures that:
    - Context instances are sorted by chain_order for proper submission order
    - Jobs with "dependency" behavior wait for the previous job
    - Jobs with "passthrough" behavior run in parallel
    - Dependencies are stored in farm_instance_dependencies list

    Attributes:
        order: Plugin execution order (CollectorOrder + 0.491).
        label: Display name for the plugin.
        targets: Plugin targets (only "local").
    """
    
    order = pyblish.api.CollectorOrder + 0.491
    label = "Link Farm Jobs"
    targets = ["local"]
    
    def process(self, context):
        """Process context and establish farm job dependencies.

        Collects all farm instances with chain attributes, sorts them by
        chain_order, and creates dependency links. The context itself is
        sorted to ensure proper submission order to Deadline.

        Args:
            context (pyblish.api.Context): The publishing context containing
                all instances.
        """
        # 1. Collect only farm instances that have our attributes
        farm_instances = []
        for instance in context:
            if instance.data.get("farm") and "chain_order" in instance.data:
                farm_instances.append(instance)

        if not farm_instances:
            return

        # 2. Sort the MAIN context to ensure Deadline submits them in order (0 -> 1 -> 2)
        # This is critical so Job 0 exists before Job 1 tries to link to it.
        context[:] = sorted(
            context,
            key=lambda x: (
                x.data.get("chain_order", 0) if x in farm_instances else 0,
                x.data.get("name")
            )
        )
        
        # 3. Create a clean, sorted list for linking
        sorted_instances = [i for i in context if i in farm_instances]
        
        self.log.info(f"--- Processing {len(sorted_instances)} sorted jobs ---")

        # 4. Index-based loop (Guaranteed to work)
        for i in range(len(sorted_instances)):
            current = sorted_instances[i]
            
            # If index is 0, it is the start. It cannot depend on anything.
            if i == 0:
                self.log.info(f"  [START] '{current.data['name']}' (Order {current.data['chain_order']})")
                continue

            # For any other index, look at the previous item (i-1)
            behavior = current.data.get("chain_behavior")
            
            if behavior == "dependency":
                previous = sorted_instances[i-1]
                
                # Store the link
                deps = current.data.get("farm_instance_dependencies", [])
                deps.append(previous)
                current.data["farm_instance_dependencies"] = deps
                
                self.log.info(
                    f"  [LINK]  '{current.data['name']}' (Order {current.data['chain_order']}) "
                    f"-> Waiting for '{previous.data['name']}'"
                )
            else:
                self.log.info(f"  [PARALLEL] '{current.data['name']}' set to '{behavior}'")