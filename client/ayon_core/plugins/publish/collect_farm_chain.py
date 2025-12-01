import pyblish.api
from ayon_core.lib import TextDef, NumberDef, EnumDef
from ayon_core.pipeline import (
    OptionalPyblishPluginMixin,
    PublishValidationError
)

class CollectFarmChain(
    pyblish.api.ContextPlugin,
    OptionalPyblishPluginMixin
):
    """Manager for creating dependencies between farm jobs.
    
    This tool allows users to chain multiple farm jobs together (e.g. cache -> render).
    Jobs with the same 'Chain Group' will be executed in the order defined by 'Chain Order'.
    """
    
    order = pyblish.api.CollectorOrder + 0.490
    label = "Farm Job Chain"
    targets = ["local"]
    
    # Define the attributes that will appear in the Publisher UI
    @classmethod
    def get_attribute_defs(cls):
        return [
            TextDef(
                "chain_group",
                label="Chain Group",
                tooltip="Jobs with the same Group name will be linked.",
                placeholder="e.g. 'character_A', 'main_seq'"
            ),
            NumberDef(
                "chain_order",
                label="Chain Order",
                default=0,
                decimals=0,
                tooltip="Lower numbers run first. 0 runs before 1."
            ),
            EnumDef(
                "chain_behavior",
                label="Behavior",
                items={
                    "dependency": "Wait for previous (Dependency)",
                    "batch": "Just group (Batch only)"
                },
                default="dependency",
                tooltip="Should the job wait for the previous one to finish?"
            )
        ]

    def process(self, context):
        """Process instances to establish dependencies."""
        
        # 1. Group instances by their chain group
        chains = {}
        
        for instance in context:
            if not instance.data.get("farm"):
                continue
            
            # Get attribute values (handled by AYON's attribute system)
            # Note: Attributes are usually stored in 'publish_attributes' by the controller
            attrs = self.get_attr_values_from_data(instance.data)
            
            group = attrs.get("chain_group")
            if not group:
                continue
                
            if group not in chains:
                chains[group] = []
            
            chains[group].append((instance, attrs))

        # 2. Sort and Link
        for group_name, items in chains.items():
            # Sort by 'chain_order' and then by instance name as a tie-breaker
            items.sort(key=lambda x: (x[1].get("chain_order", 0), x[0].data.get("name")))
            
            self.log.info(f"Processing chain '{group_name}' with {len(items)} jobs.")
            
            previous_instance = None
            
            for instance, attrs in items:
                # Store the batch name on the instance for the submitter to use
                instance.data["jobBatchName"] = group_name
                
                if attrs.get("chain_behavior") == "dependency":
                    if previous_instance:
                        # We attach the actual Instance object here.
                        # The Farm Submitter must be updated to read this object 
                        # and retrieve the submitted Job ID from it.
                        deps = instance.data.get("farm_instance_dependencies", [])
                        deps.append(previous_instance)
                        instance.data["farm_instance_dependencies"] = deps
                        
                        self.log.info(
                            f"  > Linked {instance.data['name']} "
                            f"to wait for {previous_instance.data['name']}"
                        )
                    else:
                        self.log.info(f"  > {instance.data['name']} is first in chain.")
                
                previous_instance = instance
