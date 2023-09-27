# Copyright (C) 2012 Matthew Hampton, 2023 Sartography
#
# This file is part of SpiffWorkflow.
#
# SpiffWorkflow is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 3.0 of the License, or (at your option) any later version.
#
# SpiffWorkflow is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301  USA

from SpiffWorkflow.task import Task
from SpiffWorkflow.util.task import TaskState, TaskFilter, TaskIterator
from SpiffWorkflow.workflow import Workflow
from SpiffWorkflow.exceptions import WorkflowException, TaskNotFoundException

from SpiffWorkflow.bpmn.specs.mixins.events.event_types import CatchingEvent
from SpiffWorkflow.bpmn.specs.mixins.events.start_event import StartEvent
from SpiffWorkflow.bpmn.specs.mixins.subworkflow_task import CallActivity

from SpiffWorkflow.bpmn.specs.control import BoundaryEventSplit
from .PythonScriptEngine import PythonScriptEngine


class BpmnTaskFilter(TaskFilter):

    def __init__(self, catches_event=None, lane=None, **kwargs):
        super().__init__(**kwargs)
        self.catches_event = catches_event
        self.lane = lane

    def matches(self, task):

        def _catches_event(task):
            return isinstance(task.task_spec, CatchingEvent) and task.task_spec.catches(task, self.catches_event)

        return all([
            super().matches(task),
            self.catches_event is None or _catches_event(task),
            self.lane is None or task.task_spec.lane == self.lane,
        ])


class BpmnTaskIterator(TaskIterator):

    def __init__(self, task, end_at_spec=None, max_depth=10000, depth_first=True, task_filter=None, **kwargs):

        task_filter = task_filter or BpmnTaskFilter(**kwargs)
        super().__init__(task, end_at_spec, max_depth, depth_first, task_filter)

    def _next(self):

        if len(self.task_list) == 0:
            raise StopIteration()

        task = self.task_list.pop(-1)
        subprocess = task.workflow.top_workflow.subprocesses.get(task.id)

        if all([
            len(task._children) > 0 or subprocess is not None,
            task.state >= self.min_state,
            self.depth < self.max_depth,
            task.task_spec.name != self.end_at_spec,
        ]):
            add_tasks = [t for t in reversed(task.children)]
            if self.depth_first:
                if subprocess is not None:
                    add_tasks.append(subprocess.task_tree)
                self.task_list.extend(add_tasks)
            else:
                if subprocess is not None:
                    add_tasks = [subprocess.task_tree] + add_tasks
                self.task_list = add_tasks + self.task_list
            self.depth += 1
        elif len(self.task_list) > 0 and task.parent != self.task_list[0].parent:
            self.depth -= 1
        return task


class BpmnSubWorkflow(Workflow):

    def __init__(self, spec, parent_task_id, top_workflow, **kwargs):
        super().__init__(spec, **kwargs)
        self.parent_task_id = parent_task_id
        self.top_workflow = top_workflow
        self.correlations = {}

    @property
    def script_engine(self):
        return self.top_workflow.script_engine

    @property
    def parent_workflow(self):
        task = self.top_workflow.get_task_from_id(self.parent_task_id)
        return task.workflow

    @property
    def depth(self):
        current, depth = self, 0
        while current.parent_workflow is not None:
            depth += 1
            current = current.parent_workflow
        return depth

    def get_task_from_id(self, task_id):
        try:
            return super().get_task_from_id(task_id)
        except TaskNotFoundException as exc:
            pass


