# This code is part of Qiskit.
#
# (C) Copyright IBM 2019.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

# pylint: disable=cyclic-import, missing-return-doc

"""
=========
Schedules
=========

.. currentmodule:: qiskit.pulse

Schedules are Pulse programs. They describe instruction sequences for the control hardware.
The Schedule is one of the most fundamental objects to this pulse-level programming module.
A ``Schedule`` is a representation of a *program* in Pulse. Each schedule tracks the time of each
instruction occuring in parallel over multiple signal *channels*.

.. autosummary::
   :toctree: ../stubs/

   Schedule
   ScheduleBlock
"""

import abc
import copy
import functools
import itertools
import multiprocessing as mp
import sys
from typing import List, Tuple, Iterable, Union, Dict, Callable, Set, Optional, Any

import numpy as np

from qiskit.circuit.parameter import Parameter
from qiskit.circuit.parameterexpression import ParameterExpression, ParameterValueType
from qiskit.pulse.channels import Channel
from qiskit.pulse.exceptions import PulseError
from qiskit.pulse.instructions import Instruction
from qiskit.pulse.utils import instruction_duration_validation
from qiskit.utils.multiprocessing import is_main_process


# Imports
from ast import Del

ScheduleComponent = None
BlockComponent = None



Interval = Tuple[int, int]
"""An interval type is a tuple of a start time (inclusive) and an end time (exclusive)."""

TimeSlots = Dict[Channel, List[Interval]]
"""List of timeslots occupied by instructions for each channel."""

def merge_slots_pulse_delay(pulse_slot,delay_slot): # returns new delay-slot. 
    if delay_slot[0] < pulse_slot[0] and delay_slot[1]> pulse_slot[0]:
        return  (delay_slot[0],pulse_slot[0])
    elif delay_slot[0] < pulse_slot[1] and delay_slot[1]> pulse_slot[1]:
        return (pulse_slot[1],delay_slot[1])
    else:
        return None

def expand_slot(slot_inner, slot_outer): 
    if slots_overlap(slot_inner, slot_outer):
        return (min(slot_inner[0], slot_outer[0]), max(slot_inner[1], slot_outer[1]))
    else: 
        ValueError('slots do not overlap !!!')

def slots_overlap(slot_inner, slot_outer): 
    return slot_outer[0]<=slot_inner[0]  or slot_inner[1]<=slot_outer[1]

