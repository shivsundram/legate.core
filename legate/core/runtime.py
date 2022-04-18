# Copyright 2021 NVIDIA Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import gc
import math
import struct
import weakref
from collections import deque
from functools import reduce

from legion_cffi import ffi  # Make sure we only have one ffi instance
from legion_top import cleanup_items, top_level

from legate.core import types as ty

import datetime

from .context import Context
from .corelib import CoreLib
from .launcher import TaskLauncher
from .legion import (
    FieldSpace,
    Future,
    IndexSpace,
    OutputRegion,
    Rect,
    Region,
    legate_task_postamble,
    legate_task_preamble,
    legion,
)
from .partition import Restriction
from .shape import Shape
from .solver import Partitioner, Strategy
from .store import RegionField, Store, FusionMetadata
import numpy as np
from .constraints import Alignment



# A Field holds a reference to a field in a region tree
# that can be used by many different RegionField objects
class Field(object):
    __slots__ = [
        "runtime",
        "region",
        "field_id",
        "dtype",
        "shape",
        "own",
    ]

    def __init__(
        self,
        runtime,
        region,
        field_id,
        dtype,
        shape,
        own=True,
    ):
        self.runtime = runtime
        self.region = region
        self.field_id = field_id
        self.dtype = dtype
        self.shape = shape
        self.own = own

    def same_handle(self, other):
        return type(self) == type(other) and self.field_id == other.field_id

    def __str__(self):
        return f"Field({self.field_id})"

    def __del__(self):
        if self.own:
            # Return our field back to the runtime
            self.runtime.free_field(
                self.region,
                self.field_id,
                self.dtype,
                self.shape,
            )


_sizeof_int = ffi.sizeof("int")
_sizeof_size_t = ffi.sizeof("size_t")
assert _sizeof_size_t == 4 or _sizeof_size_t == 8


# A helper class for doing field management with control replication
class FieldMatch(object):
    __slots__ = ["manager", "fields", "input", "output", "future"]

    def __init__(self, manager, fields):
        self.manager = manager
        self.fields = fields
        # Allocate arrays of ints that are twice as long as fields since
        # our values will be 'field_id,tree_id' pairs
        if len(fields) > 0:
            alloc_string = "int[" + str(2 * len(fields)) + "]"
            self.input = ffi.new(alloc_string)
            self.output = ffi.new(alloc_string)
            # Fill in the input buffer with our data
            for idx in range(len(fields)):
                region, field_id = fields[idx]
                self.input[2 * idx] = region.handle.tree_id
                self.input[2 * idx + 1] = field_id
        else:
            self.input = ffi.NULL
            self.output = ffi.NULL
        self.future = None

    def launch(self, runtime, context):
        assert self.future is None
        self.future = Future(
            legion.legion_context_consensus_match(
                runtime,
                context,
                self.input,
                self.output,
                len(self.fields),
                2 * _sizeof_int,
            )
        )
        return self.future

    def update_free_fields(self):
        # If we know there are no fields then we can be done early
        if len(self.fields) == 0:
            return
        # Wait for the future to be ready
        if not self.future.is_ready():
            self.future.wait()
        # Get the size of the buffer in the returned
        if _sizeof_size_t == 4:
            num_fields = struct.unpack_from("I", self.future.get_buffer(4))[0]
        else:
            num_fields = struct.unpack_from("Q", self.future.get_buffer(8))[0]
        assert num_fields <= len(self.fields)
        if num_fields > 0:
            # Put all the returned fields onto the ordered queue in the order
            # that they are in this list since we know
            ordered_fields = [None] * num_fields
            for region, field_id in self.fields:
                found = False
                for idx in range(num_fields):
                    if self.output[2 * idx] != region.handle.tree_id:
                        continue
                    if self.output[2 * idx + 1] != field_id:
                        continue
                    assert ordered_fields[idx] is None
                    ordered_fields[idx] = (region, field_id)
                    found = True
                    break
                if not found:
                    # Not found so put it back int the unordered queue
                    self.manager.free_field(region, field_id, ordered=False)
            # Notice that we do this in the order of the list which is the
            # same order as they were in the array returned by the match
            for region, field_id in ordered_fields:
                self.manager.free_field(region, field_id, ordered=True)
        else:
            # No fields on all shards so put all our fields back into
            # the unorered queue
            for region, field_id in self.fields:
                self.manager.free_field(region, field_id, ordered=False)


# This class manages all regions of the same shape.
class RegionManager(object):
    def __init__(self, runtime, shape):
        self._runtime = runtime
        self._shape = shape
        self._top_regions = []
        self._region_set = set()

    def __del__(self):
        self._shape = None
        self._top_regions = None
        self._region_set = None

    def destroy(self):
        while self._top_regions:
            region = self._top_regions.pop()
            region.destroy()
        self._top_regions = []
        self._region_set = set()

    def import_region(self, region):
        if region not in self._region_set:
            self._top_regions.append(region)
            self._region_set.add(region)

    @property
    def active_region(self):
        return self._top_regions[-1]

    @property
    def has_space(self):
        return (
            len(self._top_regions) > 0
            and self.active_region.field_space.has_space
        )

    def _create_region(self):
        # Note that the regions created in this method are always fresh
        # so we don't need to de-duplicate them to keep track of their
        # life cycles correctly.
        index_space = self._shape.get_index_space(self._runtime)
        field_space = self._runtime.create_field_space()
        region = self._runtime.create_region(index_space, field_space)
        self._top_regions.append(region)
        self._region_set.add(region)

    def allocate_field(self, dtype):
        if not self.has_space:
            self._create_region()
        region = self.active_region
        field_id = region.field_space.allocate_field(dtype)
        return region, field_id


