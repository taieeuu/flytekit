from __future__ import annotations

import datetime
import typing
from typing import Any, Dict, List, Optional, Union
from typing import Literal as L

from flyteidl.core import tasks_pb2

from flytekit.core.cache import Cache
from flytekit.core.pod_template import PodTemplate
from flytekit.core.resources import (
    Resources,
    ResourceSpec,
    construct_extended_resources,
    convert_resources_to_resource_model,
)
from flytekit.core.utils import _dnsify
from flytekit.extras.accelerators import BaseAccelerator
from flytekit.loggers import logger
from flytekit.models import literals as _literal_models
from flytekit.models.core import workflow as _workflow_model
from flytekit.models.task import Resources as _resources_model


def assert_not_promise(v: Any, location: str):
    """
    This function will raise an exception if the value is a promise. This should be used to ensure that we don't
    accidentally use a promise in a place where we don't support it.
    """
    from flytekit.core.promise import Promise

    if isinstance(v, Promise):
        raise AssertionError(f"Cannot use a promise in the {location} Value: {v}")


def assert_no_promises_in_resources(resources: _resources_model):
    """
    This function will raise an exception if any of the resources have promises in them. This is because we don't
    support promises in resources / runtime overriding of resources through input values.
    """
    if resources is None:
        return
    if resources.requests is not None:
        for r in resources.requests:
            assert_not_promise(r.value, "resources.requests")
    if resources.limits is not None:
        for r in resources.limits:
            assert_not_promise(r.value, "resources.limits")


