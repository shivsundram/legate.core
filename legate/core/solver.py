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

from .legion import Rect
from .partition import NoPartition
from .shape import Shape


class EqClass(object):
    def __init__(self):
        # Maps a store to the equivalent class id
        self._class_ids = {}
        self._next_class_id = 0
        # Maps an equivalent class id to the class
        self._classes = {}

    @property
    def empty(self):
        return self._next_class_id == 0

    def _add(self, store1, store2):
        cls = set([store1, store2])
        cls_id = self._next_class_id
        self._next_class_id + 1
        self._classes[cls_id] = cls
        self._class_ids[store1] = cls_id
        self._class_ids[store2] = cls_id

    def _update(self, store1, store2):
        cls_id = self._class_ids[store1]
        cls = self._classes[cls_id]
        cls.add(store2)
        self._class_ids[store2] = cls_id

    def _merge(self, store1, store2):
        cls_id1 = self._class_ids[store1]
        cls_id2 = self._class_ids[store2]
        cls = self._classes[cls_id1] | self._classes[cls_id2]
        self._classes[cls_id1] = cls
        self._classes[cls_id2] = cls

    def record(self, store1, store2):
        """
        Record an equivalence relation between two stores
        """
        found1 = store1 in self._class_ids
        found2 = store2 in self._class_ids

        if not found1 and not found2:
            self._add(store1, store2)
        elif found1:
            self._update(store1, store2)
        elif found2:
            self._update(store2, store1)
        else:
            self._merge(store1, store2)

    def copy(self):
        new = EqClass()
        new._class_ids = self._class_ids.copy()
        new._classes = self._classes.copy()
        return new

    def union(self, other):
        if self.empty:
            self._class_ids = other._class_ids.copy()
            self._classes = other._classes.copy()
        else:
            for other_class in other._classes.values():
                cls = other_class.copy()
                store1 = cls.pop()
                for store2 in cls:
                    self.record(store1, store2)

    def find(self, store):
        """
        Return an equivalence class for a given store.
        """
        if store not in self._class_ids:
            return set([store])
        else:
            return self._classes[self._class_ids[store]]


class Strategy(object):
    def __init__(self, launch_shape, strategy, fspaces):
        self._launch_shape = launch_shape
        self._strategy = strategy
        self._fspaces = fspaces

    def __getitem__(self, store):
        assert not store.unbound
        if store not in self._strategy:
            raise ValueError(f"No strategy is found for {store}")
        return self._strategy[store].get_requirement(self._launch_shape, store)

    def get_field_space(self, store):
        assert store.unbound
        if store not in self._fspaces:
            raise ValueError(f"No strategy is found for {store}")
        return self._fspaces[store]

    def launch(self, launcher, output=None, redop=None):
        if output is None:
            if self._launch_shape is None:
                launcher.execute_single()
            else:
                launcher.execute(Rect(self._launch_shape))
        else:
            if self._launch_shape is None:
                result = launcher.execute_single()
            else:
                assert redop is not None
                result = launcher.execute(Rect(self._launch_shape), redop)
            output.set_storage(result)


class Partitioner(object):
    def __init__(self, runtime, ops, must_be_single=False):
        self._runtime = runtime
        self._ops = ops
        self._must_be_single = must_be_single

    def partition_stores(self):
        stores = set()
        constraints = EqClass()
        broadcasts = set()
        for op in self._ops:
            stores.update(op.get_all_stores())
            constraints.union(op.constraints)
            broadcasts.update(op.broadcasts)

        if self._must_be_single or len(stores) == 0:
            partitions = {}
            fspaces = {}
            for store in stores:
                partitions[store] = NoPartition()
                if store.unbound:
                    cls = constraints.find(store)
                    assert all(to_align.unbound for to_align in cls)
                    fspace = self._runtime.create_field_space()
                    for to_align in cls:
                        fspaces[to_align] = fspace
            return Strategy(None, partitions, fspaces)

        must_be_1d_launch = any(store.unbound for store in stores)

        partitions = {}
        fspaces = {}
        prev_part = None
        while len(stores) > 0:
            store = stores.pop()
            if store.scalar or store in broadcasts:
                partitions[store] = NoPartition()
                continue
            elif store.unbound:
                cls = constraints.find(store)
                assert all(to_align.unbound for to_align in cls)
                fspace = self._runtime.create_field_space()
                for to_align in cls:
                    partitions[to_align] = NoPartition()
                    fspaces[to_align] = fspace
                continue

            if isinstance(prev_part, NoPartition):
                partition = prev_part
            else:
                partition = store.find_key_partition()

            cls = constraints.find(store)
            for to_align in cls:
                if to_align.scalar:
                    partitions[to_align] = NoPartition()
                else:
                    partitions[to_align] = partition
            stores = stores - cls
            prev_part = partition

        color_shape = None if prev_part is None else prev_part.color_shape

        if must_be_1d_launch and color_shape is not None:
            color_shape = Shape((color_shape.volume(),))

        return Strategy(color_shape, partitions, fspaces)