class Schedule:
    """A quantum program *schedule* with exact time constraints for its instructions, operating
    over all input signal *channels* and supporting special syntaxes for building.

    Pulse program representation for the original Qiskit Pulse model [1].
    Instructions are not allowed to overlap in time
    on the same channel. This overlap constraint is immediately
    evaluated when a new instruction is added to the ``Schedule`` object.

    It is necessary to specify the absolute start time and duration
    for each instruction so as to deterministically fix its execution time.

    The ``Schedule`` program supports some syntax sugar for easier programming.

    - Appending an instruction to the end of a channel

      .. code-block:: python

          sched = Schedule()
          sched += Play(Gaussian(160, 0.1, 40), DriveChannel(0))

    - Appending an instruction shifted in time by a given amount

      .. code-block:: python

          sched = Schedule()
          sched += Play(Gaussian(160, 0.1, 40), DriveChannel(0)) << 30

    - Merge two schedules

      .. code-block:: python

          sched1 = Schedule()
          sched1 += Play(Gaussian(160, 0.1, 40), DriveChannel(0))

          sched2 = Schedule()
          sched2 += Play(Gaussian(160, 0.1, 40), DriveChannel(1))
          sched2 = sched1 | sched2

    A :obj:`.PulseError` is immediately raised when the overlap constraint is violated.

    In the schedule representation, we cannot parametrize the duration of instructions.
    Thus we need to create a new schedule object for each duration.
    To parametrize an instruction's duration, the :class:`~qiskit.pulse.ScheduleBlock`
    representation may be used instead.

    References:
        [1]: https://arxiv.org/abs/2004.06755

    """

    # Prefix to use for auto naming.
    prefix = "sched"

    # Counter to count instance number.
    instances_counter = itertools.count()

    def __init__(
        self,
        *schedules: Union["ScheduleComponent", Tuple[int, "ScheduleComponent"]],
        name: Optional[str] = None,
        metadata: Optional[dict] = None,
    ):
        """Create an empty schedule.

        Args:
            *schedules: Child Schedules of this parent Schedule. May either be passed as
                        the list of schedules, or a list of ``(start_time, schedule)`` pairs.
            name: Name of this schedule. Defaults to an autogenerated string if not provided.
            metadata: Arbitrary key value metadata to associate with the schedule. This gets
                stored as free-form data in a dict in the
                :attr:`~qiskit.pulse.Schedule.metadata` attribute. It will not be directly
                used in the schedule.
        Raises:
            TypeError: if metadata is not a dict.
        """
        from qiskit.pulse.parameter_manager import ParameterManager

        if name is None:
            name = self.prefix + str(next(self.instances_counter))
            if sys.platform != "win32" and not is_main_process():
                name += f"-{mp.current_process().pid}"

        self._name = name
        self._parameter_manager = ParameterManager()

        if not isinstance(metadata, dict) and metadata is not None:
            raise TypeError("Only a dictionary or None is accepted for schedule metadata")
        self._metadata = metadata or {}

        self._duration = 0

        # These attributes are populated by ``_mutable_insert``
        self._timeslots = {}
        self._children = []
        for sched_pair in schedules:
            try:
                time, sched = sched_pair
            except TypeError:
                # recreate as sequence starting at 0.
                time, sched = 0, sched_pair
            self._mutable_insert(time, sched)

    @classmethod
    def initialize_from(cls, other_program: Any, name: Optional[str] = None) -> "Schedule":
        """Create new schedule object with metadata of another schedule object.

        Args:
            other_program: Qiskit program that provides metadata to new object.
            name: Name of new schedule. Name of ``schedule`` is used by default.

        Returns:
            New schedule object with name and metadata.

        Raises:
            PulseError: When `other_program` does not provide necessary information.
        """
        try:
            name = name or other_program.name

            if other_program.metadata:
                metadata = other_program.metadata.copy()
            else:
                metadata = None

            return cls(name=name, metadata=metadata)
        except AttributeError as ex:
            raise PulseError(
                f"{cls.__name__} cannot be initialized from the program data "
                f"{other_program.__class__.__name__}."
            ) from ex

    @property
    def name(self) -> str:
        """Name of this Schedule"""
        return self._name

    @property
    def metadata(self) -> Dict[str, Any]:
        """The user provided metadata associated with the schedule.

        User provided ``dict`` of metadata for the schedule.
        The metadata contents do not affect the semantics of the program
        but are used to influence the execution of the schedule. It is expected
        to be passed between all transforms of the schedule and that providers
        will associate any schedule metadata with the results it returns from the
        execution of that schedule.
        """
        return self._metadata

    @metadata.setter
    def metadata(self, metadata):
        """Update the schedule metadata"""
        if not isinstance(metadata, dict) and metadata is not None:
            raise TypeError("Only a dictionary or None is accepted for schedule metadata")
        self._metadata = metadata or {}

    @property
    def timeslots(self) -> TimeSlots:
        """Time keeping attribute."""
        return self._timeslots

    @property
    def duration(self) -> int:
        """Duration of this schedule."""
        return self._duration

    @property
    def start_time(self) -> int:
        """Starting time of this schedule."""
        return self.ch_start_time(*self.channels)

    @property
    def stop_time(self) -> int:
        """Stopping time of this schedule."""
        return self.duration

    @property
    def channels(self) -> Tuple[Channel]:
        """Returns channels that this schedule uses."""
        return tuple(self._timeslots.keys())

    @property
    def children(self) -> Tuple[Tuple[int, "ScheduleComponent"], ...]:
        """Return the child schedule components of this ``Schedule`` in the
        order they were added to the schedule.

        Notes:
            Nested schedules are returned as-is. If you want to collect only instructions,
            use py:meth:`~Schedule.instructions` instead.

        Returns:
            A tuple, where each element is a two-tuple containing the initial
            scheduled time of each ``NamedValue`` and the component
            itself.
        """
        return tuple(self._children)

    @property
    def instructions(self) -> Tuple[Tuple[int, Instruction]]:
        """Get the time-ordered instructions from self."""

        def key(time_inst_pair):
            inst = time_inst_pair[1]
            return time_inst_pair[0], inst.duration, sorted(chan.name for chan in inst.channels)

        return tuple(sorted(self._instructions(), key=key))

    @property
    def parameters(self) -> Set:
        """Parameters which determine the schedule behavior."""
        return self._parameter_manager.parameters

    def ch_duration(self, *channels: Channel) -> int:
        """Return the time of the end of the last instruction over the supplied channels.

        Args:
            *channels: Channels within ``self`` to include.
        """
        return self.ch_stop_time(*channels)

    def ch_start_time(self, *channels: Channel) -> int:
        """Return the time of the start of the first instruction over the supplied channels.

        Args:
            *channels: Channels within ``self`` to include.
        """
        try:
            chan_intervals = (self._timeslots[chan] for chan in channels if chan in self._timeslots)
            return min(intervals[0][0] for intervals in chan_intervals)
        except ValueError:
            # If there are no instructions over channels
            return 0

    def ch_stop_time(self, *channels: Channel) -> int:
        """Return maximum start time over supplied channels.

        Args:
            *channels: Channels within ``self`` to include.
        """
        try:
            chan_intervals = (self._timeslots[chan] for chan in channels if chan in self._timeslots)
            return max(intervals[-1][1] for intervals in chan_intervals)
        except ValueError:
            # If there are no instructions over channels
            return 0

    def _instructions(self, time: int = 0):
        """Iterable for flattening Schedule tree.

        Args:
            time: Shifted time due to parent.

        Yields:
            Iterable[Tuple[int, Instruction]]: Tuple containing the time each
                :class:`~qiskit.pulse.Instruction`
                starts at and the flattened :class:`~qiskit.pulse.Instruction` s.
        """
        for insert_time, child_sched in self.children:
            yield from child_sched._instructions(time + insert_time)

    def shift(self, time: int, name: Optional[str] = None, inplace: bool = False) -> "Schedule":
        """Return a schedule shifted forward by ``time``.

        Args:
            time: Time to shift by.
            name: Name of the new schedule. Defaults to the name of self.
            inplace: Perform operation inplace on this schedule. Otherwise
                return a new ``Schedule``.
        """
        if inplace:
            return self._mutable_shift(time)
        return self._immutable_shift(time, name=name)

    def _immutable_shift(self, time: int, name: Optional[str] = None) -> "Schedule":
        """Return a new schedule shifted forward by `time`.

        Args:
            time: Time to shift by
            name: Name of the new schedule if call was mutable. Defaults to name of self
        """
        shift_sched = Schedule.initialize_from(self, name)
        shift_sched.insert(time, self, inplace=True)

        return shift_sched

    def _mutable_shift(self, time: int) -> "Schedule":
        """Return this schedule shifted forward by `time`.

        Args:
            time: Time to shift by

        Raises:
            PulseError: if ``time`` is not an integer.
        """
        if not isinstance(time, int):
            raise PulseError("Schedule start time must be an integer.")

        timeslots = {}
        for chan, ch_timeslots in self._timeslots.items():
            timeslots[chan] = [(ts[0] + time, ts[1] + time) for ts in ch_timeslots]

        _check_nonnegative_timeslot(timeslots)

        self._duration = self._duration + time
        self._timeslots = timeslots
        self._children = [(orig_time + time, child) for orig_time, child in self.children]
        return self

    def insert(
        self,
        start_time: int,
        schedule: "ScheduleComponent",
        name: Optional[str] = None,
        inplace: bool = False,
    ) -> "Schedule":
        """Return a new schedule with ``schedule`` inserted into ``self`` at ``start_time``.

        Args:
            start_time: Time to insert the schedule.
            schedule: Schedule to insert.
            name: Name of the new schedule. Defaults to the name of self.
            inplace: Perform operation inplace on this schedule. Otherwise
                return a new ``Schedule``.
        """
        if inplace:
            return self._mutable_insert(start_time, schedule)
        return self._immutable_insert(start_time, schedule, name=name)

    def _mutable_insert(self, start_time: int, schedule: ScheduleComponent) -> "Schedule":
        """Mutably insert `schedule` into `self` at `start_time`.

        Args:
            start_time: Time to insert the second schedule.
            schedule: Schedule to mutably insert.
        """

        # here we have to calculate a new start time, since the DP channels should not be regarded for this
        # get DP-channels from backend_configuration: 
        from qiskit.pulse import DriveChannel,Play, Delay
        dp_channels_idx = [24, 1]
        dp_channels = [DriveChannel(i) for i in dp_channels_idx]
        channels_dp_common = list(set(self.channels) & set(schedule.channels) & set(dp_channels))

        is_sched = False
        if isinstance(schedule,Schedule):
            if len(list(set(schedule.channels) - set(dp_channels))) != 0 and len(list(set(self.channels) & set(schedule.channels) & set(dp_channels))) != 0:  # need to share at least one DP channel, and the added schedule needs to have at least one SP. 

                # get shared DP-channels 
                """for channel_dp in channels_dp_common:
                sched_channel_dp_self   = self.filter(channels = channel_dp)
                sched_channel_dp        = schedule.filter(channels=channel_dp)
                #exclude common DP channel, and generate overlapping DP channel
                self = self.exclude(channels = channel_dp)
                schedule = schedule.exclude(channels = channel_dp)
                dp_sched = merge_channels(sched_channel_dp_self, sched_channel_dp, channel_dp, time_shift=start_time) # TODO: diesenhier ändern..
                self.append(dp_sched, inplace = True)
                self._renew_timeslots() """
                # alternative: without merge_channels..
                def key(time_inst_pair):
                        inst = time_inst_pair[1]
                        return time_inst_pair[0], inst.duration, sorted(chan.name for chan in inst.channels)

                def _overlaps(first: Interval, second: Interval) -> bool:
                    """Return True iff first and second overlap.
                    Note: first.stop may equal second.start, since Interval stop times are exclusive.
                    """
                    if first[0] == second[0] == second[1]:
                        # They fail to overlap if one of the intervals has duration 0
                        return False
                    if first[0] > second[0]:
                        first, second = second, first
                    return second[0] < first[1]

                #print('merging dp channels')

                for channel_dp in channels_dp_common:
                    #exclude delays
                    #print('start_time', channel_dp, start_time)

                    sched1_instructions = self.filter(channels=channel_dp,time_ranges=[(start_time, self.duration)] ).instructions #.exclude(channels=channel_dp, instruction_types=Delay)
                    sched2_instructions = schedule.filter(channels=channel_dp).shift(start_time, inplace=False).instructions #.exclude(channels=channel_dp, instruction_types=Delay)
                    # hier eventuell ein problem !! ?
                    sched_instructions = list(tuple(sorted(sched1_instructions + sched2_instructions, key=key)))  # sort instruction...
                    #print('instructions', sched_instructions)
                    new_instructions = []
                    while(len(sched_instructions) != 0):
                        (t_outer, inst_outer) = sched_instructions.pop(0)
                        int_outer = (t_outer, t_outer + inst_outer.duration)
                        for (t_inner, inst_inner) in sched_instructions:
                            int_inner = (t_inner, t_inner + inst_inner.duration)
                            if _overlaps(int_outer, int_inner):
                                if inst_outer.duration != 0 and inst_inner.duration != 0 :  # both overlapping instructions are Plays or Delays !!
                                    # merge the play pulses !!
                                    if isinstance(inst_outer, Play) and isinstance(inst_inner, Play):
                                        #int_new = expand_slot(int_outer, int_inner)
                                        
                                        """ dur_new = int_new[1] - int_new[0]
                                        #amp_new = inst_outer.operands[0].amp
                                        amp_new = inst_outer.pulse.amp
                                        channel_new = inst_outer.channel
                                        inst_outer = Play(Constant(dur_new, amp_new),channel_new)    # TODO: maybe change here because of different pulses (static, logic etc. ) """

                                        #inst_outer = Play(inst_outer.merge(inst_inner),channel_new)  
                                        inst_outer.pulse.merge(inst_inner.pulse,int_outer, int_inner)
                                        int_outer = expand_slot(int_outer, int_inner)
                                        t_outer = int_outer[0]
                                        sched_instructions.remove((t_inner, inst_inner))
                                    elif isinstance(inst_outer, Play) and isinstance(inst_inner, Delay):
                                        int_new = merge_slots_pulse_delay(int_outer, int_inner)

                                        if int_new: 
                                            dur_new = int_new[1] - int_new[0]
                                            channel_new = inst_outer.channel
                                            inst_new = Delay(dur_new, channel_new)
                                            sched_instructions.remove((t_inner, inst_inner))
                                            sched_instructions.append((int_new[0],inst_new))
                                            sched1_instructions.sort(key=key)
                                        else:  # remove if delay slot is inside pulse slot
                                            sched_instructions.remove((t_inner, inst_inner))
                                    elif isinstance(inst_outer, Delay) and isinstance(inst_inner, Play):
                                        int_new = merge_slots_pulse_delay(int_outer, int_inner)

                                        if int_new: 
                                            dur_new = int_new[1] - int_new[0]
                                            channel_new = inst_outer.channel
                                            inst_outer = Delay(dur_new, channel_new)
                                            int_outer = int_new # new interval
                                            t_outer = int_outer[0]
                                        else: 
                                            t_outer = None
                                            inst_outer = None
                                            int_outer = None
                                    elif isinstance(inst_outer, Delay) and isinstance(inst_outer, Delay):
                                            int_new = expand_slot(int_outer, int_inner)
                                            dur_new = int_new[1] - int_new[0]
                                            channel_new = inst_outer.channel
                                            inst_outer = Delay(dur_new, channel_new)
                                            sched_instructions.remove((t_inner, inst_inner))


                                elif inst_outer.duration == 0 and inst_inner.duration != 0: 
                                    # insert to the front. 
                                    t_outer = t_inner # Insert at the front of the mabe to the back?..
                                elif inst_outer.duration != 0 and inst_inner.duration == 0: 
                                    # insert  second one to the front, to the front. 
                                    # don nothing gets handled by the above satements.
                                    
                                    sched_instructions.remove((t_inner, inst_inner))

                        if inst_outer:
                            new_instructions.append((t_outer, inst_outer))

                    new_instructions = list(tuple(sorted(new_instructions, key=key)))  # sort instruction...

                    #self = self.exclude(channels = channel_dp,time_ranges=[(start_time, self.duration)])
                    #print('self exclude before', self)
                    self = self.exclude(channels = channel_dp,time_ranges=[(start_time, self.duration)])
                    #print('self exclude after', self)
                    schedule = schedule.exclude(channels=channel_dp )

                    #print('new_instructions',new_instructions)
                    for t, inst in new_instructions: 
                        #print('inserted', t,inst)
                        self.insert(t, inst, inplace = True)

                    #self._renew_timeslots()
                    #schedule._renew_timeslots()
                    #print('DP-channel, self',  self)
                    #print('schedule', schedule)
                    is_sched = True


        self._add_timeslots(start_time, schedule)
        self._children.append((start_time, schedule))
        self._parameter_manager.update_parameter_table(schedule)
        #if is_sched: print('self.children', self)
        return self

    def _immutable_insert(
        self,
        start_time: int,
        schedule: "ScheduleComponent",
        name: Optional[str] = None,
    ) -> "Schedule":
        """Return a new schedule with ``schedule`` inserted into ``self`` at ``start_time``.
        Args:
            start_time: Time to insert the schedule.
            schedule: Schedule to insert.
            name: Name of the new ``Schedule``. Defaults to name of ``self``.
        """
        new_sched = Schedule.initialize_from(self, name)
        new_sched._mutable_insert(0, self)
        new_sched._mutable_insert(start_time, schedule)
        return new_sched

    def append(
        self, schedule: ScheduleComponent, name: Optional[str] = None, inplace: bool = False
    ) -> "Schedule":
        r"""Return a new schedule with ``schedule`` inserted at the maximum time over
        all channels shared between ``self`` and ``schedule``.

        .. math::

            t = \textrm{max}(\texttt{x.stop_time} |\texttt{x} \in
                \texttt{self.channels} \cap \texttt{schedule.channels})

        Args:
            schedule: Schedule to be appended.
            name: Name of the new ``Schedule``. Defaults to name of ``self``.
            inplace: Perform operation inplace on this schedule. Otherwise
                return a new ``Schedule``.
        """
        # here we have to calculate a new start time, since the DP channels should not be regarded for this
            # get DP-channels from backend_configuration: 
        from qiskit.pulse import DriveChannel
        dp_channels_idx = [24, 1]
        dp_channels = [DriveChannel(i) for i in dp_channels_idx]   # TODO: change to only the insert point is the SP-Channel to do overlapping. Solve later. 

        if len(list(set(schedule.channels) - set(dp_channels))) == 0 or len(list(set(schedule.channels) & set(dp_channels))) == 0 : # Only DP channels are aded: treat as normal.    // when Sched keinen Nur SP order nur DP ist, dann füge normal ein. Wenn DP + SP, und der DP mit dem anderen Sched geteilt wird, dann füge nach der Zeit vom SP ein. 
            common_channels = set(self.channels) & set(schedule.channels)
        else: 
            common_channels = set(self.channels) & set(schedule.channels) - set(dp_channels) # excude DP-channels to find insertion time. 

        time = self.ch_stop_time(*common_channels)
        #print(f'time: {time}, common_channels: {common_channels}')
        new_sched = self.insert(time, schedule, name=name, inplace=inplace)

        return new_sched

    def filter(
        self,
        *filter_funcs: Callable,
        channels: Optional[Iterable[Channel]] = None,
        instruction_types: Union[Iterable[abc.ABCMeta], abc.ABCMeta] = None,
        time_ranges: Optional[Iterable[Tuple[int, int]]] = None,
        intervals: Optional[Iterable[Interval]] = None,
        check_subroutine: bool = True,
    ) -> "Schedule":
        """Return a new ``Schedule`` with only the instructions from this ``Schedule`` which pass
        though the provided filters; i.e. an instruction will be retained iff every function in
        ``filter_funcs`` returns ``True``, the instruction occurs on a channel type contained in
        ``channels``, the instruction type is contained in ``instruction_types``, and the period
        over which the instruction operates is *fully* contained in one specified in
        ``time_ranges`` or ``intervals``.

        If no arguments are provided, ``self`` is returned.

        Args:
            filter_funcs: A list of Callables which take a (int, Union['Schedule', Instruction])
                tuple and return a bool.
            channels: For example, ``[DriveChannel(0), AcquireChannel(0)]``.
            instruction_types: For example, ``[PulseInstruction, AcquireInstruction]``.
            time_ranges: For example, ``[(0, 5), (6, 10)]``.
            intervals: For example, ``[(0, 5), (6, 10)]``.
            check_subroutine: Set `True` to individually filter instructions inside of a subroutine
                defined by the :py:class:`~qiskit.pulse.instructions.Call` instruction.
        """
        from qiskit.pulse.filters import composite_filter, filter_instructions

        filters = composite_filter(channels, instruction_types, time_ranges, intervals)
        filters.extend(filter_funcs)

        return filter_instructions(
            self, filters=filters, negate=False, recurse_subroutines=check_subroutine
        )

    def exclude(
        self,
        *filter_funcs: Callable,
        channels: Optional[Iterable[Channel]] = None,
        instruction_types: Union[Iterable[abc.ABCMeta], abc.ABCMeta] = None,
        time_ranges: Optional[Iterable[Tuple[int, int]]] = None,
        intervals: Optional[Iterable[Interval]] = None,
        check_subroutine: bool = True,
    ) -> "Schedule":
        """Return a ``Schedule`` with only the instructions from this Schedule *failing*
        at least one of the provided filters.
        This method is the complement of py:meth:`~self.filter`, so that::

            self.filter(args) | self.exclude(args) == self

        Args:
            filter_funcs: A list of Callables which take a (int, Union['Schedule', Instruction])
                tuple and return a bool.
            channels: For example, ``[DriveChannel(0), AcquireChannel(0)]``.
            instruction_types: For example, ``[PulseInstruction, AcquireInstruction]``.
            time_ranges: For example, ``[(0, 5), (6, 10)]``.
            intervals: For example, ``[(0, 5), (6, 10)]``.
            check_subroutine: Set `True` to individually filter instructions inside of a subroutine
                defined by the :py:class:`~qiskit.pulse.instructions.Call` instruction.
        """
        from qiskit.pulse.filters import composite_filter, filter_instructions

        filters = composite_filter(channels, instruction_types, time_ranges, intervals)
        filters.extend(filter_funcs)

        return filter_instructions(
            self, filters=filters, negate=True, recurse_subroutines=check_subroutine
        )

    def _add_timeslots(self, time: int, schedule: "ScheduleComponent") -> None:
        """Update all time tracking within this schedule based on the given schedule.

        Args:
            time: The time to insert the schedule into self.
            schedule: The schedule to insert into self.

        Raises:
            PulseError: If timeslots overlap or an invalid start time is provided.
        """
        if not np.issubdtype(type(time), np.integer):
            raise PulseError("Schedule start time must be an integer.")

        other_timeslots = _get_timeslots(schedule)
        self._duration = max(self._duration, time + schedule.duration)

        for channel in schedule.channels:
            if channel not in self._timeslots:
                if time == 0:
                    self._timeslots[channel] = copy.copy(other_timeslots[channel])
                else:
                    self._timeslots[channel] = [
                        (i[0] + time, i[1] + time) for i in other_timeslots[channel]
                    ]
                continue

            for idx, interval in enumerate(other_timeslots[channel]):
                if interval[0] + time >= self._timeslots[channel][-1][1]:
                    # Can append the remaining intervals
                    self._timeslots[channel].extend(
                        [(i[0] + time, i[1] + time) for i in other_timeslots[channel][idx:]]
                    )
                    break

                try:
                    interval = (interval[0] + time, interval[1] + time)
                    index = _find_insertion_index(self._timeslots[channel], interval)
                    self._timeslots[channel].insert(index, interval)
                except PulseError as ex:
                    raise PulseError(
                        "Schedule(name='{new}') cannot be inserted into Schedule(name='{old}') at "
                        "time {time} because its instruction on channel {ch} scheduled from time "
                        "{t0} to {tf} overlaps with an existing instruction."
                        "".format(
                            new=schedule.name or "",
                            old=self.name or "",
                            time=time,
                            ch=channel,
                            t0=interval[0],
                            tf=interval[1],
                        )
                    ) from ex

        _check_nonnegative_timeslot(self._timeslots)

    def _remove_timeslots(self, time: int, schedule: "ScheduleComponent"):
        """Delete the timeslots if present for the respective schedule component.

        Args:
            time: The time to remove the timeslots for the ``schedule`` component.
            schedule: The schedule to insert into self.

        Raises:
            PulseError: If timeslots overlap or an invalid start time is provided.
        """
        if not isinstance(time, int):
            raise PulseError("Schedule start time must be an integer.")

        for channel in schedule.channels:

            if channel not in self._timeslots:
                raise PulseError(f"The channel {channel} is not present in the schedule")

            channel_timeslots = self._timeslots[channel]
            other_timeslots = _get_timeslots(schedule)

            for interval in other_timeslots[channel]:
                if channel_timeslots:
                    interval = (interval[0] + time, interval[1] + time)
                    index = _interval_index(channel_timeslots, interval)
                    if channel_timeslots[index] == interval:
                        channel_timeslots.pop(index)
                        continue

                raise PulseError(
                    "Cannot find interval ({t0}, {tf}) to remove from "
                    "channel {ch} in Schedule(name='{name}').".format(
                        ch=channel, t0=interval[0], tf=interval[1], name=schedule.name
                    )
                )

            if not channel_timeslots:
                self._timeslots.pop(channel)

    def _replace_timeslots(self, time: int, old: "ScheduleComponent", new: "ScheduleComponent"):
        """Replace the timeslots of ``old`` if present with the timeslots of ``new``.

        Args:
            time: The time to remove the timeslots for the ``schedule`` component.
            old: Instruction to replace.
            new: Instruction to replace with.
        """
        self._remove_timeslots(time, old)
        self._add_timeslots(time, new)

    def _renew_timeslots(self):
        """Regenerate timeslots based on current instructions."""
        self._timeslots.clear()
        for t0, inst in self.instructions:
            self._add_timeslots(t0, inst)

    def replace(
        self,
        old: "ScheduleComponent",
        new: "ScheduleComponent",
        inplace: bool = False,
    ) -> "Schedule":
        """Return a ``Schedule`` with the ``old`` instruction replaced with a ``new``
        instruction.

        The replacement matching is based on an instruction equality check.

        .. jupyter-kernel:: python3
            :id: replace

        .. jupyter-execute::

            from qiskit import pulse

            d0 = pulse.DriveChannel(0)

            sched = pulse.Schedule()

            old = pulse.Play(pulse.Constant(100, 1.0), d0)
            new = pulse.Play(pulse.Constant(100, 0.1), d0)

            sched += old

            sched = sched.replace(old, new)

            assert sched == pulse.Schedule(new)

        Only matches at the top-level of the schedule tree. If you wish to
        perform this replacement over all instructions in the schedule tree.
        Flatten the schedule prior to running::

        .. jupyter-execute::

            sched = pulse.Schedule()

            sched += pulse.Schedule(old)

            sched = sched.flatten()

            sched = sched.replace(old, new)

            assert sched == pulse.Schedule(new)

        Args:
            old: Instruction to replace.
            new: Instruction to replace with.
            inplace: Replace instruction by mutably modifying this ``Schedule``.

        Returns:
            The modified schedule with ``old`` replaced by ``new``.

        Raises:
            PulseError: If the ``Schedule`` after replacements will has a timing overlap.
        """
        from qiskit.pulse.parameter_manager import ParameterManager

        new_children = []
        new_parameters = ParameterManager()

        for time, child in self.children:
            if child == old:
                new_children.append((time, new))
                new_parameters.update_parameter_table(new)
            else:
                new_children.append((time, child))
                new_parameters.update_parameter_table(child)

        if inplace:
            self._children = new_children
            self._parameter_manager = new_parameters
            self._renew_timeslots()
            return self
        else:
            try:
                new_sched = Schedule.initialize_from(self)
                for time, inst in new_children:
                    new_sched.insert(time, inst, inplace=True)
                return new_sched
            except PulseError as err:
                raise PulseError(
                    f"Replacement of {old} with {new} results in overlapping instructions."
                ) from err

    def is_parameterized(self) -> bool:
        """Return True iff the instruction is parameterized."""
        return self._parameter_manager.is_parameterized()

    def assign_parameters(
        self, value_dict: Dict[ParameterExpression, ParameterValueType], inplace: bool = True
    ) -> "Schedule":
        """Assign the parameters in this schedule according to the input.

        Args:
            value_dict: A mapping from Parameters to either numeric values or another
                Parameter expression.
            inplace: Set ``True`` to override this instance with new parameter.

        Returns:
            Schedule with updated parameters.
        """
        return self._parameter_manager.assign_parameters(
            pulse_program=self, value_dict=value_dict, inplace=inplace
        )

    def get_parameters(self, parameter_name: str) -> List[Parameter]:
        """Get parameter object bound to this schedule by string name.

        Because different ``Parameter`` objects can have the same name,
        this method returns a list of ``Parameter`` s for the provided name.

        Args:
            parameter_name: Name of parameter.

        Returns:
            Parameter objects that have corresponding name.
        """
        return self._parameter_manager.get_parameters(parameter_name)

    def __len__(self) -> int:
        """Return number of instructions in the schedule."""
        return len(self.instructions)

    def __add__(self, other: "ScheduleComponent") -> "Schedule":
        """Return a new schedule with ``other`` inserted within ``self`` at ``start_time``."""
        return self.append(other)

    def __or__(self, other: "ScheduleComponent") -> "Schedule":
        """Return a new schedule which is the union of `self` and `other`."""
        return self.insert(0, other)

    def __lshift__(self, time: int) -> "Schedule":
        """Return a new schedule which is shifted forward by ``time``."""
        return self.shift(time)

    def __eq__(self, other: "ScheduleComponent") -> bool:
        """Test if two Schedule are equal.

        Equality is checked by verifying there is an equal instruction at every time
        in ``other`` for every instruction in this ``Schedule``.

        .. warning::

            This does not check for logical equivalency. Ie.,

            ```python
            >>> Delay(10, DriveChannel(0)) + Delay(10, DriveChannel(0))
                == Delay(20, DriveChannel(0))
            False
            ```
        """
        # 0. type check, we consider Instruction is a subtype of schedule
        if not isinstance(other, (type(self), Instruction)):
            return False

        # 1. channel check
        if set(self.channels) != set(other.channels):
            return False

        # 2. size check
        if len(self.instructions) != len(other.instructions):
            return False

        # 3. instruction check
        return all(
            self_inst == other_inst
            for self_inst, other_inst in zip(self.instructions, other.instructions)
        )

    def __repr__(self) -> str:
        name = format(self._name) if self._name else ""
        instructions = ", ".join([repr(instr) for instr in self.instructions[:50]])
        if len(self.instructions) > 25:
            instructions += ", ..."
        return f'{self.__class__.__name__}({instructions}, name="{name}")'


