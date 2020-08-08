# -*- coding: utf-8 -*-
#  Copyright (c) 2019 - now, Eggroll Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
import threading
from collections import defaultdict

from eggroll.core.pair_store.format import ArrayByteBuffer, PairBinReader
from eggroll.core.transfer.transfer_service import TransferService
from eggroll.roll_pair import create_adapter
from eggroll.utils.log_utils import get_logger

L = get_logger()
FINISH_STATUS = "finish_partition"


class BatchStreamStatus:
    _recorder = {}
    _recorder_lock = threading.Lock()

    def __init__(self, tag):
        self._tag = tag
        self._stage = "doing"
        self.total_batches = -1
        self.content_type = "unknown"
        self.counter = defaultdict(int)
        self._condition = threading.Condition()
        with self._recorder_lock:
            self._recorder[self._tag] = self

    def _debug_string(self):
        return f"BatchStreams end normally, tag={self._tag} " \
               f"total_batches={self.total_batches}:total_elems={sum(self.counter.values())}"

    def set_finish(self, total_batches):
        if self.total_batches != len(self.counter):
            L.debug(f"MarkEnd BatchStream ahead of all BatchStreams received, {self._debug_string()}")
        else:
            self._stage = "done"
            self.total_batches = total_batches
            self._condition.notify_all()
            L.trace(f"All BatchStreams finish normally, {self._debug_string()}")

    def count_batch(self, batch_seq_id, batch_pairs):
        self.counter[batch_seq_id] = batch_pairs
        if self._stage == "done" and self.total_batches == len(self.counter):
            self._condition.notify_all()
            L.debug(f"All BatchStreams finish out-of-order, {self._debug_string()}")

    @classmethod
    def get_or_create(cls, tag):
        with cls._recorder_lock:
            if tag not in cls._recorder:
                bss = BatchStreamStatus(tag)
            else:
                bss = cls._recorder[tag]
        return bss

    @classmethod
    def wait_finish(cls, tag, timeout):
        bss = cls.get_or_create(tag)
        bss._condition.wait(timeout)
        finished = bss._stage == "done" and bss.total_batches == len(bss.counter)
        if finished:
            del cls._recorder[tag]
        return finished, bss.total_batches, bss.counter, bss.content_type


class PutBatchTask:
    def __init__(self, tag, partition):
        self.partition = partition
        self.tag = tag

    """
    transfer a total roll_pair by several batch streams
    """
    # tag -> seq -> count

    _put_batch_lock = threading.Lock()

    def run(self):
        # batch stream must be executed serially, and reinit.
        with self._put_batch_lock:
            L.trace(f"do_store start for tag={self.tag}")
            bss = BatchStreamStatus.get_or_create(self.tag)
            try:
                broker = TransferService.get_or_create_broker(self.tag, write_signals=1)
                with create_adapter(self.partition) as db, db.new_batch() as wb:
                    for batch in broker:
                        batch_pairs = 0
                        bin_data = ArrayByteBuffer(batch.data)
                        reader = PairBinReader(pair_buffer=bin_data, data=batch.data)
                        for k_bytes, v_bytes in reader.read_all():
                            wb.put(k_bytes, v_bytes)
                            batch_pairs += 1
                        bss.count_batch(batch.header.id, batch_pairs)
                        # TODO:0
                        bss.content_type = batch.header.status
                        if batch.header.status == FINISH_STATUS:
                            bss.set_finish(batch.header.totalSize)

                    # TransferService.remove_broker(tag) will be called in get_status phrase finished or exception got
            except Exception as e:
                L.exception(f'_run_put_batch error, tag={self.tag}')
                raise e

    def get_status(self, timeout):
        return BatchStreamStatus.wait_finish(self.tag, timeout)
