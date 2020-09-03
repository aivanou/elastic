#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import ctypes
import multiprocessing
import multiprocessing.connection
import signal
import sys
import time
import warnings
from typing import List, Optional

from torchelastic.multiprocessing import error_reporter


class SubprocessException(Exception):
    __slots__ = ["error_index", "error_pid"]

    def __init__(self, error_index: int, error_pid: int, msg: str):
        super().__init__(msg)
        self.error_index = error_index
        self.error_pid = error_pid


class WorkerRaisedException(SubprocessException):
    def __init__(
        self,
        error_index: int,
        error_pid: int,
        msg: str,
        error_report_msg: Optional[str] = None,
    ):
        if error_report_msg:
            msg = "".join([msg, "\n**Captured errors**\n", error_report_msg])
        super().__init__(error_index, error_pid, msg)


class WorkerSignaledException(SubprocessException):
    __slots__ = ["signal_name"]

    def __init__(
        self, error_index: int, error_pid: int, signal_name: str, signal_trace_msg: str
    ):
        super().__init__(error_index, error_pid, signal_trace_msg)
        self.signal_name = signal_name


class WorkerExitedException(SubprocessException):
    __slots__ = ["exit_code"]

    def __init__(
        self, error_index: int, error_pid: int, exit_code: int, exit_trace_msg: str
    ):
        super().__init__(error_index, error_pid, exit_trace_msg)
        self.exit_code = exit_code


def _pr_set_pdeathsig(sig=signal.SIGTERM):
    r"""Set the parent process death signal of the calling process.

    https://linux.die.net/man/2/prctl
    """
    libc = ctypes.CDLL("libc.so.6")
    PR_SET_PDEATHSIG = 1
    rv = libc.prctl(PR_SET_PDEATHSIG, sig)
    assert not rv, "prctl"


def _wrap(fn, i, args, error_queue):
    # prctl(2) is a Linux specific system call.
    # On other systems the following function call has no effect.
    # This is set to ensure that non-daemonic child processes can
    # terminate if their parent terminates before they do.
    _pr_set_pdeathsig(signal.SIGINT)

    try:
        fn(i, *args)
    except KeyboardInterrupt:
        pass  # SIGINT; Killed by parent, do nothing
    except Exception:
        # Propagate exception to parent process, keeping original traceback
        import traceback

        error_queue.put(traceback.format_exc())
        sys.exit(1)


# Multiprocessing contexts are introduced at Python 3.4
_supports_context = sys.version_info >= (3, 4)


def _python_version_check():
    if not _supports_context:
        raise RuntimeError(
            "Requires python 3.4 or higher to use "
            "torch.multiprocessing.spawn and "
            "torch.multiprocessing.ProcessContext helper "
            "to launch multiple processes. If you are using "
            "this for distributed training and have a lower "
            "version of python, please use "
            "torch.distributed.launch instead."
        )


class ProcessContext:
    def __init__(
        self,
        processes: List[multiprocessing.Process],
        error_queues: List[multiprocessing.SimpleQueue],
    ):
        _python_version_check()
        self.error_queues = error_queues
        self.processes = processes
        self.sentinels = {
            process.sentinel: index for index, process in enumerate(processes)
        }

    def pids(self):
        return [int(process.pid) for process in self.processes]

    def join(self, timeout=None):
        r"""
        Tries to join one or more processes in this spawn context.
        If one of them exited with a non-zero exit status, this function
        kills the remaining processes and raises an exception with the cause
        of the first process exiting.

        Returns ``True`` if all processes have been joined successfully,
        ``False`` if there are more processes that need to be joined.

        Arguments:
            timeout (float): Wait this long before giving up on waiting.
        """
        # Ensure this function can be called even when we're done.
        if len(self.sentinels) == 0:
            return True

        # Wait for any process to fail or all of them to succeed.
        ready = multiprocessing.connection.wait(self.sentinels.keys(), timeout=timeout)

        error_index = None
        for sentinel in ready:
            index = self.sentinels.pop(sentinel)
            process = self.processes[index]
            process.join()
            if process.exitcode != 0:
                error_index = index
                break

        # Return if there was no error.
        if error_index is None:
            # Return whether or not all processes have been joined.
            return len(self.sentinels) == 0

        # Assume failure. Terminate processes that are still alive.
        for process in self.processes:
            if process.is_alive():
                process.terminate()
            process.join()

        # Get error information from subprocess's private error file
        error_pid = self.processes[error_index].pid
        subprocess_error_msg = error_reporter.get_error(error_pid)

        # There won't be an error on the queue if the process crashed.
        if self.error_queues[error_index].empty():
            exitcode = self.processes[error_index].exitcode
            if exitcode < 0:
                name = signal.Signals(-exitcode).name
                raise WorkerSignaledException(
                    error_index, error_pid, name, subprocess_error_msg
                )
            else:
                raise WorkerExitedException(
                    error_index, error_pid, exitcode, subprocess_error_msg
                )

        original_trace = self.error_queues[error_index].get()
        msg = "\n\n-- Process %d terminated with the following error:\n" % error_index
        msg += original_trace
        raise WorkerRaisedException(error_index, error_pid, msg, subprocess_error_msg)