# This class manages the allocation and reuse of fields
class FieldManager(object):
    def __init__(self, runtime, shape, dtype):
        self.runtime = runtime
        self.shape = shape
        self.dtype = dtype
        # This is a sanitized list of (region,field_id) pairs that is
        # guaranteed to be ordered across all the shards even with
        # control replication
        self.free_fields = deque()
        # This is an unsanitized list of (region,field_id) pairs which is not
        # guaranteed to be ordered across all shards with control replication
        self.freed_fields = list()
        # A list of match operations that have been issued and for which
        # we are waiting for values to come back
        self.matches = deque()
        self.match_counter = 0
        # Figure out how big our match frequency is based on our size
        volume = reduce(lambda x, y: x * y, self.shape)
        size = volume * self.dtype.size
        if size > runtime.max_field_reuse_size:
            # Figure out the ratio our size to the max reuse size (round up)
            ratio = (
                size + runtime.max_field_reuse_size - 1
            ) // runtime.max_field_reuse_size
            assert ratio >= 1
            # Scale the frequency by the ratio, but make it at least 1
            self.match_frequency = (
                runtime.max_field_reuse_frequency + ratio - 1
            ) // ratio
        else:
            self.match_frequency = runtime.max_field_reuse_frequency

    def destroy(self):
        self.free_fields = None
        self.freed_fields = None
        self.fill_space = None

    def allocate_field(self):
        # Increment our match counter
        self.match_counter += 1
        # If the match counter equals our match frequency then do an exchange
        if self.match_counter == self.match_frequency:
            # This is where the rubber meets the road between control
            # replication and garbage collection. We need to see if there
            # are any freed fields that are shared across all the shards.
            # We have to test this deterministically no matter what even
            # if we don't have any fields to offer ourselves because this
            # is a collective with other shards. If we have any we can use
            # the first one and put the remainder on our free fields list
            # so that we can reuse them later. If there aren't any then
            # all the shards will go allocate a new field.
            local_freed_fields = self.freed_fields
            # The match now owns our freed fields so make a new list
            # Have to do this before dispatching the match
            self.freed_fields = list()
            match = FieldMatch(self, local_freed_fields)
            # Dispatch the match
            self.runtime.dispatch(match)
            # Put it on the deque of outstanding matches
            self.matches.append(match)
            # Reset the match counter back to 0
            self.match_counter = 0
        # First, if we have a free field then we know everyone has one of those
        if len(self.free_fields) > 0:
            return self.free_fields.popleft()
        # If we don't have any free fields then see if we have a pending match
        # outstanding that we can now add to our free fields and use
        while len(self.matches) > 0:
            match = self.matches.popleft()
            match.update_free_fields()
            # Check again to see if we have any free fields
            if len(self.free_fields) > 0:
                return self.free_fields.popleft()

        region_manager = self.runtime.find_or_create_region_manager(self.shape)
        return region_manager.allocate_field(self.dtype)

    def free_field(self, region, field_id, ordered=False):
        if ordered:
            if self.free_fields is not None:
                self.free_fields.append((region, field_id))
        else:  # Put this on the unordered list
            if self.freed_fields is not None:
                self.freed_fields.append((region, field_id))


class Attachment(object):
    def __init__(self, ptr, extent, shareable, region_field):
        self.ptr = ptr
        self.extent = extent
        self.end = ptr + extent - 1
        # Catch a case where we try to (individually) re-attach a buffer that
        # was used in an IndexAttach. In that case it would be wrong to return
        # the RegionField produced by that IndexAttach, since the buffer in
        # question only covers a part of that.
        self.shareable = shareable
        self._region_field = weakref.ref(region_field)

    def overlaps(self, other):
        return not (self.end < other.ptr or other.end < self.ptr)

    @property
    def region_field(self):
        return self._region_field()

    @region_field.setter
    def region_field(self, region_field):
        self._region_field = weakref.ref(region_field)


class AttachmentManager(object):
    def __init__(self, runtime):
        self._runtime = runtime
        self._attachments = dict()
        self._next_detachment_key = 0
        self._registered_detachments = dict()
        self._deferred_detachments = list()
        self._pending_detachments = dict()

    def destroy(self):
        gc.collect()
        while self._deferred_detachments:
            self.perform_detachments()
            # Make sure progress is made on any of these operations
            self._runtime._progress_unordered_operations()
            gc.collect()
        # Always make sure we wait for any pending detachments to be done
        # so that we don't lose the references and make the GC unhappy
        gc.collect()
        while self._pending_detachments:
            self.prune_detachments()
            gc.collect()

        # Clean up our attachments so that they can be collected
        self._attachments = None

    @staticmethod
    def attachment_key(buf):
        assert isinstance(buf, memoryview)
        base_ptr = int(ffi.cast("uintptr_t", ffi.from_buffer(buf)))
        return (base_ptr, buf.nbytes)

    def has_attachment(self, buf):
        key = self.attachment_key(buf)
        attachment = self._attachments.get(key, None)
        return attachment is not None and attachment.region_field is not None

    def reuse_existing_attachment(self, buf):
        key = self.attachment_key(buf)
        attachment = self._attachments.get(key, None)
        if attachment is None:
            return None
        rf = attachment.region_field
        # If the region field is already collected, we don't need to keep
        # track of it for de-duplication.
        if rf is None:
            del self._attachments[key]
            return None
        return rf if attachment.shareable else None

    def _add_attachment(self, buf, shareable, region_field):
        key = self.attachment_key(buf)
        attachment = self._attachments.get(key, None)
        if not (attachment is None or attachment.region_field is None):
            raise RuntimeError(
                "Cannot attach two different RegionFields to the same buffer"
            )
        # If the region field is already collected, we don't need to keep
        # track of it for de-duplication.
        if attachment is not None:
            del self._attachments[key]
        attachment = Attachment(*key, shareable, region_field)
        for other in self._attachments.values():
            if other.overlaps(attachment):
                raise RuntimeError(
                    "Aliased attachments not supported by Legate"
                )
        self._attachments[key] = attachment

    def attach_external_allocation(self, alloc, region_field):
        if isinstance(alloc, memoryview):
            self._add_attachment(alloc, True, region_field)
        else:
            for buf in alloc.shard_local_buffers.values():
                self._add_attachment(buf, False, region_field)

    def _remove_attachment(self, buf):
        key = self.attachment_key(buf)
        if key not in self._attachments:
            raise RuntimeError("Unable to find attachment to remove")
        del self._attachments[key]

    def _remove_allocation(self, alloc):
        if isinstance(alloc, memoryview):
            self._remove_attachment(alloc)
        else:
            for buf in alloc.shard_local_buffers.values():
                self._remove_attachment(buf)

    def detach_external_allocation(
        self, alloc, detach, defer=False, previously_deferred=False
    ):
        # If the detachment was previously deferred, then we don't
        # need to remove the allocation from the map again.
        if not previously_deferred:
            self._remove_allocation(alloc)
        if defer:
            # If we need to defer this until later do that now
            self._deferred_detachments.append((alloc, detach))
            return
        future = self._runtime.dispatch(detach)
        # Dangle a reference to the field off the future to prevent the
        # field from being recycled until the detach is done
        future.field_reference = detach.field
        # If the future is already ready, then no need to track it
        if future.is_ready():
            return
        self._pending_detachments[future] = alloc

    def register_detachment(self, detach):
        key = self._next_detachment_key
        self._registered_detachments[key] = detach
        self._next_detachment_key += 1
        return key

    def remove_detachment(self, detach_key):
        detach = self._registered_detachments[detach_key]
        del self._registered_detachments[detach_key]
        return detach

    def perform_detachments(self):
        detachments = self._deferred_detachments
        self._deferred_detachments = list()
        for alloc, detach in detachments:
            self.detach_external_allocation(
                alloc, detach, defer=False, previously_deferred=True
            )

    def prune_detachments(self):
        to_remove = []
        for future in self._pending_detachments.keys():
            if future.is_ready():
                to_remove.append(future)
        for future in to_remove:
            del self._pending_detachments[future]