def _require_schedule_conversion(function: Callable) -> Callable:
    """A method decorator to convert schedule block to pulse schedule.

    This conversation is performed for backward compatibility only if all durations are assigned.
    """

    @functools.wraps(function)
    def wrapper(self, *args, **kwargs):
        from qiskit.pulse.transforms import block_to_schedule

        return function(block_to_schedule(self), *args, **kwargs)

    return wrapper


class ScheduleBlock:
    r"""A ``ScheduleBlock`` is a time-ordered sequence of instructions and transform macro to
    manage their relative timing. The relative position of the instructions is managed by
    the ``alignment_context``. This allows ``ScheduleBlock`` to support instructions with
    a parametric duration and allows the lazy scheduling of instructions,
    i.e. allocating the instruction time just before execution.

    ``ScheduleBlock``\ s should be initialized with one of the following alignment contexts:

    - :class:`~qiskit.pulse.transforms.AlignLeft`: Align instructions in the
      `as-soon-as-possible` manner. Instructions are scheduled at the earliest
      possible time on the channel.

    - :class:`~qiskit.pulse.transforms.AlignRight`: Align instructions in the
      `as-late-as-possible` manner. Instructions are scheduled at the latest
      possible time on the channel.

    - :class:`~qiskit.pulse.transforms.AlignSequential`: Align instructions sequentially
      even though they are allocated in different channels.

    - :class:`~qiskit.pulse.transforms.AlignEquispaced`: Align instructions with
      equal interval within a specified duration. Instructions on different channels
      are aligned sequentially.

    - :class:`~qiskit.pulse.transforms.AlignFunc`: Align instructions with
      arbitrary position within the given duration. The position is specified by
      a callback function taking a pulse index ``j`` and returning a
      fractional coordinate in [0, 1].

    The ``ScheduleBlock`` defaults to the ``AlignLeft`` alignment.
    The timing overlap constraint of instructions is not immediately evaluated,
    and thus we can assign a parameter object to the instruction duration.
    Instructions are implicitly scheduled at optimum time when the program is executed.

    Note that ``ScheduleBlock`` can contain :class:`~qiskit.pulse.instructions.Instruction`
    and other ``ScheduleBlock`` to build an experimental program, but ``Schedule`` is not
    supported. This should be added as a :class:`~qiskit.pulse.instructions.Call` instruction.
    This conversion is automatically performed with the pulse builder.

    By using ``ScheduleBlock`` representation we can fully parametrize pulse waveforms.
    For example, Rabi schedule generator can be defined as

    .. code-block:: python

        duration = Parameter('rabi_dur')
        amp = Parameter('rabi_amp')

        block = ScheduleBlock()
        rabi_pulse = pulse.Gaussian(duration=duration, amp=amp, sigma=duration/4)

        block += Play(rabi_pulse, pulse.DriveChannel(0))
        block += Call(measure_schedule)

    Note that such waveform cannot be appended to the ``Schedule`` representation.

    In the block representation, the interval between two instructions can be
    managed with the ``Delay`` instruction. Because the schedule block lacks an instruction
    start time ``t0``, we cannot ``insert`` or ``shift`` the target instruction.
    In addition, stored instructions are not interchangable because the schedule block is
    sensitive to the relative position of instructions.
    Apart from these differences, the block representation can provide compatible
    functionality with ``Schedule`` representation.
    """

    # Prefix to use for auto naming.
    prefix = "block"

    # Counter to count instance number.
    instances_counter = itertools.count()

    def __init__(
        self, name: Optional[str] = None, metadata: Optional[dict] = None, alignment_context=None
    ):
        """Create an empty schedule block.

        Args:
            name: Name of this schedule. Defaults to an autogenerated string if not provided.
            metadata: Arbitrary key value metadata to associate with the schedule. This gets
                stored as free-form data in a dict in the
                :attr:`~qiskit.pulse.ScheduleBlock.metadata` attribute. It will not be directly
                used in the schedule.
            alignment_context (AlignmentKind): ``AlignmentKind`` instance that manages
                scheduling of instructions in this block.
        Raises:
            TypeError: if metadata is not a dict.
        """
        from qiskit.pulse.parameter_manager import ParameterManager
        from qiskit.pulse.transforms import AlignLeft

        if name is None:
            name = self.prefix + str(next(self.instances_counter))
            if sys.platform != "win32" and not is_main_process():
                name += f"-{mp.current_process().pid}"

        self._name = name
        self._parameter_manager = ParameterManager()

        if not isinstance(metadata, dict) and metadata is not None:
            raise TypeError("Only a dictionary or None is accepted for schedule metadata")
        self._metadata = metadata or {}

        self._alignment_context = alignment_context or AlignLeft()
        self._blocks = []

        # get parameters from context
        self._parameter_manager.update_parameter_table(self._alignment_context)

    @classmethod
    def initialize_from(cls, other_program: Any, name: Optional[str] = None) -> "ScheduleBlock":
        """Create new schedule object with metadata of another schedule object.

        Args:
            other_program: Qiskit program that provides metadata to new object.
            name: Name of new schedule. Name of ``block`` is used by default.

        Returns:
            New block object with name and metadata.

        Raises:
            PulseError: When `other_program` does not provide necessary information.
        """
        try:
            name = name or other_program.name

            if other_program.metadata:
                metadata = other_program.metadata.copy()
            else:
                metadata = None

            try:
                alignment_context = other_program.alignment_context
            except AttributeError:
                alignment_context = None

            return cls(name=name, metadata=metadata, alignment_context=alignment_context)
        except AttributeError as ex:
            raise PulseError(
                f"{cls.__name__} cannot be initialized from the program data "
                f"{other_program.__class__.__name__}."
            ) from ex

    @property
    def name(self) -> str:
        """Name of this Schedule"""
        return self._name

    @property
    def metadata(self) -> Dict[str, Any]:
        """The user provided metadata associated with the schedule.

        User provided ``dict`` of metadata for the schedule.
        The metadata contents do not affect the semantics of the program
        but are used to influence the execution of the schedule. It is expected
        to be passed between all transforms of the schedule and that providers
        will associate any schedule metadata with the results it returns from the
        execution of that schedule.
        """
        return self._metadata

    @metadata.setter
    def metadata(self, metadata):
        """Update the schedule metadata"""
        if not isinstance(metadata, dict) and metadata is not None:
            raise TypeError("Only a dictionary or None is accepted for schedule metadata")
        self._metadata = metadata or {}

    @property
    def alignment_context(self):
        """Return alignment instance that allocates block component to generate schedule."""
        return self._alignment_context

    def is_schedulable(self) -> bool:
        """Return ``True`` if all durations are assigned."""
        # check context assignment
        for context_param in self.alignment_context._context_params:
            if isinstance(context_param, ParameterExpression):
                return False

        # check duration assignment
        for block in self.blocks:
            if isinstance(block, ScheduleBlock):
                if not block.is_schedulable():
                    return False
            else:
                if not isinstance(block.duration, int):
                    return False
        return True

    @property
    @_require_schedule_conversion
    def duration(self) -> int:
        """Duration of this schedule block."""
        return self.duration

    @property
    def channels(self) -> Tuple[Channel]:
        """Returns channels that this schedule clock uses."""
        chans = set()
        for block in self.blocks:
            for chan in block.channels:
                chans.add(chan)
        return tuple(chans)

    @property
    @_require_schedule_conversion
    def instructions(self) -> Tuple[Tuple[int, Instruction]]:
        """Get the time-ordered instructions from self."""
        return self.instructions

    @property
    def blocks(self) -> Tuple["BlockComponent", ...]:
        """Get the time-ordered instructions from self."""
        return tuple(self._blocks)

    @property
    def parameters(self) -> Set:
        """Parameters which determine the schedule behavior."""
        return self._parameter_manager.parameters

    @_require_schedule_conversion
    def ch_duration(self, *channels: Channel) -> int:
        """Return the time of the end of the last instruction over the supplied channels.

        Args:
            *channels: Channels within ``self`` to include.
        """
        return self.ch_duration(*channels)

    def append(
        self, block: "BlockComponent", name: Optional[str] = None, inplace: bool = True
    ) -> "ScheduleBlock":
        """Return a new schedule block with ``block`` appended to the context block.
        The execution time is automatically assigned when the block is converted into schedule.

        Args:
            block: ScheduleBlock to be appended.
            name: Name of the new ``Schedule``. Defaults to name of ``self``.
            inplace: Perform operation inplace on this schedule. Otherwise
                return a new ``Schedule``.

        Returns:
            Schedule block with appended schedule.

        Raises:
            PulseError: When invalid schedule type is specified.
        """
        if not isinstance(block, (ScheduleBlock, Instruction)):
            raise PulseError(
                f"Appended `schedule` {block.__class__.__name__} is invalid type. "
                "Only `Instruction` and `ScheduleBlock` can be accepted."
            )

        if not inplace:
            ret_block = copy.deepcopy(self)
            ret_block._name = name or self.name
            ret_block.append(block, inplace=True)
            return ret_block
        else:
            self._blocks.append(block)
            self._parameter_manager.update_parameter_table(block)

            return self

    def filter(
        self,
        *filter_funcs: List[Callable],
        channels: Optional[Iterable[Channel]] = None,
        instruction_types: Union[Iterable[abc.ABCMeta], abc.ABCMeta] = None,
        time_ranges: Optional[Iterable[Tuple[int, int]]] = None,
        intervals: Optional[Iterable[Interval]] = None,
        check_subroutine: bool = True,
    ):
        """Return a new ``Schedule`` with only the instructions from this ``ScheduleBlock``
        which pass though the provided filters; i.e. an instruction will be retained iff
        every function in ``filter_funcs`` returns ``True``, the instruction occurs on
        a channel type contained in ``channels``, the instruction type is contained
        in ``instruction_types``, and the period over which the instruction operates
        is *fully* contained in one specified in ``time_ranges`` or ``intervals``.

        If no arguments are provided, ``self`` is returned.

        .. note:: This method is currently not supported. Support will be soon added
            please create an issue if you believe this must be prioritized.

        Args:
            filter_funcs: A list of Callables which take a (int, Union['Schedule', Instruction])
                tuple and return a bool.
            channels: For example, ``[DriveChannel(0), AcquireChannel(0)]``.
            instruction_types: For example, ``[PulseInstruction, AcquireInstruction]``.
            time_ranges: For example, ``[(0, 5), (6, 10)]``.
            intervals: For example, ``[(0, 5), (6, 10)]``.
            check_subroutine: Set `True` to individually filter instructions inside of a subroutine
                defined by the :py:class:`~qiskit.pulse.instructions.Call` instruction.

        Returns:
            ``Schedule`` consisting of instructions that matches with filtering condition.

        Raises:
            PulseError: When this method is called. This method will be supported soon.
        """
        raise PulseError(
            "Method ``ScheduleBlock.filter`` is not supported as this program "
            "representation does not have the notion of an explicit instruction "
            "time. Apply ``qiskit.pulse.transforms.block_to_schedule`` function to "
            "this program to obtain the ``Schedule`` representation supporting "
            "this method."
        )

    def exclude(
        self,
        *filter_funcs: List[Callable],
        channels: Optional[Iterable[Channel]] = None,
        instruction_types: Union[Iterable[abc.ABCMeta], abc.ABCMeta] = None,
        time_ranges: Optional[Iterable[Tuple[int, int]]] = None,
        intervals: Optional[Iterable[Interval]] = None,
        check_subroutine: bool = True,
    ):
        """Return a ``Schedule`` with only the instructions from this Schedule *failing*
        at least one of the provided filters.
        This method is the complement of py:meth:`~self.filter`, so that::

            self.filter(args) | self.exclude(args) == self

        .. note:: This method is currently not supported. Support will be soon added
            please create an issue if you believe this must be prioritized.

        Args:
            filter_funcs: A list of Callables which take a (int, Union['Schedule', Instruction])
                tuple and return a bool.
            channels: For example, ``[DriveChannel(0), AcquireChannel(0)]``.
            instruction_types: For example, ``[PulseInstruction, AcquireInstruction]``.
            time_ranges: For example, ``[(0, 5), (6, 10)]``.
            intervals: For example, ``[(0, 5), (6, 10)]``.
            check_subroutine: Set `True` to individually filter instructions inside of a subroutine
                defined by the :py:class:`~qiskit.pulse.instructions.Call` instruction.

        Returns:
            ``Schedule`` consisting of instructions that are not match with filtering condition.

        Raises:
            PulseError: When this method is called. This method will be supported soon.
        """
        raise PulseError(
            "Method ``ScheduleBlock.exclude`` is not supported as this program "
            "representation does not have the notion of instruction "
            "time. Apply ``qiskit.pulse.transforms.block_to_schedule`` function to "
            "this program to obtain the ``Schedule`` representation supporting "
            "this method."
        )

    def replace(
        self,
        old: "BlockComponent",
        new: "BlockComponent",
        inplace: bool = True,
    ) -> "ScheduleBlock":
        """Return a ``ScheduleBlock`` with the ``old`` component replaced with a ``new``
        component.

        Args:
            old: Schedule block component to replace.
            new: Schedule block component to replace with.
            inplace: Replace instruction by mutably modifying this ``ScheduleBlock``.

        Returns:
            The modified schedule block with ``old`` replaced by ``new``.
        """
        from qiskit.pulse.parameter_manager import ParameterManager

        new_blocks = []
        new_parameters = ParameterManager()

        for block in self.blocks:
            if block == old:
                new_blocks.append(new)
                new_parameters.update_parameter_table(new)
            else:
                if isinstance(block, ScheduleBlock):
                    new_blocks.append(block.replace(old, new, inplace))
                else:
                    new_blocks.append(block)
                new_parameters.update_parameter_table(block)

        if inplace:
            self._blocks = new_blocks
            self._parameter_manager = new_parameters
            return self
        else:
            ret_block = copy.deepcopy(self)
            ret_block._blocks = new_blocks
            ret_block._parameter_manager = new_parameters
            return ret_block

    def is_parameterized(self) -> bool:
        """Return True iff the instruction is parameterized."""
        return self._parameter_manager.is_parameterized()

    def assign_parameters(
        self, value_dict: Dict[ParameterExpression, ParameterValueType], inplace: bool = True
    ) -> "ScheduleBlock":
        """Assign the parameters in this schedule according to the input.

        Args:
            value_dict: A mapping from Parameters to either numeric values or another
                Parameter expression.
            inplace: Set ``True`` to override this instance with new parameter.

        Returns:
            Schedule with updated parameters.
        """
        return self._parameter_manager.assign_parameters(
            pulse_program=self, value_dict=value_dict, inplace=inplace
        )

    def get_parameters(self, parameter_name: str) -> List[Parameter]:
        """Get parameter object bound to this schedule by string name.

        Because different ``Parameter`` objects can have the same name,
        this method returns a list of ``Parameter`` s for the provided name.

        Args:
            parameter_name: Name of parameter.

        Returns:
            Parameter objects that have corresponding name.
        """
        return self._parameter_manager.get_parameters(parameter_name)

    def __len__(self) -> int:
        """Return number of instructions in the schedule."""
        return len(self.blocks)

    def __eq__(self, other: "ScheduleBlock") -> bool:
        """Test if two ScheduleBlocks are equal.

        Equality is checked by verifying there is an equal instruction at every time
        in ``other`` for every instruction in this ``ScheduleBlock``. This check is
        performed by converting the instruction representation into directed acyclic graph,
        in which execution order of every instruction is evaluated correctly across all channels.
        Also ``self`` and ``other`` should have the same alignment context.

        .. warning::

            This does not check for logical equivalency. Ie.,

            ```python
            >>> Delay(10, DriveChannel(0)) + Delay(10, DriveChannel(0))
                == Delay(20, DriveChannel(0))
            False
            ```
        """
        # 0. type check
        if not isinstance(other, type(self)):
            return False

        # 1. transformation check
        if self.alignment_context != other.alignment_context:
            return False

        # 2. channel check
        if set(self.channels) != set(other.channels):
            return False

        # 3. size check
        if len(self) != len(other):
            return False

        # 4. instruction check
        import retworkx as rx
        from qiskit.pulse.transforms import block_to_dag

        return rx.is_isomorphic_node_match(
            block_to_dag(self), block_to_dag(other), lambda x, y: x == y
        )

    def __repr__(self) -> str:
        name = format(self._name) if self._name else ""
        blocks = ", ".join([repr(instr) for instr in self.blocks[:50]])
        if len(self.blocks) > 25:
            blocks += ", ..."
        return '{}({}, name="{}", transform={})'.format(
            self.__class__.__name__, blocks, name, repr(self.alignment_context)
        )

    def __add__(self, other: "BlockComponent") -> "ScheduleBlock":
        """Return a new schedule with ``other`` inserted within ``self`` at ``start_time``."""
        return self.append(other)