class SpawnContext(ProcessContext):
    def __init__(self, processes, error_queues):
        warnings.warn("SpawnContext is renamed to ProcessContext since 1.4 release.")
        super(SpawnContext, self).__init__(self, processes, error_queues)

    pass


# Note: [start_processes]
# mp.start_processes handles both start_method='spawn' and 'fork'. It's supposed to be a
# more generalized API than mp.spawn. Currently we only document mp.spawn as it's the
# CUDA compatible start_method. However, in environments like Ipython notebooks, 'fork'
# works better than 'spawn'. Every helper function we created for mp.spawn is indeed
# general enough, and backends like XLA can reuse them in Colab notebooks as well.
# Currently we only add this API first, we can consider adding it to documentation as
# needed in the future.
def start_processes(
    fn, args=(), nprocs=1, join=True, daemon=False, start_method="spawn"
):
    _python_version_check()
    timestamp_str_ms = str(int(time.monotonic() * 1000))
    error_reporter.configure(timestamp_str_ms)
    mp = multiprocessing.get_context(start_method)
    error_queues = []
    processes = []
    for i in range(nprocs):
        error_queue = mp.SimpleQueue()
        process = mp.Process(
            target=error_reporter.exec_fn,
            args=(_wrap, (fn, i, args, error_queue)),
            daemon=daemon,
        )
        process.start()
        error_queues.append(error_queue)
        processes.append(process)

    context = ProcessContext(processes, error_queues)
    if not join:
        return context

    # Loop on join until it returns True or raises an exception.
    while not context.join():
        pass


def spawn(fn, args=(), nprocs=1, join=True, daemon=False, start_method="spawn"):
    r"""Spawns ``nprocs`` processes that run ``fn`` with ``args``.

    If one of the processes exits with a non-zero exit status, the
    remaining processes are killed and an exception is raised with the
    cause of termination. In the case an exception was caught in the
    child process, it is forwarded and its traceback is included in
    the exception raised in the parent process.

    Arguments:
        fn (function): Function is called as the entrypoint of the
            spawned process. This function must be defined at the top
            level of a module so it can be pickled and spawned. This
            is a requirement imposed by multiprocessing.

            The function is called as ``fn(i, *args)``, where ``i`` is
            the process index and ``args`` is the passed through tuple
            of arguments.

        args (tuple): Arguments passed to ``fn``.
        nprocs (int): Number of processes to spawn.
        join (bool): Perform a blocking join on all processes.
        daemon (bool): The spawned processes' daemon flag. If set to True,
                       daemonic processes will be created.
        start_method (string): (deprecated) this method will always use ``spawn``
                               as the start method. To use a different start method
                               use ``start_processes()``.

    Returns:
        None if ``join`` is ``True``,
        :class:`~ProcessContext` if ``join`` is ``False``

    """
    if start_method != "spawn":
        msg = (
            "This method only supports start_method=spawn (got: %s).\n"
            "To use a different start_method use:\n\t\t"
            " torch.multiprocessing.start_process(...)" % start_method
        )
        warnings.warn(msg)
    return start_processes(fn, args, nprocs, join, daemon, start_method="spawn")
