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

import legate.core.types as ty

from .launcher import CopyLauncher, TaskLauncher
from .solver import EqClass


class Operation(object):
    def __init__(self, context, mapper_id=0):
        self._context = context
        self._mapper_id = mapper_id
        self._no_accesses = []
        self._inputs = []
        self._outputs = []
        self._reductions = []
        self._scalar_output = None
        self._scalar_reduction = None
        self._constraints = EqClass()
        self._broadcasts = set()

    @property
    def context(self):
        return self._context

    @property
    def mapper_id(self):
        return self._mapper_id

    @property
    def no_accesses(self):
        return self._no_accesses

    @property
    def inputs(self):
        return self._inputs

    @property
    def outputs(self):
        return self._outputs

    @property
    def reductions(self):
        return self._reductions

    @property
    def constraints(self):
        return self._constraints

    @property
    def broadcasts(self):
        return self._broadcasts

    def get_all_stores(self):
        stores = (
            set(self._no_accesses)
            | set(self._inputs)
            | set(self._outputs)
            | set(store for (store, _) in self._reductions)
        )
        return stores

    def add_no_access(self, store):
        self._no_accesses.append(store)

    def add_input(self, store):
        self._inputs.append(store)

    @property
    def _has_scalar_output(self):
        return (
            self._scalar_reduction is not None
            or self._scalar_output is not None
        )

    def _check_scalar_output(self):
        if self._has_scalar_output:
            raise ValueError("Only one scalar store can be used for output")

    def add_output(self, store):
        if store.scalar:
            self._check_scalar_output()
            self._scalar_output = store
        else:
            self._outputs.append(store)

    def add_reduction(self, store, redop):
        if store.scalar:
            self._check_scalar_output()
            self._scalar_reduction = (store, redop)
        else:
            self._reductions.append((store, redop))

    def add_alignment(self, store1, store2):
        if store1.shape != store2.shape:
            print(store1.shape)
            print(store2.shape)
            raise ValueError(
                "Stores must have the same shape to be aligned, "
                f"but got {store1.shape} and {store2.shape}"
            )
        self._constraints.record(store1, store2)

    def add_broadcast(self, store):
        self._broadcasts.add(store)

    def execute(self):
        self._context.runtime.submit(self)


class Task(Operation):
    def __init__(self, context, task_id, mapper_id=0):
        Operation.__init__(self, context, mapper_id)
        self._task_id = task_id
        self._scalar_args = []
        self._futures = []

    def add_scalar_arg(self, value, dtype):
        self._scalar_args.append((value, dtype))

    def add_dtype_arg(self, dtype):
        code = self._context.type_system[dtype].code
        self._scalar_args.append((code, ty.int32))

    def add_future(self, future):
        self._futures.append(future)

    def launch(self, strategy):
        launcher = TaskLauncher(self.context, self._task_id, self.mapper_id)

        for no_access in self._no_accesses:
            launcher.add_no_access(no_access, strategy[no_access])
        for input in self._inputs:
            launcher.add_input(input, strategy[input])
        for output in self._outputs:
            if not output.unbound:
                launcher.add_output(output, strategy[output])
        for (reduction, redop) in self._reductions:
            partition = strategy[reduction]
            partition.redop = redop
            launcher.add_reduction(reduction, partition)
        for output in self._outputs:
            if not output.unbound:
                continue
            fspace = strategy.get_field_space(output)
            field_id = fspace.allocate_field(output.type)
            launcher.add_unbound_output(output, fspace, field_id)

        for (arg, dtype) in self._scalar_args:
            launcher.add_scalar_arg(arg, dtype)

        for future in self._futures:
            launcher.add_future(future)

        if self._scalar_output is not None:
            strategy.launch(launcher, self._scalar_output)
        elif self._scalar_reduction is not None:
            (store, redop) = self._scalar_reduction
            strategy.launch(launcher, store, redop)
        else:
            strategy.launch(launcher)


class Copy(Operation):
    def __init__(self, context, mapper_id=0):
        Operation.__init__(self, context, mapper_id)
        self._source_indirects = []
        self._target_indirects = []

    @property
    def inputs(self):
        return (
            super(Copy, self).inputs
            + self._source_indirects
            + self._target_indirects
        )

    def add_source_indirect(self, store):
        self._source_indirects.append(store)

    def add_target_indirect(self, store):
        self._target_indirects.append(store)

    def launch(self, strategy):
        launcher = CopyLauncher(self.context, self.mapper_id)

        assert len(self._no_accesses) == 0
        assert len(self._inputs) == len(self._outputs) or len(
            self._inputs
        ) == len(self._reductions)
        assert len(self._source_indirects) == 0 or len(
            self._source_indirects
        ) == len(self._inputs)
        assert len(self._target_indirects) == 0 or len(
            self._target_indirects
        ) == len(self._outputs)

        for input in self._inputs:
            launcher.add_input(input, strategy[input])
        for output in self._outputs:
            assert not output.unbound
            launcher.add_output(output, strategy[output])
        for indirect in self._source_indirects:
            launcher.add_source_indirect(indirect, strategy[indirect])
        for indirect in self._target_indirects:
            launcher.add_target_indirect(indirect, strategy[indirect])

        for (reduction, redop) in self._reductions:
            partition = strategy[reduction]
            partition.redop = redop
            launcher.add_reduction(reduction, partition)

        strategy.launch(launcher)