def _common_method(*classes):
    """A function decorator to attach the function to specified classes as a method.

    .. note:: For developer: A method attached through this decorator may hurt readability
        of the codebase, because the method may not be detected by a code editor.
        Thus, this decorator should be used to a limited extent, i.e. huge helper method.
        By using this decorator wisely, we can reduce code maintenance overhead without
        losing readability of the codebase.
    """

    def decorator(method):
        @functools.wraps(method)
        def wrapper(*args, **kwargs):
            return method(*args, **kwargs)

        for cls in classes:
            setattr(cls, method.__name__, wrapper)
        return method

    return decorator


@_common_method(Schedule, ScheduleBlock)
def draw(
    self,
    style: Optional[Dict[str, Any]] = None,
    backend=None,  # importing backend causes cyclic import
    time_range: Optional[Tuple[int, int]] = None,
    time_unit: str = "dt",
    disable_channels: Optional[List[Channel]] = None,
    show_snapshot: bool = True,
    show_framechange: bool = True,
    show_waveform_info: bool = True,
    show_barrier: bool = True,
    plotter: str = "mpl2d",
    axis: Optional[Any] = None,
):
    """Plot the schedule.

    Args:
        style: Stylesheet options. This can be dictionary or preset stylesheet classes. See
            :py:class:`~qiskit.visualization.pulse_v2.stylesheets.IQXStandard`,
            :py:class:`~qiskit.visualization.pulse_v2.stylesheets.IQXSimple`, and
            :py:class:`~qiskit.visualization.pulse_v2.stylesheets.IQXDebugging` for details of
            preset stylesheets.
        backend (Optional[BaseBackend]): Backend object to play the input pulse program.
            If provided, the plotter may use to make the visualization hardware aware.
        time_range: Set horizontal axis limit. Tuple `(tmin, tmax)`.
        time_unit: The unit of specified time range either `dt` or `ns`.
            The unit of `ns` is available only when `backend` object is provided.
        disable_channels: A control property to show specific pulse channel.
            Pulse channel instances provided as a list are not shown in the output image.
        show_snapshot: Show snapshot instructions.
        show_framechange: Show frame change instructions. The frame change represents
            instructions that modulate phase or frequency of pulse channels.
        show_waveform_info: Show additional information about waveforms such as their name.
        show_barrier: Show barrier lines.
        plotter: Name of plotter API to generate an output image.
            One of following APIs should be specified::

                mpl2d: Matplotlib API for 2D image generation.
                    Matplotlib API to generate 2D image. Charts are placed along y axis with
                    vertical offset. This API takes matplotlib.axes.Axes as ``axis`` input.

            ``axis`` and ``style`` kwargs may depend on the plotter.
        axis: Arbitrary object passed to the plotter. If this object is provided,
            the plotters use a given ``axis`` instead of internally initializing
            a figure object. This object format depends on the plotter.
            See plotter argument for details.

    Returns:
        Visualization output data.
        The returned data type depends on the ``plotter``.
        If matplotlib family is specified, this will be a ``matplotlib.pyplot.Figure`` data.
    """
    # pylint: disable=cyclic-import, missing-return-type-doc
    from qiskit.visualization import pulse_drawer_v2

    return pulse_drawer_v2(
        program=self,
        style=style,
        backend=backend,
        time_range=time_range,
        time_unit=time_unit,
        disable_channels=disable_channels,
        show_snapshot=show_snapshot,
        show_framechange=show_framechange,
        show_waveform_info=show_waveform_info,
        show_barrier=show_barrier,
        plotter=plotter,
        axis=axis,
    )