class BpmnWorkflow(Workflow):
    """
    The engine that executes a BPMN workflow. This specialises the standard
    Spiff Workflow class with a few extra methods and attributes.
    """

    def __init__(self, spec, subprocess_specs=None, script_engine=None, **kwargs):
        """
        Constructor.

        :param script_engine: set to an extension of PythonScriptEngine if you
        need a specialised version. Defaults to the script engine of the top
        most workflow, or to the PythonScriptEngine if none is provided.
        """
        self.subprocess_specs = subprocess_specs or {}
        self.subprocesses = {}
        self.bpmn_events = []
        self.correlations = {}
        super(BpmnWorkflow, self).__init__(spec, **kwargs)

        self.__script_engine = script_engine or PythonScriptEngine()

    @property
    def script_engine(self):
        return self.__script_engine

    @script_engine.setter
    def script_engine(self, engine):
        self.__script_engine = engine

    @property
    def top_workflow(self):
        return self

    @property
    def parent_task_id(self):
        return None
    
    @property
    def parent_workflow(self):
        return None
    
    @property
    def depth(self):
        return 0

    def get_tasks_iterator(self, first_task=None, **kwargs):
        return BpmnTaskIterator(first_task or self.task_tree, **kwargs)

    def create_subprocess(self, my_task, spec_name):
        # This creates a subprocess for an existing task
        subprocess = BpmnSubWorkflow(
            self.subprocess_specs[spec_name],
            parent_task_id=my_task.id,
            top_workflow=self)
        self.subprocesses[my_task.id] = subprocess
        return subprocess

    def get_subprocess(self, my_task):
        return self.subprocesses.get(my_task.id)

    def delete_subprocess(self, my_task):
        subprocess = self.subprocesses.get(my_task.id)
        tasks = subprocess.get_tasks()
        for sp in [c for c in self.subprocesses.values() if c.parent_workflow == subprocess]:
            tasks.extend(self.delete_subprocess(self.get_task_from_id(sp.parent_task_id)))
        del self.subprocesses[my_task.id]
        return tasks

    def get_active_subprocesses(self):
        return [sp for sp in self.subprocesses.values() if not sp.is_completed()]

    def catch(self, event):
        """
        Tasks can always catch events, regardless of their state.  The event information is stored in the task's
        internal data and processed when the task is reached in the workflow.  If a task should only receive messages
        while it is running (eg a boundary event), the task should call the event_definition's reset method before
        executing to clear out a stale message.

        :param event: the thrown event
        """
        if event.target is None:
            self.update_collaboration(event)
            tasks = self.get_tasks(spec_class=CatchingEvent, catches_event=event)
            # Figure out if we need to create an external event
            if len(tasks) == 0:
                self.bpmn_events.append(event)
        else:
            tasks = self.get_tasks(state=TaskState.NOT_FINISHED_MASK, catches_event=event)

        for task in tasks:
            task.task_spec.catch(task, event)

        self.refresh_waiting_tasks()

    def send_event(self, event):
        """Allows this workflow to catch an externally generated event."""

        tasks = self.get_tasks(spec_class=CatchingEvent, catches_event=event)
        if len(tasks) == 0:
            raise WorkflowException(f"This process is not waiting for {event.event_definition.name}")
        for task in tasks:
            task.task_spec.catch(task, event)

        self.refresh_waiting_tasks()

    def get_events(self):
        """Returns the list of events that cannot be handled from within this workflow."""
        events = self.bpmn_events
        self.bpmn_events = []
        return events

    def waiting_events(self):
        iter = self.get_tasks_iterator(state=TaskState.WAITING, spec_class=CatchingEvent)
        return [t.task_spec.event_definition.details(t) for t in iter]

    def do_engine_steps(self, will_complete_task=None, did_complete_task=None):
        """
        Execute any READY tasks that are engine specific (for example, gateways
        or script tasks). This is done in a loop, so it will keep completing
        those tasks until there are only READY User tasks, or WAITING tasks
        left.

        :param will_complete_task: Callback that will be called prior to completing a task
        :param did_complete_task: Callback that will be called after completing a task
        """
        def update_workflow(wf):
            count = 0
            # Wanted to use the iterator method here, but at least this is a shorter list
            for task in wf.get_tasks(state=TaskState.READY):
                if not task.task_spec.manual:
                    if will_complete_task is not None:
                        will_complete_task(task)
                    task.run()
                    count += 1
                    if did_complete_task is not None:
                        did_complete_task(task)
            return count

        active_subprocesses = self.get_active_subprocesses()
        for subprocess in sorted(active_subprocesses, key=lambda v: v.depth, reverse=True):
            count = None
            while count is None or count > 0:
                count = update_workflow(subprocess)
            if subprocess.parent_task_id is not None:
                task = self.get_task_from_id(subprocess.parent_task_id)
                task.task_spec._update(task)

        count = update_workflow(self)
        if count > 0 or len(self.get_active_subprocesses()) > len(active_subprocesses):
            self.do_engine_steps(will_complete_task, did_complete_task)

    def refresh_waiting_tasks(self, will_refresh_task=None, did_refresh_task=None):
        """
        Refresh the state of all WAITING tasks. This will, for example, update
        Catching Timer Events whose waiting time has passed.

        :param will_refresh_task: Callback that will be called prior to refreshing a task
        :param did_refresh_task: Callback that will be called after refreshing a task
        """
        def update_task(task):
            if will_refresh_task is not None:
                will_refresh_task(task)
            task.task_spec._update(task)
            if did_refresh_task is not None:
                did_refresh_task(task)           
 
        for subprocess in sorted(self.get_active_subprocesses(), key=lambda v: v.depth, reverse=True):
            for task in subprocess.get_tasks_iterator(state=TaskState.WAITING):
                update_task(task)

        for task in self.get_tasks_iterator(state=TaskState.WAITING):
            update_task(task)

    def get_task_from_id(self, task_id):
        for subprocess in self.subprocesses.values():
            task = subprocess.get_task_from_id(task_id)
            if task is not None:
                return task
        return super().get_task_from_id(task_id)

    def reset_from_task_id(self, task_id, data=None, remove_subprocess=True):

        task = self.get_task_from_id(task_id)
        run_task_at_end = False
        if isinstance(task.parent.task_spec, BoundaryEventSplit):
            task = task.parent
            run_task_at_end = True # we jumped up one level, so exectute so we are on the correct task as requested.

        descendants = []
        # Since recursive deletion of subprocesses requires access to the tasks, we have to delete any subprocesses first
        # We also need diffeent behavior for the case where we explictly reset to a subprocess (in which case we delete it)
        # vs resetting inside (where we leave it and reset the tasks that descend from it)
        for item in task:
            if item == task and not remove_subprocess:
                continue
            if item.id in self.subprocesses:
                descendants.extend(self.delete_subprocess(item))
        descendants.extend(super().reset_from_task_id(task.id, data))

        if task.workflow.parent_task_id is not None:
            sp_task = self.get_task_from_id(task.workflow.parent_task_id)
            descendants.extend(self.reset_from_task_id(sp_task.id, remove_subprocess=False))
            sp_task._set_state(TaskState.WAITING)

        if run_task_at_end:
            task.run()

        return descendants

    def cancel(self, workflow=None):

        wf = workflow or self
        cancelled = Workflow.cancel(wf)
        cancelled_ids = [t.id for t in cancelled]
        to_cancel = []
        for sp_id, sp in self.subprocesses.items():
            if sp_id in cancelled_ids:
                to_cancel.append(sp)

        for sp in to_cancel:
            cancelled.extend(self.cancel(sp))

        return cancelled

    def update_collaboration(self, event):

        def get_or_create_subprocess(task_spec, wf_spec):

            for sp in self.subprocesses.values():
                if sp.get_next_task(state=TaskState.WAITING, spec_name=task_spec.name) is not None:
                    return sp

            # This creates a new task associated with a process when an event that kicks of a process is received
            # I need to know what class is being used to create new processes in this case, and this seems slightly
            # less bad than adding yet another argument.  Still sucks though.
            # TODO: Make collaborations a class rather than trying to shoehorn them into a process.
            for spec in self.spec.task_specs.values():
                if isinstance(spec, CallActivity):
                    spec_class = spec.__class__
                    break
            else:
                # Default to the mixin class, which will probably fail in many cases.
                spec_class = CallActivity

            new = spec_class(self.spec, f'{wf_spec.name}_{len(self.subprocesses)}', wf_spec.name)
            self.spec.start.connect(new)
            task = Task(self, new, parent=self.task_tree)
            # This (indirectly) calls create_subprocess
            task.task_spec._update(task)
            return self.subprocesses[task.id]

        # Start a subprocess for known specs with start events that catch this
        for spec in self.subprocess_specs.values():
            for task_spec in spec.task_specs.values():
                if isinstance(task_spec, StartEvent) and task_spec.event_definition == event.event_definition:
                    subprocess = get_or_create_subprocess(task_spec, spec)
                    subprocess.correlations.update(event.correlations)
