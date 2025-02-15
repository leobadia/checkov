from __future__ import annotations

import itertools
import typing
from collections import defaultdict
from copy import deepcopy
from typing import Any

from checkov.common.util.consts import RESOLVED_MODULE_ENTRY_NAME
from checkov.terraform import TFModule
from checkov.terraform.graph_builder.foreach.abstract_handler import ForeachAbstractHandler
from checkov.terraform.graph_builder.foreach.consts import FOREACH_STRING, COUNT_STRING
from checkov.terraform.graph_builder.graph_components.block_types import BlockType
from checkov.terraform.graph_builder.graph_components.blocks import TerraformBlock

if typing.TYPE_CHECKING:
    from checkov.terraform.graph_builder.local_graph import TerraformLocalGraph


class ForeachModuleHandler(ForeachAbstractHandler):
    def __init__(self, local_graph: TerraformLocalGraph):
        super().__init__(local_graph)

    def handle(self, modules_blocks: list[int]) -> None:
        """
        modules_blocks (list[int]): list of module blocks indexes in the graph that contains for_each / counts.
        """
        if not modules_blocks:
            return
        current_level = [None]
        main_module_modules = deepcopy(self.local_graph.vertices_by_module_dependency.get(None)[BlockType.MODULE])
        modules_to_render = main_module_modules

        while modules_to_render:
            modules_to_render = self._render_foreach_modules_by_levels(modules_blocks, modules_to_render, current_level)

    def _render_foreach_modules_by_levels(self, modules_blocks: list[int], modules_to_render: list[int],
                                          current_level: list[int | None]) -> list[int]:
        """
        modules_blocks: The module blocks with for_each/count statement in the graph.
        modules_to_render: The list of modules indexes to render at this iteration.
        current_level: The parent current level that we are running on this iteration (first will be None).

        return: the next (list) of the modules to render.

        For example: at this folder - tests/terraform/graph/variable_rendering/resources/foreach_module_dup_foreach
        We will run over the levels by:
        First level -> s3_module and s3_module2 (Copying the module and all his dependencies)
        Second level -> inner_s3_module and inner_s3_module2 (Copying the module and all his dependencies)
        This will generate a graph with 20 modules and 16 resources.
        """
        sub_graph = self._build_sub_graph(modules_blocks)
        self._render_sub_graph(sub_graph, blocks_to_render=modules_blocks)
        for module_idx in modules_to_render:
            module_block = self.local_graph.vertices[module_idx]
            for_each = module_block.attributes.get(FOREACH_STRING)
            count = module_block.attributes.get(COUNT_STRING)
            if for_each:
                for_each = self._handle_static_statement(module_idx, sub_graph)
                if not self._is_static_statement(module_idx, sub_graph):
                    continue
                self._duplicate_module_with_for_each(module_idx, for_each)
            elif count:
                count = self._handle_static_statement(module_idx, sub_graph)
                if not self._is_static_statement(module_idx, sub_graph):
                    continue
                self._duplicate_module_with_count(module_idx, count)
        return self._get_modules_to_render(current_level)

    def _duplicate_module_with_for_each(self, module_idx: int, for_each: dict[str, Any] | list[str]) -> None:
        self._create_new_resources_foreach(for_each, module_idx)

    def _duplicate_module_with_count(self, module_idx: int, count: int) -> None:
        self._create_new_resources_count(count, module_idx)

    def _get_modules_to_render(self, current_level: list[TFModule | None]) -> list[int]:
        rendered_modules = [self.local_graph.vertices_by_module_dependency[curr][BlockType.MODULE] for curr in current_level][0]
        current_level.clear()
        for m_idx in rendered_modules:
            current_level.append(self._get_current_tf_module_object(m_idx))
        modules_to_render = [self.local_graph.vertices_by_module_dependency[curr][BlockType.MODULE] for curr in current_level]
        return list(itertools.chain.from_iterable(modules_to_render))

    def _get_current_tf_module_object(self, m_idx: int) -> TFModule:
        m = self.local_graph.vertices[m_idx]
        m_name = m.name.split('[')[0]
        return TFModule(m.path, m_name, m.source_module_object, m.for_each_index)

    def _create_new_resources_foreach(self, statement: list[str] | dict[str, Any], block_idx: int) -> None:
        # Important it will be before the super call to avoid changes occuring from super
        main_resource = self.local_graph.vertices[block_idx]
        super()._create_new_resources_foreach(statement, block_idx)

        if isinstance(statement, list):
            for i, new_value in enumerate(statement):
                should_override = True if i == 0 else False
                self._update_module_children(main_resource, new_value, should_override_foreach_key=should_override)
        elif isinstance(statement, dict):
            for i, (new_key, _) in enumerate(statement.items()):
                should_override = True if i == 0 else False
                self._update_module_children(main_resource, new_key, should_override_foreach_key=should_override)

    def _create_new_foreach_resource(self, block_idx: int, foreach_idx: int, main_resource: TerraformBlock,
                                     new_key: int | str, new_value: int | str) -> None:
        self._create_new_module(main_resource, new_value, new_key=new_key, resource_idx=block_idx,
                                foreach_idx=foreach_idx)

    def _update_module_children(self, main_resource: TerraformBlock,
                                original_foreach_or_count_key: int | str,
                                should_override_foreach_key: bool = True) -> None:
        original_module_key = TFModule(path=main_resource.path, name=main_resource.name,
                                       nested_tf_module=main_resource.source_module_object)

        if not should_override_foreach_key:
            original_module_key.foreach_idx = original_foreach_or_count_key

        self._update_children_foreach_index(original_foreach_or_count_key, original_module_key,
                                            should_override_foreach_key=should_override_foreach_key)

    def _create_new_resources_count(self, statement: int, block_idx: int) -> None:
        main_resource = self.local_graph.vertices[block_idx]
        for i in range(statement):
            self._create_new_module(main_resource, i, resource_idx=block_idx, foreach_idx=i)

        # We purposely do it at the end to avoid influencing data structures in the middle of an update
        for i in range(statement):
            should_override = True if i == 0 else False
            self._update_module_children(main_resource, i, should_override_foreach_key=should_override)

    def _update_children_foreach_index(self, original_foreach_or_count_key: int | str, original_module_key: TFModule,
                                       current_module_key: TFModule | None = None,
                                       should_override_foreach_key: bool = True) -> None:
        """
        Go through all child vertices and update source_module_object with foreach_idx
        """
        if current_module_key is None:
            current_module_key = deepcopy(original_module_key)
        if current_module_key not in self.local_graph.vertices_by_module_dependency:
            return
        values = self.local_graph.vertices_by_module_dependency[current_module_key].values()
        for child_indexes in values:
            for child_index in child_indexes:
                child = self.local_graph.vertices[child_index]

                self._update_nested_tf_module_foreach_idx(original_foreach_or_count_key, original_module_key,
                                                          child.source_module_object)
                self._update_resolved_entry_for_tf_definition(child, original_foreach_or_count_key, original_module_key)

                # Important to copy to avoid changing the object by reference
                child_source_module_object_copy = deepcopy(child.source_module_object)
                if should_override_foreach_key:
                    child_source_module_object_copy.foreach_idx = None

                child_module_key = TFModule(path=child.path, name=child.name,
                                            nested_tf_module=child_source_module_object_copy,
                                            foreach_idx=child.for_each_index)
                del child_source_module_object_copy
                self._update_children_foreach_index(original_foreach_or_count_key, original_module_key,
                                                    child_module_key)

    def _create_new_module(
            self,
            main_resource: TerraformBlock,
            new_value: int | str,
            resource_idx: int,
            foreach_idx: int,
            new_key: int | str | None = None) -> None:
        new_resource = deepcopy(main_resource)
        block_name = new_resource.name
        config_attrs = new_resource.config.get(block_name, {})
        key_to_val_changes = self._build_key_to_val_changes(main_resource, new_value, new_key)
        self._update_foreach_attrs(config_attrs, key_to_val_changes, new_resource)
        idx_to_change = new_key or new_value
        new_resource.for_each_index = idx_to_change

        main_resource_module_key = TFModule(
            path=new_resource.path,
            name=main_resource.name,
            nested_tf_module=new_resource.source_module_object,
        )

        # Without making this copy the test don't pass, as we might access the data structure in the middle of an update
        copy_of_vertices_by_module_dependency = deepcopy(self.local_graph.vertices_by_module_dependency)
        main_resource_module_value = deepcopy(copy_of_vertices_by_module_dependency[main_resource_module_key])
        new_resource_module_key = TFModule(new_resource.path, new_resource.name, new_resource.source_module_object,
                                           idx_to_change)

        self._update_block_name_and_id(new_resource, idx_to_change)
        self._update_resolved_entry_for_tf_definition(new_resource, idx_to_change, main_resource_module_key)
        if foreach_idx != 0:
            self.local_graph.vertices.append(new_resource)
            self._create_new_module_with_vertices(main_resource, main_resource_module_value, resource_idx, new_resource,
                                                  new_resource_module_key)
        else:
            self.local_graph.vertices[resource_idx] = new_resource

            key_with_foreach_index = deepcopy(main_resource_module_key)
            key_with_foreach_index.foreach_idx = idx_to_change
            self.local_graph.vertices_by_module_dependency[key_with_foreach_index] = main_resource_module_value

        del copy_of_vertices_by_module_dependency, new_resource, main_resource_module_key, main_resource_module_value

    def _create_new_module_with_vertices(self, main_resource: TerraformBlock,
                                         main_resource_module_value: dict[str, list[int]],
                                         resource_idx: Any, new_resource: TerraformBlock | None = None,
                                         new_resource_module_key: TFModule | None = None) -> None:
        if new_resource is None:
            new_resource = deepcopy(main_resource)
            new_resource_module_key = TFModule(new_resource.path, new_resource.name, new_resource.source_module_object,
                                               new_resource.for_each_index)
            del new_resource

        new_resource_vertex_idx = len(self.local_graph.vertices) - 1
        original_vertex_source_module = self.local_graph.vertices[resource_idx].source_module_object
        if original_vertex_source_module:
            source_module_key = TFModule(
                path=original_vertex_source_module.path,
                name=original_vertex_source_module.name,
                nested_tf_module=original_vertex_source_module.nested_tf_module,
            )
        else:
            source_module_key = None
        self.local_graph.vertices_by_module_dependency[source_module_key][BlockType.MODULE].append(
            new_resource_vertex_idx)
        new_vertices_module_value = self._add_new_vertices_for_module(new_resource_module_key,
                                                                      main_resource_module_value,
                                                                      new_resource_vertex_idx)
        self.local_graph.vertices_by_module_dependency.update({new_resource_module_key: new_vertices_module_value})

    def _add_new_vertices_for_module(self, new_module_key: TFModule, new_module_value: dict[str, list[int]],
                                     new_resource_vertex_idx: int) -> dict[str, list[int]]:
        new_vertices_module_value: dict[str, list[int]] = defaultdict(list)
        for vertex_type, vertices_idx in new_module_value.items():
            for vertex_idx in vertices_idx:
                module_vertex = self.local_graph.vertices[vertex_idx]
                new_vertex = deepcopy(module_vertex)
                new_vertex.source_module_object = new_module_key
                self.local_graph.vertices.append(new_vertex)

                # Update source module based on the new added vertex
                new_vertex.source_module.pop()
                new_vertex.source_module.add(new_resource_vertex_idx)

                new_vertex_idx = len(self.local_graph.vertices) - 1
                new_vertices_module_value[vertex_type].append(new_vertex_idx)

                if vertex_type == BlockType.MODULE:
                    module_vertex_key = TFModule(path=module_vertex.path, name=module_vertex.name,
                                                 nested_tf_module=module_vertex.source_module_object,
                                                 foreach_idx=module_vertex.for_each_index)
                    module_vertex_value = self.local_graph.vertices_by_module_dependency[module_vertex_key]
                    self._create_new_module_with_vertices(new_vertex, module_vertex_value, new_vertex_idx)

        return new_vertices_module_value

    @staticmethod
    def _update_resolved_entry_for_tf_definition(child: TerraformBlock, original_foreach_or_count_key: int | str,
                                                 original_module_key: TFModule) -> None:
        if child.block_type == BlockType.RESOURCE:
            child_name, child_type = child.name.split('.')
            config = child.config[child_name][child_type]
        else:
            config = child.config.get(child.name)
        if isinstance(config, dict) and config.get(RESOLVED_MODULE_ENTRY_NAME) is not None and \
                len(config.get(RESOLVED_MODULE_ENTRY_NAME)) > 0:
            tf_moudle: TFModule = config[RESOLVED_MODULE_ENTRY_NAME][0].tf_source_modules
            ForeachAbstractHandler._update_nested_tf_module_foreach_idx(original_foreach_or_count_key,
                                                                        original_module_key,
                                                                        tf_moudle)