def _interval_index(intervals: List[Interval], interval: Interval) -> int:
    """Find the index of an interval.

    Args:
        intervals: A sorted list of non-overlapping Intervals.
        interval: The interval for which the index into intervals will be found.

    Returns:
        The index of the interval.

    Raises:
        PulseError: If the interval does not exist.
    """
    index = _locate_interval_index(intervals, interval)
    found_interval = intervals[index]
    if found_interval != interval:
        raise PulseError(f"The interval: {interval} does not exist in intervals: {intervals}")
    return index


def _locate_interval_index(intervals: List[Interval], interval: Interval, index: int = 0) -> int:
    """Using binary search on start times, find an interval.

    Args:
        intervals: A sorted list of non-overlapping Intervals.
        interval: The interval for which the index into intervals will be found.
        index: A running tally of the index, for recursion. The user should not pass a value.

    Returns:
        The index into intervals that new_interval would be inserted to maintain
        a sorted list of intervals.
    """
    if not intervals or len(intervals) == 1:
        return index

    mid_idx = len(intervals) // 2
    mid = intervals[mid_idx]
    if interval[1] <= mid[0] and (interval != mid):
        return _locate_interval_index(intervals[:mid_idx], interval, index=index)
    else:
        return _locate_interval_index(intervals[mid_idx:], interval, index=index + mid_idx)