class Node(object):
    """
    This class will hold all the things necessary to make an SdkNode but we won't make one until we know things like
    ID, which from the registration step
    """

    TIMEOUT_OVERRIDE_SENTINEL = object()

    def __init__(
        self,
        id: str,
        metadata: _workflow_model.NodeMetadata,
        bindings: List[_literal_models.Binding],
        upstream_nodes: List[Node],
        flyte_entity: Any,
    ):
        if id is None:
            raise ValueError("Illegal construction of node, without a Node ID")
        self._id = _dnsify(id)
        self._metadata = metadata
        self._bindings = bindings
        self._upstream_nodes = upstream_nodes
        self._flyte_entity = flyte_entity
        self._aliases: _workflow_model.Alias = None
        self._outputs = None
        self._resources: typing.Optional[_resources_model] = None
        self._extended_resources: typing.Optional[tasks_pb2.ExtendedResources] = None
        self._container_image: typing.Optional[str] = None
        self._pod_template: typing.Optional[PodTemplate] = None

    def runs_before(self, other: Node):
        """
        This is typically something we shouldn't do. This modifies an attribute of the other instance rather than
        self. But it's done so only because we wanted this English function to be the same as the shift function.
        That is, calling node_1.runs_before(node_2) and node_1 >> node_2 are the same. The shift operator going the
        other direction is not implemented to further avoid confusion. Right shift was picked rather than left shift
        because that's what most users are familiar with.
        """
        if self not in other._upstream_nodes:
            other._upstream_nodes.append(self)

    def __rshift__(self, other: Node):
        self.runs_before(other)
        return other

    @property
    def name(self) -> str:
        return self._id

    @property
    def outputs(self):
        if self._outputs is None:
            raise AssertionError("Cannot use outputs with all Nodes, node must've been created from create_node()")
        return self._outputs

    @property
    def id(self) -> str:
        return self._id

    @property
    def bindings(self) -> List[_literal_models.Binding]:
        return self._bindings

    @property
    def upstream_nodes(self) -> List[Node]:
        return self._upstream_nodes

    @property
    def flyte_entity(self) -> Any:
        return self._flyte_entity

    @property
    def run_entity(self) -> Any:
        from flytekit.core.array_node_map_task import ArrayNodeMapTask
        from flytekit.core.legacy_map_task import MapPythonTask

        if isinstance(self.flyte_entity, MapPythonTask):
            return self.flyte_entity.run_task
        if isinstance(self.flyte_entity, ArrayNodeMapTask):
            return self.flyte_entity.python_function_task
        return self.flyte_entity

    @property
    def metadata(self) -> _workflow_model.NodeMetadata:
        return self._metadata

    def _override_node_metadata(
        self,
        name,
        timeout: Optional[Union[int, datetime.timedelta, object]] = TIMEOUT_OVERRIDE_SENTINEL,
        retries: Optional[int] = None,
        interruptible: typing.Optional[bool] = None,
        cache: Optional[Union[bool, Cache]] = None,
        **kwargs,
    ):
        from flytekit.core.array_node_map_task import ArrayNodeMapTask

        # Maintain backwards compatibility with the old cache parameters,
        # while cleaning up the task function definition.
        cache_serialize = kwargs.get("cache_serialize")
        cache_version = kwargs.get("cache_version")
        # TODO support ignore_input_vars in with_overrides
        cache_ignore_input_vars = kwargs.get("cache_ignore_input_vars")

        if isinstance(self.flyte_entity, ArrayNodeMapTask):
            # override the sub-node's metadata
            node_metadata = self.flyte_entity.sub_node_metadata
        else:
            node_metadata = self._metadata

        if timeout is not Node.TIMEOUT_OVERRIDE_SENTINEL:
            if timeout is None:
                node_metadata._timeout = datetime.timedelta()
            elif isinstance(timeout, int):
                node_metadata._timeout = datetime.timedelta(seconds=timeout)
            elif isinstance(timeout, datetime.timedelta):
                node_metadata._timeout = timeout
            else:
                raise ValueError("timeout should be duration represented as either a datetime.timedelta or int seconds")

        if retries is not None:
            assert_not_promise(retries, "retries")
            node_metadata._retries = (
                _literal_models.RetryStrategy(0) if retries is None else _literal_models.RetryStrategy(retries)
            )

        if interruptible is not None:
            assert_not_promise(interruptible, "interruptible")
            node_metadata._interruptible = interruptible

        if name is not None:
            node_metadata._name = name

        if cache is not None:
            assert_not_promise(cache, "cache")

            # Note: any future changes should look into how these cache params are set in tasks
            # If the cache is of type bool but cache_version is not set, then assume that we want to use the
            # default cache policies in Cache
            if isinstance(cache, bool) and cache is True and cache_version is None:
                cache = Cache(
                    serialize=cache_serialize if cache_serialize is not None else False,
                    ignored_inputs=cache_ignore_input_vars if cache_ignore_input_vars is not None else tuple(),
                )

            if isinstance(cache, Cache):
                # Validate that none of the deprecated cache-related parameters are set.
                # if cache_serialize is not None or cache_version is not None or cache_ignore_input_vars is not None:
                if cache_serialize is not None or cache_version is not None:
                    raise ValueError(
                        "cache_serialize, cache_version, and cache_ignore_input_vars are deprecated. Please use Cache object"
                    )

                # TODO support unset cache version in with_overrides
                if cache.version is None:
                    raise ValueError("must specify cache version when overriding")

                cache_version = cache.version
                cache_serialize = cache.serialize
                cache = True

        node_metadata._cacheable = cache

        if cache_version is not None:
            assert_not_promise(cache_version, "cache_version")
            node_metadata._cache_version = cache_version

        if cache_serialize is not None:
            assert_not_promise(cache_serialize, "cache_serialize")
            node_metadata._cache_serializable = cache_serialize

    def with_overrides(
        self,
        node_name: Optional[str] = None,
        aliases: Optional[Dict[str, str]] = None,
        requests: Optional[Resources] = None,
        limits: Optional[Resources] = None,
        timeout: Optional[Union[int, datetime.timedelta, object]] = TIMEOUT_OVERRIDE_SENTINEL,
        retries: Optional[int] = None,
        interruptible: Optional[bool] = None,
        name: Optional[str] = None,
        task_config: Optional[Any] = None,
        container_image: Optional[str] = None,
        accelerator: Optional[BaseAccelerator] = None,
        cache: Optional[Union[bool, Cache]] = None,
        shared_memory: Optional[Union[L[True], str]] = None,
        pod_template: Optional[PodTemplate] = None,
        resources: Optional[Resources] = None,
        *args,
        **kwargs,
    ):
        if node_name is not None:
            # Convert the node name into a DNS-compliant.
            # https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#dns-subdomain-names
            assert_not_promise(node_name, "node_name")
            self._id = _dnsify(node_name)

        if aliases is not None:
            if not isinstance(aliases, dict):
                raise AssertionError("Aliases should be specified as dict[str, str]")
            self._aliases = []
            for k, v in aliases.items():
                self._aliases.append(_workflow_model.Alias(var=k, alias=v))

        if resources is not None:
            if limits is not None or requests is not None:
                msg = "`resource` can not be used together with the `limits` or `requests`. Please only set `resource`."
                raise ValueError(msg)
            resource_spec = ResourceSpec.from_multiple_resource(resources)
            requests = resource_spec.requests
            limits = resource_spec.limits

        if requests is not None or limits is not None:
            if requests and not isinstance(requests, Resources):
                raise AssertionError("requests should be specified as flytekit.Resources")
            if limits and not isinstance(limits, Resources):
                raise AssertionError("limits should be specified as flytekit.Resources")

            if not limits:
                logger.warning(
                    (
                        f"Requests overridden on node {self.id} ({self.metadata.short_string()}) without specifying limits. "
                        "Requests are clamped to original limits."
                    )
                )

            resources_ = convert_resources_to_resource_model(requests=requests, limits=limits)
            assert_no_promises_in_resources(resources_)
            self._resources = resources_

        if task_config is not None:
            logger.warning("This override is beta. We may want to revisit this in the future.")
            if not isinstance(task_config, type(self.run_entity._task_config)):
                raise ValueError("can't change the type of the task config")
            self.run_entity._task_config = task_config

        if container_image is not None:
            assert_not_promise(container_image, "container_image")
            self._container_image = container_image

        if accelerator is not None:
            assert_not_promise(accelerator, "accelerator")

        if shared_memory is not None:
            assert_not_promise(shared_memory, "shared_memory")

        self._extended_resources = construct_extended_resources(accelerator=accelerator, shared_memory=shared_memory)

        self._override_node_metadata(name, timeout, retries, interruptible, cache, **kwargs)

        if pod_template is not None:
            assert_not_promise(pod_template, "podtemplate")
            self._pod_template = pod_template

        return self


def _convert_resource_overrides(
    resources: typing.Optional[Resources], resource_name: str
) -> typing.List[_resources_model.ResourceEntry]:
    if resources is None:
        return []

    resource_entries = []
    if resources.cpu is not None:
        resource_entries.append(_resources_model.ResourceEntry(_resources_model.ResourceName.CPU, resources.cpu))

    if resources.mem is not None:
        resource_entries.append(_resources_model.ResourceEntry(_resources_model.ResourceName.MEMORY, resources.mem))

    if resources.gpu is not None:
        resource_entries.append(_resources_model.ResourceEntry(_resources_model.ResourceName.GPU, resources.gpu))

    if resources.ephemeral_storage is not None:
        resource_entries.append(
            _resources_model.ResourceEntry(
                _resources_model.ResourceName.EPHEMERAL_STORAGE,
                resources.ephemeral_storage,
            )
        )

    return resource_entries