class PartitionManager(object):
    def __init__(self, runtime):
        self._runtime = runtime
        self._num_pieces = runtime.core_context.get_tunable(
            runtime.core_library.LEGATE_CORE_TUNABLE_NUM_PIECES,
            ty.int32,
        )
        self._min_shard_volume = runtime.core_context.get_tunable(
            runtime.core_library.LEGATE_CORE_TUNABLE_MIN_SHARD_VOLUME,
            ty.int64,
        )

        self._launch_spaces = {}
        factors = list()
        pieces = self._num_pieces
        while pieces % 2 == 0:
            factors.append(2)
            pieces = pieces // 2
        while pieces % 3 == 0:
            factors.append(3)
            pieces = pieces // 3
        while pieces % 5 == 0:
            factors.append(5)
            pieces = pieces // 5
        while pieces % 7 == 0:
            factors.append(7)
            pieces = pieces // 7
        while pieces % 11 == 0:
            factors.append(11)
            pieces = pieces // 11
        if pieces > 1:
            raise ValueError(
                "legate.numpy currently doesn't support processor "
                + "counts with large prime factors greater than 11"
            )
        self._piece_factors = list(reversed(factors))
        self._index_partitions = {}

    def compute_launch_shape(self, store, restrictions):
        shape = store.shape
        assert len(restrictions) == shape.ndim

        to_partition = ()
        for dim, restriction in enumerate(restrictions):
            if restriction != Restriction.RESTRICTED:
                to_partition += (shape[dim],)
        launch_shape = self._compute_launch_shape(to_partition)
        if launch_shape is None:
            return None

        idx = 0
        result = ()
        for restriction in restrictions:
            if restriction != Restriction.RESTRICTED:
                result += (launch_shape[idx],)
                idx += 1
            else:
                result += (1,)

        return Shape(result)

    def _compute_launch_shape(self, shape):
        assert self._num_pieces > 0
        # Easy case if we only have one piece: no parallel launch space
        if self._num_pieces == 1:
            return None
        # If there is only one point or no points then we never do a parallel
        # launch
        # If we only have one point then we never do parallel launches
        elif all(ext <= 1 for ext in shape):
            return None
        # Check to see if we already did the math
        elif shape in self._launch_spaces:
            return self._launch_spaces[shape]
        # Prune out any dimensions that are 1
        temp_shape = ()
        temp_dims = ()
        volume = 1
        for dim in range(len(shape)):
            assert shape[dim] > 0
            if shape[dim] == 1:
                continue
            temp_shape = temp_shape + (shape[dim],)
            temp_dims = temp_dims + (dim,)
            volume *= shape[dim]
        # Figure out how many shards we can make with this array
        max_pieces = (
            volume + self._min_shard_volume - 1
        ) // self._min_shard_volume
        assert max_pieces > 0
        # If we can only make one piece return that now
        if max_pieces == 1:
            self._launch_spaces[shape] = None
            return None
        else:
            # TODO: a better heuristic here For now if we can make at least two
            # pieces then we will make N pieces
            max_pieces = self._num_pieces
        # Otherwise we need to compute it ourselves
        # First compute the N-th root of the number of pieces
        dims = len(temp_shape)
        temp_result = ()
        if dims == 0:
            # Project back onto the original number of dimensions
            result = ()
            for dim in range(len(shape)):
                result = result + (1,)
            return result
        elif dims == 1:
            # Easy case for one dimensional things
            temp_result = (min(temp_shape[0], max_pieces),)
        elif dims == 2:
            if volume < max_pieces:
                # TBD: Once the max_pieces heuristic is fixed, this should
                # never happen
                temp_result = temp_shape
            else:
                # Two dimensional so we can use square root to try and generate
                # as square a pieces as possible since most often we will be
                # doing matrix operations with these
                nx = temp_shape[0]
                ny = temp_shape[1]
                swap = nx > ny
                if swap:
                    temp = nx
                    nx = ny
                    ny = temp
                n = math.sqrt(float(max_pieces * nx) / float(ny))
                # Need to constraint n to be an integer with numpcs % n == 0
                # try rounding n both up and down
                n1 = int(math.floor(n + 1e-12))
                n1 = max(n1, 1)
                while max_pieces % n1 != 0:
                    n1 -= 1
                n2 = int(math.ceil(n - 1e-12))
                while max_pieces % n2 != 0:
                    n2 += 1
                # pick whichever of n1 and n2 gives blocks closest to square
                # i.e. gives the shortest long side
                side1 = max(nx // n1, ny // (max_pieces // n1))
                side2 = max(nx // n2, ny // (max_pieces // n2))
                px = n1 if side1 <= side2 else n2
                py = max_pieces // px
                # we need to trim launch space if it is larger than the
                # original shape in one of the dimensions (can happen in
                # testing)
                if swap:
                    temp_result = (
                        min(py, temp_shape[0]),
                        min(px, temp_shape[1]),
                    )
                else:
                    temp_result = (
                        min(px, temp_shape[0]),
                        min(py, temp_shape[1]),
                    )
        else:
            # For higher dimensions we care less about "square"-ness
            # and more about evenly dividing things, compute the prime
            # factors for our number of pieces and then round-robin
            # them onto the shape, with the goal being to keep the
            # last dimension >= 32 for good memory performance on the GPU
            temp_result = list()
            for dim in range(dims):
                temp_result.append(1)
            factor_prod = 1
            for factor in self._piece_factors:
                # Avoid exceeding the maximum number of pieces
                if factor * factor_prod > max_pieces:
                    break
                factor_prod *= factor
                remaining = tuple(
                    map(lambda s, r: (s + r - 1) // r, temp_shape, temp_result)
                )
                big_dim = remaining.index(max(remaining))
                if big_dim < len(temp_dims) - 1:
                    # Not the last dimension, so do it
                    temp_result[big_dim] *= factor
                else:
                    # Last dim so see if it still bigger than 32
                    if (
                        len(remaining) == 1
                        or remaining[big_dim] // factor >= 32
                    ):
                        # go ahead and do it
                        temp_result[big_dim] *= factor
                    else:
                        # Won't be see if we can do it with one of the other
                        # dimensions
                        big_dim = remaining.index(
                            max(remaining[0 : len(remaining) - 1])
                        )
                        if remaining[big_dim] // factor > 0:
                            temp_result[big_dim] *= factor
                        else:
                            # Fine just do it on the last dimension
                            temp_result[len(temp_dims) - 1] *= factor
        # Project back onto the original number of dimensions
        assert len(temp_result) == dims
        result = ()
        for dim in range(len(shape)):
            if dim in temp_dims:
                result = result + (temp_result[temp_dims.index(dim)],)
            else:
                result = result + (1,)
        result = Shape(result)
        # Save the result for later
        self._launch_spaces[shape] = result
        return result

    def compute_tile_shape(self, shape, launch_space):
        assert len(shape) == len(launch_space)
        # Over approximate the tiles so that the ends might be small
        return Shape(
            tuple(map(lambda x, y: (x + y - 1) // y, shape, launch_space))
        )

    def use_complete_tiling(self, shape, tile_shape):
        # If it would generate a very large number of elements then
        # we'll apply a heuristic for now and not actually tile it
        # TODO: A better heurisitc for this in the future
        num_tiles = (shape // tile_shape).volume()
        return not (num_tiles > 256 and num_tiles > 16 * self._num_pieces)

    def find_partition(self, index_space, functor):
        key = (index_space, functor)
        return self._index_partitions.get(key)

    def record_partition(self, index_space, functor, index_partition):
        key = (index_space, functor)
        assert key not in self._index_partitions
        self._index_partitions[key] = index_partition


class FusionChecker(object):
    def __init__(self, ops, contexts, runtime):
        """
        This is a class containing a list of constraints for fusing ops
        It emits whether or not a given list of ops can be fused
        """
        self.constraints = []
        self.ops = ops
        self.contexts = contexts
        self.runtime=runtime
        self.partitioners = []
        self.strategies = []

    def register_constraint(self, fusion_constraint_rule):
        self.constraints.append(fusion_constraint_rule)

    def supress_small_fusions(self, intervals, threshold):
        #find if there's a fusable sub window of length
        #greater than or equal to fusion_thresh
        final_set = []
        fusable=False
        for interval in intervals:
            if interval[1] - interval[0]  >=threshold:
                final_set.append(interval)
                fusable = True
            else:
                for i in range(interval[0], interval[1]):
                    final_set.append((i, i+1))
        return fusable, final_set

    def can_fuse(self):
        partitions = [] 
        """
        for op in self.ops:
            for output, part in zip(op._outputs, op._output_parts):
                output.reset_key_partition()
                for input in op._inputs:
                    input.reset_key_partition()
        """

        for op in self.ops:
            must_be_single = any(len(gop.scalar_outputs) > 0 for gop in [op])
            partitioner = Partitioner(self.runtime, [op], must_be_single=must_be_single)
            self.partitioners.append( partitioner )
            strategy = partitioner.partition_stores()
            self.strategies.append(strategy)

        windows = [(0, len(self.ops))]
        for constraint in self.constraints:
            windows = constraint.apply(self.contexts, self.runtime, self.ops, windows, self.partitioners, self.strategies)
        
        #print("windows", windows)
        fusable,final_set = self.supress_small_fusions(windows, self.runtime._fusion_threshold)
        """
        allKeyed=True
        for op in reversed(self.ops):
            for output, part, in zip(op._outputs, op._output_parts):
                if output._key_partition==None:
                    allKeyed=False
            for input in op._inputs:
                if input._key_partition==None:
                    allKeyed=False
        if allKeyed:
            fusable,final_set = self.supress_small_fusions(windows, self.runtime._fusion_threshold)
            return fusable, final_set, self.strategies
        """ 
        ist=0
        return fusable, final_set, self.strategies
        """
            for op in reversed(self.ops[ window[0]:window[1] ]):
                strategy = old_strategies[ist]
                local_partitions =  []
                for output, part, in zip(op._outputs, op._output_parts):
                    partition = strategy.get_partition(part)
                    local_partitions.append(partition)
                ist+=1
                midpoint = int(len(local_partitions)/2)
                partition = local_partitions[midpoint]
                print("selected", partition)
                for output, part, in zip(op._outputs, op._output_parts):
                    #output.set_key_partition(partition)
                    strategy._strategy[part] = partition
                    key_part = partition
                    #check if input and output should be aligned
                    for input, ipart in zip(op._inputs, op._input_parts):
                        if input.shape== output.shape:
                            #input.set_key_partition(key_part)
                            strategy._strategy[ipart] = partition
                #self.strategies.append(strategy)
        #self.strategies.reverse()
        """
        """
        for op in reversed(self.ops):
            must_be_single = any(len(gop.scalar_outputs) > 0 for gop in [op])
            partitioner = Partitioner(self.runtime, [op], must_be_single=must_be_single)
            self.partitioners.append( partitioner )
            strategy = partitioner.partition_stores()
            for output, part, in zip(op._outputs, op._output_parts):
                partition = strategy.get_partition(part)
                #partition = partitions[15]
                output.set_key_partition(partition)
                key_part = partition
                #check if input and output should be aligned
                for input in op._inputs:
                    if input.shape== output.shape:
                    #if op.has_constraint(input, output):
                        input.set_key_partition(key_part)
        #self.strategies.reverse()
        """


class FusionConstraint(object):
    def apply(self, contexts, runtime, ops, baseIntervals, partitioners, strategies):
        """"
         Abstract class for determining a rule that constrains
         which legate operations can be fused
         """
        raise NotImplementedError("Implement in derived classes")

class cuNumericContextExists(FusionConstraint):
    """
    Fusion currently exists as a cuNumeric operation
    This can be removed once fusion becomes a core task
    """
    def apply(self, contexts, runtime, ops, baseIntervals, partitioners, strategies):
        if "cunumeric" in contexts:
            return baseIntervals
        else:
           return [(i, i+1) for i in range(len(ops))]


class AllValidOps(FusionConstraint):
    """
    Class for only fusing only potentially fusable ops.
    This class performs the first pass of legality filtering
    """
    def __init__(self):
        self.validIDs = set()
        self.terminals = set()
        self.validIDs.add(2) #Binary op
        self.validIDs.add(10) #Fill op
        self.validIDs.add(21) #Unary op

    def apply(self, contexts, runtime, ops, baseIntervals, partitioners, strategies):
        fusable_intervals = []
        results = [int(op._task_id) in self.validIDs for op in ops]
        for baseInterval in baseIntervals:
            start, end = baseInterval[0], baseInterval[0]
            while end<baseInterval[1]:
                result = results[end]
                if result and (ops[end]._task_id not in self.terminals):
                    end=end+1
                else:
                    if start<end:
                        if ops[end]._task_id in self.terminals:
                            fusable_intervals.append((start,end+1))
                            start=end+1
                            end=start
                        else:
                            fusable_intervals.append((start,end))
                            start=end 
                            end=start
                    else:
                        fusable_intervals.append((start, start+1))
                        start=start+1
                        end = start
            if start<end:
                fusable_intervals.append((start,end))
        return fusable_intervals


class IdenticalProjection(FusionConstraint):
    """Fusion rule that only ops with identical
       projection functors can be fused"""

    def apply(self, contexts, runtime, ops, baseIntervals,  partitioners, strategies):
        store_to_ops = {}
        
        intervals = []
        i=0
        for baseInterval in baseIntervals:
            start, end = baseInterval
            i=start
            while i<end:
                op=ops[i]
                bufferSet = {}
                # find the set union of input and output buffers for the op
                for input, part in zip(op._inputs, op._input_parts):
                    if input not in bufferSet:
                        proj = strategies[i].get_projection(part)
                        if hasattr(proj, 'part'):
                            bufferSet[input]=proj
                for output, part in zip(op._outputs, op._output_parts):
                    if output not in bufferSet:
                        proj = strategies[i].get_projection(part)
                        if hasattr(proj, 'part'):
                            bufferSet[output]=proj
                if i==0: #we only iterate from i==1 onwards
                    i+=1
                    continue
                # for each op in the union, record its associated transform
                for buffer in bufferSet.keys():
                    proj = bufferSet[buffer]
                    matrix = proj.part.index_partition.functor.transform.trans
                    if buffer not in store_to_ops:
                        store_to_ops[buffer] = matrix
                    else: #we see a new projection for the same buffer
                        if not np.array_equal(matrix, store_to_ops[buffer]):
                            intervals.append((start, i))
                            start=i
                            i=start+1
                            store_to_ops={}
                            continue
                i+=1
            if start<end:
                intervals.append((start,end))
        return intervals


class IdenticalLaunchShapes(FusionConstraint):
    """Fusion rule that only ops with identical
       launch shapes can be fused"""

    def apply(self, contexts, runtime, ops, baseIntervals, partitioners, strategies):
        launch_shapes = []
        for i in range(len(ops)):
            launch_shapes.append(strategies[i]._launch_shape)
        intervals =[]
        for baseInterval in baseIntervals:
            start=baseInterval[0]
            i=start+1
            end = baseInterval[1]
            while i<end:
                leftNone = launch_shapes[i] is None and (launch_shapes[i-1] is not None)
                rightNone = launch_shapes[i-1] is None and (launch_shapes[i] is not None)
                if leftNone or rightNone or launch_shapes[i]!=launch_shapes[i-1]:
                    intervals.append((start, i))
                    start=i
                    i=start+1
                else:
                    i+=1
            if start<end:
                intervals.append((start, end))
        return intervals


class ValidProducerConsumer(FusionConstraint):
    """In a fused op, there cannot be a producer consumer
        relationship between different views of the same buffers"""

    def apply(self, contexts, runtime, ops, baseIntervals,  partitioners, strategies):
        childMap = {}
        intervals = []
        i, start=0, 0
        end = len(ops)
        def getRoot(store):
            while store._parent:
                store = store._parent
            return store
        for baseInterval in baseIntervals:
            start, end = baseInterval 
            while i<end:
                op = ops[i] 
                #check consumers view of root array
                #is the same as the producers view
                isSame = True
                for input in op._inputs:
                    inputRoot = getRoot(input) 
                    if inputRoot in childMap:
                        isSame = isSame and childMap[inputRoot] == input

                if not isSame:                     
                    intervals.append((start, i))
                    start=i
                    i=start+1
                    childMap = {}
                else:
                    i=i+1 
                #register producers view of buffer
                for output in op._outputs:
                    outputRoot = getRoot(output)
                    if outputRoot not in childMap:  
                        childMap[outputRoot] = output
            #end while
            if start<end:
                intervals.append((start,end))
        return intervals
 


   
class Runtime(object):
    def __init__(self, core_library):
        """
        This is a class that implements the Legate runtime.
        The Runtime object provides high-level APIs for Legate libraries
        to use services in the Legion runtime. The Runtime centralizes
	resource management for all the libraries so that they can
	       focus on implementing their domain logic.
        """

        try:
            self._legion_context = top_level.context[0]
        except AttributeError:
            pass

        # Record whether we need to run finalize tasks
        # Key off whether we are being loaded in a context or not
        try:
            # Do this first to detect if we're not in the top-level task
            self._legion_context = top_level.context[0]
            self._legion_runtime = legion.legion_runtime_get_runtime()
            legate_task_preamble(self._legion_runtime, self._legion_context)
            self._finalize_tasks = True
        except AttributeError:
            self._legion_runtime = None
            self._legion_context = None
            self._finalize_tasks = False

        # Initialize context lists for library registration
        self._contexts = {}
        self._context_list = []

        # Register the core library now as we need it for the rest of
        # the runtime initialization
        self.register_library(core_library)
        self._core_context = self._context_list[0]
        self._core_library = core_library

        # This list maintains outstanding operations from all legate libraries
        # to be dispatched. This list allows cross library introspection for
        # Legate operations.
        self._outstanding_ops = []

        self._window_size = self._core_context.get_tunable(
            legion.LEGATE_CORE_TUNABLE_WINDOW_SIZE,
            ty.uint32,)
        self._window_size=50
        self._fusion_threshold =2
        #used for logging window and fusion lengths
        self._clearing_pipe = False

        # Now we initialize managers
        self._attachment_manager = AttachmentManager(self)
        self._partition_manager = PartitionManager(self)
        self.index_spaces = {}  # map shapes to index spaces
        self.region_managers = {}  # map from shape to region managers
        self.field_managers = {}  # map from (shape,dtype) to field managers

        self.destroyed = False
        self.max_field_reuse_size = self._core_context.get_tunable(
            legion.LEGATE_CORE_TUNABLE_FIELD_REUSE_SIZE,
            ty.uint64,
        )
        self.max_field_reuse_frequency = self._core_context.get_tunable(
            legion.LEGATE_CORE_TUNABLE_FIELD_REUSE_FREQUENCY,
            ty.uint32,
        )
        self._empty_argmap = legion.legion_argument_map_create()

        # A projection functor and its corresponding sharding functor
        # have the same local id
        self._next_projection_id = (
            core_library._lib.LEGATE_CORE_FIRST_DYNAMIC_FUNCTOR_ID
        )
        self._next_sharding_id = (
            core_library._lib.LEGATE_CORE_FIRST_DYNAMIC_FUNCTOR_ID
        )
        self._registered_projections = {}
        self._registered_shardings = {}

    @property
    def legion_runtime(self):
        if self._legion_runtime is None:
            self._legion_runtime = legion.legion_runtime_get_runtime()
        return self._legion_runtime

    @property
    def legion_context(self):
        return self._legion_context

    @property
    def core_context(self):
        return self._core_context

    @property
    def core_library(self):
        return self._core_library._lib

    @property
    def empty_argmap(self):
        return self._empty_argmap

    @property
    def attachment_manager(self):
        return self._attachment_manager

    @property
    def partition_manager(self):
        return self._partition_manager

    def register_library(self, library):
        libname = library.get_name()
        if libname in self._contexts:
            raise RuntimeError(
                f"library {libname} has already been registered!"
            )
        # It's important that we load the library so that its constants
        # can be used for configuration.
        self.load_library(library)
        context = Context(self, library)
        self._contexts[libname] = context
        self._context_list.append(context)
        return context

    @staticmethod
    def load_library(library):
        shared_lib_path = library.get_shared_library()
        if shared_lib_path is not None:
            header = library.get_c_header()
            if header is not None:
                ffi.cdef(header)
            shared_lib = ffi.dlopen(shared_lib_path)
            library.initialize(shared_lib)
            callback_name = library.get_registration_callback()
            callback = getattr(shared_lib, callback_name)
            callback()
        else:
            library.initialize()

    def destroy(self):
        # Before we clean up the runtime, we should execute all outstanding
        # operations.
        self.flush_scheduling_window()
        # Destroy all libraries. Note that we should do this
        # from the lastly added one to the first one
        for context in reversed(self._context_list):
            context.destroy()
        del self._contexts
        del self._context_list

        self._attachment_manager.destroy()

        # Remove references to our legion resources so they can be collected
        self.region_managers = None
        self.field_managers = None
        self.index_spaces = None

        if self._finalize_tasks:
            # Run a gc and then end the legate task
            gc.collect()
            legate_task_postamble(self.legion_runtime, self.legion_context)

        self.destroyed = True

    def dispatch(self, op, redop=None):
        self._attachment_manager.perform_detachments()
        self._attachment_manager.prune_detachments()
        if redop:
            return op.launch(self.legion_runtime, self.legion_context, redop)
        else:
            return op.launch(self.legion_runtime, self.legion_context)


    def serialize_multiop_metadata(self, numpy_context, ops):
        """creates a 'header' for a fused op that denotes metadata
        on each ops inputs, outputs, reductions and scalars
        """
        #generate offset maps for all inputs to serialize metadata
        input_starts, output_starts, offset_starts, offsets= [],[],[],[]
        reduction_starts, scalar_starts, future_starts, op_ids = [], [], [], []
        input_start, output_start, offset_start = 0,0,0
        reduction_start, scalar_start, future_start = 0,0,0
 
        for op in ops:
            input_starts.append(input_start)
            output_starts.append(output_start)
            offset_starts.append(offset_start)
            reduction_starts.append(reduction_start)
            scalar_starts.append(scalar_start)
            future_starts.append(future_start)

            for i,input in enumerate(op._inputs):
                offsets.append(i+1)
                if input.kind is Future:
                    future_start+=1
            for o,output in enumerate(op._outputs):
                offsets.append(-(o+1)) 
            for r,reduction in enumerate(op._reductions):
                offsets.append(-(r+1)) 
 
            #todo: this will have to be generalized to work with any library
            op_ids.append(numpy_context.get_task_id(op._task_id._value_))
            offset_start+=(len(op._inputs)+len(op._outputs))
            input_start+=len(op._inputs)
            output_start+=len(op._outputs)
            reduction_start+=len(op._reductions)
            scalar_start+=len(op._scalar_args)

        #terminators
        input_starts.append(input_start)
        output_starts.append(output_start)
        offset_starts.append(offset_start)
        reduction_starts.append(reduction_start)
        scalar_starts.append(scalar_start)
        future_starts.append(future_start)

        meta_arrs =  (input_starts, output_starts, offset_starts, offsets, reduction_starts,  scalar_starts, 
                      future_starts, op_ids)
        fusion_metadata = FusionMetadata(*meta_arrs)
        meta_maps=None
        return fusion_metadata
   

    def build_fused_op(self,ops):

        fusion_checker = FusionChecker(ops, self._contexts, self)
        #starttime = datetime.datetime.now()
        #fusion_checker.register_constraint(cuNumericContextExists())
        fusion_checker.register_constraint(AllValidOps())
        fusion_checker.register_constraint(IdenticalLaunchShapes())
        fusion_checker.register_constraint(IdenticalProjection())
        fusion_checker.register_constraint(ValidProducerConsumer())
        can_fuse,fusable_sets, partitions = fusion_checker.can_fuse()
                
        super_strategies = []
        z=0
        """
        for fusable_set in fusable_sets:   
            #create super strategy for this fusable set
            super_strat = {}
            super_fspace = {}
            super_keystore = set()
            start,end = fusable_set
            for j in range(start,end):
                super_strat = {**(super_strat.copy()), **partitions[j]._strategy}
                super_fspace = {**(super_fspace.copy()), **partitions[j]._fspaces}
                super_keystore = super_keystore.union(partitions[j]._key_parts)
            super_strategies.append(Strategy(partitions[start]._launch_shape, super_strat, super_fspace, super_keystore))
        """
        #stop = datetime.datetime.now()
        #delta=stop-starttime
        #total = delta.total_seconds() * 1000.0
        #print("time", total)

        #starttime = datetime.datetime.now()
       
        #once fusion in the core is playing nicely with the mapepr
        #the following two lines will be removed, and be replaced 
        #with the 2 subsequent (commented out) lines
        fused_id = self._contexts["cunumeric"].fused_id
        numpy_context = self._contexts["cunumeric"]
        #fused_id = self._contexts["legate.core"]._library.fused_id
        #numpy_context = self._contexts["legate.core"]

        opID=0
        new_op_list = []
        for i,fusable_set in enumerate(fusable_sets):
            start, end = fusable_set
            op_subset = ops[start:end]
            #if nothing to fuse, just use the original op
            if end-start==0: 
                continue
            if end-start==1:
                normal_op = ops[start]
                normal_op.strategy =  partitions[opID] #._strategy#uper_strategies[i]
                new_op_list.append(normal_op)
                opID+=1
                #print("\nmaking single", normal_op._task_id)
            elif end-start > 1:
                #initialize fused task
                fused_task = numpy_context.create_task(fused_id)
                #serialize necessary metadata on all encapsulated ops 
                fusion_metadata = self.serialize_multiop_metadata(numpy_context, op_subset)
                fused_task.add_fusion_metadata(fusion_metadata) #sets fused_task._is_fused to true

                #add typical inputs and outputs of all subtasks to fused task
                key_part = None
                fused_task._unfused_input_parts = []
                fused_task._unfused_output_parts = []
                fused_task._unfused_reduction_parts = []
                #print("\nmaking", fused_task._task_id)
                for j,op in enumerate(op_subset):
                    #print("sub optask_id",j,op._task_id)
                    currStrategy = partitions[j]
                    for scalar in op._scalar_args:
                        fused_task.add_scalar_arg(scalar[0], ty.int32)
                    for (reduction, redop), part in zip(op._reductions, op._reduction_parts):
                        fused_task.add_reduction(reduction, redop)
                        #fused_task._unfused_reduction_parts.append(part)
                    for input,part in zip(op._inputs, op._input_parts):
                        fused_task.add_input(input)   
                        #fused_task._unfused_input_parts.append(part)
                    for output,part in zip(op._outputs, op._output_parts):
                        fused_task.add_output(output) 
                        #fused_task._unfused_output_parts.append(part)
                    for future in op._futures:
                        fused_task.add_future(future)
                    for constraint in op._constraints:
                        if (isinstance(constraint, Alignment)):
                            fused_task.add_alignment(constraint._lhs.store, constraint._rhs.store)
                    opID+=1
                new_op_list.append(fused_task)
                #print("nout", len(fused_task._outputs))
                #print("nin", len(fused_task._inputs))
                #print("ndedup", len(set(fused_task._inputs+fused_task._outputs)))
        #print("new op list", new_op_list)
        strats=[]
   
        redoPar=True
        for i,fused_task in enumerate(new_op_list):
            if redoPar and fused_task._is_fused:
                #print("redo partition for", fused_task._task_id)
                #print("output parts", fused_task._output_parts)
                must_be_single = any(len(gop.scalar_outputs) > 0 for gop in [fused_task])
                partitioner = Partitioner(self, [fused_task], must_be_single=must_be_single)
                strategy = partitioner.partition_stores()
                fused_task.strategy = strategy
                strats.append(strategy)
                #print(strategy)
                #for output, part in zip(fused_task._outputs, fused_task._output_parts):
                    #partition = strategy.get_partition(part)
                    #print(partition)
            else:
                strats.append(fused_task.strategy)
            #else:
            #    fused_task.strategy = super_strategies[i]
            #    strats.append( super_strategies[i])

        #stop = datetime.datetime.now()
        #delta=stop-starttime
        #total = delta.total_seconds() * 1000.0
        #print("time2", total)

        return new_op_list, strats       

    def _launch_outstanding(self, force_eval=True):        
        if len(self._outstanding_ops):
            ops = self._outstanding_ops
            self._outstanding_ops = []
            self._schedule(ops, force_eval)

    def _schedule(self, ops, force_eval=False):
        ids = [op._task_id for op in ops]
        #case 1: try fusing current window of tasks
        strats = False
        if len(ops)>=2 and (not force_eval):
            fused_task_list,strats = self.build_fused_op(ops)
            if fused_task_list:
                self._clearing_pipe = True
                for task in fused_task_list:
                    task.execute() 
                self._clearing_pipe = False

        # case 2: tasks  processed for fusion already have  
        # their strategy "baked in", as we already partitioned
        # them when testing fusion legality (in case 1)
        elif len(ops)==1 and self._clearing_pipe:
            strategy = ops[0].strategy
            #print(ops[0]._task_id)
            #print(strategy)
            ops[0].launch(strategy)

        # case 3: execute the ops normally 
        # partition if op wasn't checked for fusability
        else:
            if not strats: #ops were not check for fusability, so partition them
                strats = []
                for op in ops:
                    must_be_single = any(len(gop.scalar_outputs) > 0 for gop in [op])
                    partitioner = Partitioner(self, [op], must_be_single=must_be_single)
                    strategy = partitioner.partition_stores()
                    strats.append(strategy)
            for i,op in enumerate(ops):
                op.launch(strats[i])


    def submit(self, op):
        #always launch ops that've been processed for fusion
        #do not re-add to the window
        #as the these ops already waited in the window
        if self._clearing_pipe:
            self._schedule([op])
        else:
            self._outstanding_ops.append(op)
            if len(self._outstanding_ops) >= self._window_size:
                ops = self._outstanding_ops
                self._outstanding_ops = []
                self._schedule(ops)

    def _scheduleNew(self, ops):
        # TODO: For now we run the partitioner for each operation separately.
        #       We will eventually want to compute a trace-wide partitioning
        #       strategy.
        strategies = []
        for op in ops:
            must_be_single = len(op.scalar_outputs) > 0
            partitioner = Partitioner(
                self, [op], must_be_single=must_be_single
            )
            strategies.append(partitioner.partition_stores())

        for op, strategy in zip(ops, strategies):
            op.launch(strategy)

    def flush_scheduling_window(self):
        if len(self._outstanding_ops) == 0:
            return
        ops = self._outstanding_ops
        self._outstanding_ops = []
        self._schedule(ops)

    def submitNew(self, op):
        self._outstanding_ops.append(op)
        if len(self._outstanding_ops) >= self._window_size:
            self.flush_scheduling_window()

    def _progress_unordered_operations(self):
        legion.legion_context_progress_unordered_operations(
            self.legion_runtime, self.legion_context
        )

    def unmap_region(self, physical_region, unordered=False):
        physical_region.unmap(
            self.legion_runtime, self.legion_context, unordered=unordered
        )

    def get_delinearize_functor(self):
        return self.core_context.get_projection_id(
            self.core_library.LEGATE_CORE_DELINEARIZE_PROJ_ID
        )

    def get_projection(self, src_ndim, dims):
        spec = (src_ndim, dims)
        if spec in self._registered_projections:
            return self._registered_projections[spec]

        tgt_ndim = len(dims)
        dims_c = ffi.new(f"int32_t[{tgt_ndim}]")
        for idx, dim in enumerate(dims):
            dims_c[idx] = dim

        proj_id = self.core_context.get_projection_id(self._next_projection_id)
        self._next_projection_id += 1
        self._registered_projections[spec] = proj_id

        self.core_library.legate_register_projection_functor(
            src_ndim,
            tgt_ndim,
            dims_c,
            proj_id,
        )

        shard_id = self.core_context.get_projection_id(self._next_sharding_id)
        self._next_sharding_id += 1
        self._registered_shardings[spec] = shard_id

        self.core_library.legate_create_sharding_functor_using_projection(
            shard_id,
            proj_id,
        )

        return proj_id

    def get_transform_code(self, name):
        return getattr(
            self.core_library, f"LEGATE_CORE_TRANSFORM_{name.upper()}"
        )

    def create_future(self, data, size):
        future = Future()
        future.set_value(self.legion_runtime, data, size)
        return future

    def create_store(
        self,
        dtype,
        shape=None,
        storage=None,
        optimize_scalar=False,
    ):
        if shape is not None and not isinstance(shape, Shape):
            shape = Shape(shape)
        return Store(
            self,
            dtype,
            shape=shape,
            storage=storage,
            optimize_scalar=optimize_scalar,
        )

    def find_or_create_region_manager(self, shape, region=None):
        region_mgr = self.region_managers.get(shape)
        if shape not in self.region_managers:
            region_mgr = RegionManager(self, shape)
            self.region_managers[shape] = region_mgr
        return region_mgr

    def find_or_create_field_manager(self, shape, dtype):
        key = (shape, dtype)
        field_mgr = self.field_managers.get(key)
        if key not in self.field_managers:
            field_mgr = FieldManager(self, shape, dtype)
            self.field_managers[key] = field_mgr
        return field_mgr

    def allocate_field(self, shape, dtype):
        assert not self.destroyed
        region = None
        field_id = None
        field_mgr = self.find_or_create_field_manager(shape, dtype)
        region, field_id = field_mgr.allocate_field()
        field = Field(self, region, field_id, dtype, shape)
        return RegionField(self, region, field, shape)

    def free_field(self, region, field_id, dtype, shape):
        # Have a guard here to make sure that we don't try to
        # do this after we have been destroyed
        if self.destroyed:
            return
        # Now save it in our data structure for free fields eligible for reuse
        key = (shape, dtype)
        if self.field_managers is not None:
            self.field_managers[key].free_field(region, field_id)

    def import_output_region(self, out_region, field_id, dtype):
        region = out_region.get_region()
        shape = Shape(ispace=region.index_space)

        region_mgr = self.find_or_create_region_manager(shape)
        region_mgr.import_region(region)
        field = Field(
            self,
            region,
            field_id,
            dtype,
            shape,
            own=True,
        )

        self.find_or_create_field_manager(shape, dtype)

        return RegionField(self, region, field, shape)

    def create_output_region(self, fspace, fields):
        return OutputRegion(
            self.legion_context,
            self.legion_runtime,
            field_space=fspace,
            fields=fields,
        )

    def has_attachment(self, alloc):
        return self._attachment_manager.has_attachment(alloc)

    def find_or_create_index_space(self, bounds):
        if bounds in self.index_spaces:
            return self.index_spaces[bounds]
        # Haven't seen this before so make it now
        if isinstance(bounds, Rect):
            rect = bounds
        else:
            rect = Rect(bounds)
        handle = legion.legion_index_space_create_domain(
            self.legion_runtime, self.legion_context, rect.raw()
        )
        result = IndexSpace(
            self.legion_context, self.legion_runtime, handle=handle
        )
        # Save this for the future
        self.index_spaces[bounds] = result
        return result

    def create_field_space(self):
        return FieldSpace(self.legion_context, self.legion_runtime)

    def create_region(self, index_space, field_space):
        handle = legion.legion_logical_region_create(
            self.legion_runtime,
            self.legion_context,
            index_space.handle,
            field_space.handle,
            True,
        )
        return Region(
            self.legion_context,
            self.legion_runtime,
            index_space,
            field_space,
            handle,
        )

    def find_partition(self, index_space, functor):
        return self._partition_manager.find_partition(index_space, functor)

    def record_partition(self, index_space, functor, index_partition):
        self._partition_manager.record_partition(
            index_space, functor, index_partition
        )

    def extract_scalar(self, future, idx, launch_domain=None):
        launcher = TaskLauncher(
            self.core_context,
            self.core_library.LEGATE_CORE_EXTRACT_SCALAR_TASK_ID,
            tag=self.core_library.LEGATE_CPU_VARIANT,
        )
        launcher.add_future(future)
        launcher.add_scalar_arg(idx, ty.int32)
        if launch_domain is None:
            return launcher.execute_single()
        else:
            return launcher.execute(launch_domain)

    def reduce_future_map(self, future_map, redop):
        if isinstance(future_map, Future):
            return future_map
        else:
            return future_map.reduce(
                self.legion_context,
                self.legion_runtime,
                redop,
                mapper=self.core_context.get_mapper_id(0),
            )


_runtime = Runtime(CoreLib())


def _cleanup_legate_runtime():
    global _runtime
    _runtime._launch_outstanding()
    _runtime.destroy()
    del _runtime
    gc.collect()


cleanup_items.append(_cleanup_legate_runtime)


def get_legion_runtime():
    return _runtime.legion_runtime


def get_legion_context():
    return _runtime.legion_context


def legate_add_library(library):
    _runtime.register_library(library)


def get_legate_runtime():
    return _runtime