def _find_insertion_index(intervals: List[Interval], new_interval: Interval) -> int:
    """Using binary search on start times, return the index into `intervals` where the new interval
    belongs, or raise an error if the new interval overlaps with any existing ones.
    Args:
        intervals: A sorted list of non-overlapping Intervals.
        new_interval: The interval for which the index into intervals will be found.
    Returns:
        The index into intervals that new_interval should be inserted to maintain a sorted list
        of intervals.
    Raises:
        PulseError: If new_interval overlaps with the given intervals.
    """
    index = _locate_interval_index(intervals, new_interval)
    if index < len(intervals):
        if _overlaps(intervals[index], new_interval):
            raise PulseError("New interval overlaps with existing.")
        return index if new_interval[1] <= intervals[index][0] else index + 1
    return index


def _overlaps(first: Interval, second: Interval) -> bool:
    """Return True iff first and second overlap.
    Note: first.stop may equal second.start, since Interval stop times are exclusive.
    """
    if first[0] == second[0] == second[1]:
        # They fail to overlap if one of the intervals has duration 0
        return False
    if first[0] > second[0]:
        first, second = second, first
    return second[0] < first[1]


def _check_nonnegative_timeslot(timeslots: TimeSlots):
    """Test that a channel has no negative timeslots.

    Raises:
        PulseError: If a channel timeslot is negative.
    """
    for chan, chan_timeslots in timeslots.items():
        if chan_timeslots:
            if chan_timeslots[0][0] < 0:
                raise PulseError(f"An instruction on {chan} has a negative starting time.")


def _get_timeslots(schedule: "ScheduleComponent") -> TimeSlots:
    """Generate timeslots from given schedule component.

    Args:
        schedule: Input schedule component.

    Raises:
        PulseError: When invalid schedule type is specified.
    """
    if isinstance(schedule, Instruction):
        duration = schedule.duration
        instruction_duration_validation(duration)
        timeslots = {channel: [(0, duration)] for channel in schedule.channels}
    elif isinstance(schedule, Schedule):
        timeslots = schedule.timeslots
    else:
        raise PulseError(f"Invalid schedule type {type(schedule)} is specified.")

    return timeslots


# These type aliases are defined at the bottom of the file, because as of 2022-01-18 they are
# imported into other parts of Terra.  Previously, the aliases were at the top of the file and used
# forwards references within themselves.  This was fine within the same file, but causes scoping
# issues when the aliases are imported into different scopes, in which the `ForwardRef` instances
# would no longer resolve.  Instead, we only use forward references in the annotations of _this_
# file to reference the aliases, which are guaranteed to resolve in scope, so the aliases can all be
# concrete.

ScheduleComponent = Union[Schedule, Instruction]
"""An element that composes a pulse schedule."""

BlockComponent = Union[ScheduleBlock, Instruction]
"""An element that composes a pulse schedule block."""
