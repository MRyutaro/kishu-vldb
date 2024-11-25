"""
Kishu-Jupyter Integration Module (2nd version)

# Basic Usage

```
from kishu import load_kishu
load_kishu()
```
Then, all the cell executions are recorded, and the result of each cell execution is checkpointed.


# Working with Kishu

load_kishu() adds a new variable `_kishu` (of type KishuJupyterExecHistory) to Jupyter's namespace.
The special variable can be used for kishu-related operations, as follows:
1. browse the history: _kishu.log()
2. see the database file: _kishu.database_path()
3. restore a state: _kishu.checkout(commit_id)

*Note:* currently, "restore" is limited to restoring a variable state, not including code state.

# New checkpoint file for each Python kernel process.

A new database file is created for each load_kishu(). In the same session, invoking load_kishu()
multiple times returns the same singleton instance. In a new session, the function will return
a different instance (associated with a different checkpoint file).

Restoring a checkpointed state means that we are restoring some of the variables from an old state
into the current state. Thus, the kishu object remains the same before and after any checkpointing/
restoration operations; only the current state (or the variables inside it) changes.

(Not implemented yet)
In order to give an impression that we are actually reviving an old state, kishu also manages
IPython's database including execution count and cell code.


Reference
- https://ipython.readthedocs.io/en/stable/config/callbacks.html
"""
from __future__ import annotations

import contextlib
import dill as pickle
import IPython
import io
import ipylab
import json
import jupyter_client
import nbformat
import os
import time
import uuid
import sys

from dataclasses import dataclass
from IPython.core.interactiveshell import InteractiveShell
from jupyter_ui_poll import run_ui_poll_loop
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kishu.exceptions import (
    JupyterConnectionError,
    KernelNotAliveError,
    MissingConnectionInfoError,
    MissingNotebookMetadataError,
    NoChannelError,
    PostWithoutPreError,
    StartChannelError,
)
from kishu.jupyter.namespace import Namespace
from kishu.jupyter.runtime import JupyterRuntimeEnv
from kishu.notebook_id import NotebookId
from kishu.planning.ahg import AHG, VersionedName
from kishu.planning.plan import RestorePlan
from kishu.planning.planner import CheckpointRestorePlanner, ChangedVariables
from kishu.planning.variable_version_tracker import VariableVersionTracker
from kishu.storage.branch import KishuBranch
from kishu.storage.checkpoint import KishuCheckpoint
from kishu.storage.commit import CommitEntry, CommitEntryKind, FormattedCell, KishuCommit
from kishu.storage.commit_graph import KishuCommitGraph
from kishu.storage.config import Config
from kishu.storage.path import KishuPath
from kishu.storage.tag import KishuTag
from kishu.storage.variable_version import VariableVersion

"""
Functions to find enclosing notebook name, distilled From ipynbname.
"""


def enclosing_platform() -> str:
    app = ipylab.JupyterFrontEnd()
    num_trials = 10

    def app_commands_fn():
        nonlocal num_trials
        if app.commands.list_commands() == [] and num_trials > 0:
            num_trials -= 1
            return None
        return app.commands.list_commands()

    # To fetch the command list, we need to unblock the frontend through polling loop.
    try:
        app_commands = run_ui_poll_loop(app_commands_fn)
        if "docmanager:save" in app_commands:
            # In JupyterLab.
            return "jupyterlab"
    except Exception:
        # BUG: run_ui_poll_loop throws when not in a ipython kernel.
        pass

    # In Jupyter Notebook.
    return "jupyternb"


class BareReprStr(str):

    def __init__(self, s: str):
        self.s = s

    def __repr__(self):
        return self.s


"""
Notebook instrument.
"""


@dataclass
class KishuSession:
    notebook_key: str
    kernel_id: Optional[str]
    notebook_path: Optional[str]
    is_alive: bool


@dataclass
class JupyterCommandResult:
    status: str
    message: str


class JupyterConnection:
    def __init__(self, kernel_id: str) -> None:
        self.kernel_id = kernel_id
        self.km: Optional[jupyter_client.BlockingKernelClient] = None

    @staticmethod
    def from_notebook_key(notebook_key: str) -> JupyterConnection:
        # Find connection information
        conn_info = NotebookId.try_retrieve_connection(notebook_key)
        if conn_info is None:
            raise MissingConnectionInfoError()
        return JupyterConnection(conn_info.kernel_id)

    def __enter__(self) -> JupyterConnection:
        # Find connection file.
        try:
            cf = jupyter_client.find_connection_file(self.kernel_id)
        except OSError:
            raise KernelNotAliveError()

        # Connect to kernel.
        self.km = jupyter_client.BlockingKernelClient(connection_file=cf)
        self.km.load_connection_file()
        self.km.start_channels()
        self.km.wait_for_ready()
        if not self.km.is_alive():
            self.km = None
            raise StartChannelError()

        return self

    def execute(self, command: str, pre_command: str = "") -> Tuple[Dict[str, Any], str, str]:
        if self.km is None:
            raise NoChannelError()
        with contextlib.redirect_stdout(io.StringIO()) as stdout_f, \
             contextlib.redirect_stderr(io.StringIO()) as stderr_f:
            reply = self.km.execute_interactive(
                pre_command,  # Not capture output.
                user_expressions={"command_result": command},  # To get output from command.
                silent=True,  # Do not increment cell count and trigger pre/post_run_cell hooks.
            )
            stdout = stdout_f.getvalue()
            stderr = stderr_f.getvalue()
        # print("command:", command)
        # print("**************************")
        # print(f"stdout>\n{stdout}")
        # print("**************************")
        # print(f"stderr>\n{stderr}")
        # print("**************************")
        return reply, stdout, stderr

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.km is not None:
            self.km.stop_channels()
            self.km = None

    def execute_one_command(self, command: str, pre_command: str = "") -> JupyterCommandResult:
        try:
            with self as conn:
                reply, _, _ = conn.execute(command, pre_command=pre_command)
        except JupyterConnectionError as e:
            return JupyterCommandResult(
                status="error",
                message=str(e),
            )

        # Handle unexpected status.
        if reply["content"]["status"] == "error":
            # print("\n".join(reply["content"]["traceback"]))
            ename = reply["content"]["ename"]
            evalue = reply["content"]["evalue"]
            return JupyterCommandResult(
                status="error",
                message=f"{ename}: {evalue}",
            )
        elif reply["content"]["status"] != "ok":
            return JupyterCommandResult(
                status=reply["content"]["status"],
                message=json.dumps(reply["content"]),
            )

        # Reply status is ok.
        command_result = reply["content"].get("user_expressions", {}).get("command_result", {})
        command_result_status = command_result.get("status", "")
        if command_result_status == 'error':
            ename = command_result["ename"]
            evalue = command_result["evalue"]
            return JupyterCommandResult(
                status="error",
                message=f"{ename}: {evalue}",
            )
        elif command_result_status == 'ok':
            command_result_data = command_result.get("data", {}).get("text/plain", "")
            return JupyterCommandResult(
                status="ok",
                message=command_result_data,
            )
        else:
            return JupyterCommandResult(
                status="ok",
                message=f"Executed {command} but no result.",
            )


class KishuForJupyter:
    CURRENT_CELL_ID = 'current'
    SAVE_CMD = "try { IPython.notebook.save_checkpoint(); } catch { }"
    # RELOAD_CMD = "try { IPython.notebook.load_notebook(IPython.notebook.notebook_path); } catch { }"
    RELOAD_CMD = "try { location.reload(true); } catch { }"  # will ask confirmation
    ENV_KISHU_TEST_MODE = "ENV_KISHU_TEST_MODE"

    def __init__(
        self,
        notebook_id: NotebookId,
        ip: InteractiveShell,
    ) -> None:
        # Kishu info and storages.
        self._notebook_id = notebook_id
        self._kishu_commit = KishuCommit(self._notebook_id.key())
        self._kishu_checkpoint = KishuCheckpoint(self.database_path())
        self._kishu_branch = KishuBranch(self._notebook_id.key())
        self._kishu_tag = KishuTag(self._notebook_id.key())
        self._kishu_graph: KishuCommitGraph = KishuCommitGraph.new_on_file(
            KishuPath.commit_graph_directory(self._notebook_id.key())
        )
        self._kishu_variable_version = VariableVersion(self._notebook_id.key())

        # Enclosing environment.
        self._ip = ip
        self._user_ns = Namespace(self._ip.user_ns)

        # Patch global and local namespace to monitor variable accesses.
        self._ip.init_create_namespaces(user_module=None, user_ns=self._user_ns.get_tracked_namespace())

        self._platform = enclosing_platform()
        self._session_id = 0

        # Stateful trackers.
        self._cr_planner = CheckpointRestorePlanner.from_existing(self._user_ns)
        self._variable_version_tracker = VariableVersionTracker({})
        self._start_time: Optional[float] = None
        self._last_execution_count = 0

        # Configurations.
        self._test_mode = Config.get('JUPYTERINT', 'test_mode', False)
        self._commit_id_mode = Config.get('JUPYTERINT', 'commit_id_mode', 'uuid4')
        self._enable_auto_branch = Config.get('JUPYTERINT', 'enable_auto_branch', True)
        self._enable_auto_commit_when_skip_notebook = Config.get(
            'JUPYTERINT',
            'enable_auto_commit_when_skip_notebook',
            True)

        # Initialize databases.
        self._kishu_commit.init_database()
        self._kishu_checkpoint.init_database()
        self._kishu_branch.init_database()
        self._kishu_tag.init_database()
        self._kishu_variable_version.init_database()
        self._notebook_id.record_connection()

        # For unit tests.
        if os.environ.get(KishuForJupyter.ENV_KISHU_TEST_MODE, False):
            self._test_mode = True
            self._commit_id_mode = "counter"
        self.total_commit_size = 0

    def __str__(self):
        return (
            "KishuForJupyter("
            f"id: {self._notebook_id.key()}, "
            f"path: {self._notebook_id.path()})"
        )

    def __repr__(self):
        return (
            "KishuForJupyter("
            f"notebook_id: {self._notebook_id.key()}, "
            f"kernel_id: {self._notebook_id.kernel_id()}, "
            f"notebook_path: {self._notebook_id.path()}, "
            f"session_id: {self._session_id}, "
            f"platform: {self._platform}, "
            f"commit_id_mode: {self._commit_id_mode})"
        )

    def set_session_id(self, session_id):
        self._session_id = session_id

    def database_path(self) -> str:
        return KishuPath.database_path(self._notebook_id.key())

    def install_kishu_hooks(self) -> None:
        self._ip.user_ns[KISHU_INSTRUMENT] = self
        self._ip.events.register('pre_run_cell', self.pre_run_cell)
        self._ip.events.register('post_run_cell', self.post_run_cell)

    def uninstall_kishu_hooks(self) -> None:
        """
        Removes event handlers added by load_kishu
        """
        try:
            self._ip.events.unregister('post_run_cell', self.post_run_cell)
        except ValueError:
            pass
        try:
            self._ip.events.unregister('pre_run_cell', self.pre_run_cell)
        except ValueError:
            pass
        del self._ip.user_ns[KISHU_INSTRUMENT]

    def save_notebook(self) -> None:
        if self._test_mode:  # TODO: re-enable notebook saving during tests when possible/supported.
            return
        nb_path = self._notebook_id.path()

        # Remember starting state.
        start_mtime = os.path.getmtime(nb_path)
        current_mtime = start_mtime

        # Issue save command.
        if self._platform == "jupyterlab":
            # In JupyterLab.
            KishuForJupyter._ipylab_frontend_app().commands.execute("docmanager:save")
        else:
            # In Jupyter Notebook.
            IPython.display.display(IPython.display.Javascript(KishuForJupyter.SAVE_CMD))

        # Now wait for the saving to change the notebook.
        sleep_t = 0.2
        time.sleep(sleep_t)
        while start_mtime == current_mtime and sleep_t < 1.0:
            current_mtime = os.path.getmtime(nb_path)
            sleep_t *= 1.2
            time.sleep(sleep_t)
        if sleep_t >= 1.0:
            print("WARNING: Notebook saving is taking too long. Kishu may not capture every cell.")

    def reload_jupyter_frontend(self):
        if self._test_mode:  # TODO: enable after unit test jupyter has frontend component.
            return
        if self._platform == "jupyterlab":
            # In JupyterLab.
            KishuForJupyter._ipylab_frontend_app().commands.execute("docmanager:reload")
        else:
            # In Jupyter Notebook.
            IPython.display.display(IPython.display.Javascript(KishuForJupyter.RELOAD_CMD))

    def checkout(self, branch_or_commit_id: str, skip_notebook: bool = False) -> BareReprStr:
        """
        Restores a variable state from commit_id.
        """
        start = time.time()
        commit_id = branch_or_commit_id
        # By default, checkout at commit ID in detach mode.
        branch_name: Optional[str] = None
        commit_id = branch_or_commit_id
        is_detach = True

        # Attempt to interpret target as a branch.
        retrieved_branches = self._kishu_branch.get_branch(branch_or_commit_id)
        if len(retrieved_branches) == 1:
            assert retrieved_branches[0].branch_name == branch_or_commit_id
            branch_name = retrieved_branches[0].branch_name
            commit_id = retrieved_branches[0].commit_id
            is_detach = False
        

        # Retrieve checkout plan.
        database_path = self.database_path()
        commit_id = KishuForJupyter.disambiguate_commit(self._notebook_id.key(), commit_id)
        self._cr_planner.write_row("pre-checkout-time1", time.time() - start)
        commit_entry_active_vses_string = self._kishu_commit.get_session_state(commit_id)
        # commit_entry = self._kishu_commit.get_commit(commit_id)
        # if commit_entry.restore_plan is None:
        #     raise ValueError("No restore plan found for commit_id = {}".format(commit_id))

        # # Reset ipython kernel.
        # assert self._ip is not None
        # if self._ip.history_manager is not None:
        #     self._ip.history_manager.reset(new_session=True)

        # # Restore notebook cells.
        # if not skip_notebook and commit_entry.raw_nb is not None:
        #     self._checkout_notebook(commit_entry.raw_nb)

        # # Restore list of executed cells.
        # if commit_entry.executed_cells is not None:
        #     current_executed_cells = self._user_ns.ipython_in()
        #     if current_executed_cells is not None:
        #         current_executed_cells[:] = commit_entry.executed_cells[:]

        # # Restore execution count.
        # if commit_entry.execution_count is not None:
        #     self._ip.execution_count = commit_entry.execution_count + 1  # _ip.execution_count is the next count.

        # Run the restore plan and update C/R planner with AHG from checkpoint file and new namespace.
        #REVISION-----
        #if commit_entry.active_vses_string is None:
        #    raise ValueError("No Active VSes found for commit_id = {}".format(commit_id))
        # if commit_entry.ahg_string is None:
        #    raise ValueError("No Application History Graph found for commit_id = {}".format(commit_id))

        # Find the lowest common ancestor of current and target commit.
        current_commit_id = self._kishu_graph.head()
        if Config.get('PLANNER', 'incremental_cr', False):
            lca_commit = self._kishu_graph.get_common_ancestor(commit_id, current_commit_id)
            if lca_commit:
                # lca_commit_entry = self._kishu_commit.get_commit(lca_commit)
                # #REVISION--
                # if lca_commit_entry.active_vses_string is None:
                #     raise ValueError("No Active VSes found for commit_id = {}".format(commit_id))
                # if lca_commit_entry.ahg_string is None:
                #     raise ValueError("No Application History Graph found for commit_id = {}".format(commit_id))
                # REVISION-----
                #lca_active_vses_string = lca_commit_entry.active_vses_string
                lca_active_vses_string = self._kishu_commit.get_session_state(lca_commit)
                # lca_ahg_string = lca_commit_entry.ahg_string

                #REVISION test
                target_active_vns = set(AHG.deserialize_active_vses(commit_entry_active_vses_string))
                lca_active_vns = set(AHG.deserialize_active_vses(lca_active_vses_string))
                my_active_vns = {VersionedName(vs.name, vs.version) for vs in self._cr_planner._ahg.get_active_variable_snapshots()}
                common_set = set()
                for vn in target_active_vns:
                    if vn in lca_active_vns and vn in my_active_vns:
                        common_set.add(vn)
                delta_set = target_active_vns.difference(common_set)
                parent_commit_ids = [node.commit_id for node in self._kishu_graph.list_history(commit_id)]
            else:
                #REVISION-----
                lca_active_vses_string = AHG.serialize_active_vses()
                # lca_ahg_string = AHG.serialize()

        self._cr_planner.write_row("pre-checkout-time", time.time() - start)
        

        # result_ns = self._cr_planner.restore_state(
        #     #REVISION---
        #     commit_entry.active_vses_string,
        #     commit_entry.restore_plan,
        #     database_path,
        #     commit_id,
        #     parent_commit_ids if Config.get('PLANNER', 'incremental_cr', False) else None,
        #     lca_active_vses_string if Config.get('PLANNER', 'incremental_cr', False) else None
        # )
        # self._checkout_namespace(self._user_ns, result_ns)

        # self._variable_version_tracker.set_current(self._kishu_variable_version.
        #                                            get_variable_version_by_commit_id(commit_id))

        # # Update Kishu heads.
        # self._kishu_graph.jump(commit_id)
        # self._kishu_branch.update_head(
        #     branch_name=branch_name,
        #     commit_id=commit_id,
        #     is_detach=is_detach,
        # )

        # # Create new commit when skip restoring notebook.
        # if self._enable_auto_commit_when_skip_notebook and skip_notebook:
        #     new_commit = self.commit(f"Checked out vars from {commit_entry.message}")
        #     return BareReprStr(f"Checkout {commit_id} only variables and commit {new_commit}.")

        if is_detach:
            return BareReprStr(f"Checkout {commit_id} in detach mode.")
        return BareReprStr(f"Checkout {branch_or_commit_id} ({commit_id}).")

    def pre_run_cell(self, info) -> None:
        """
        A hook invoked before running a cell.

        Example:
        print('info.raw_cell =', info.raw_cell)
        print('info.store_history =', info.store_history)
        print('info.silent =', info.silent)
        print('info.shell_futures =', info.shell_futures)
        print('info.cell_id =', info.cell_id)
        print(dir(info))
        """
        self._start_time = time.time()

        # Saving needs to be before cell execution, otherwise stream/print output will disappear.
        self.save_notebook()

        self._cr_planner.pre_run_cell_update()

    def post_run_cell(self, result) -> None:
        """
        A hook executed after the execution of each cell.

        Example:
        print('result.execution_count = ', result.execution_count)
        print('result.error_before_exec = ', result.error_before_exec)
        print('result.error_in_exec = ', result.error_in_exec)
        print('result.info = ', result.info)
        print('result.result = ', result.result)
        """
        entry = CommitEntry(kind=CommitEntryKind.jupyter)
        entry.execution_count = result.execution_count
        short_raw_cell = result.info.raw_cell if len(result.info.raw_cell) <= 40 else f"{result.info.raw_cell[:40]}..."
        entry.message = f"[{entry.execution_count}] {short_raw_cell}"

        # Jupyter-specific info for commit entry.
        entry.start_time = self._start_time
        entry.end_time = time.time()
        if entry.start_time is None:
            raise PostWithoutPreError()
        entry.raw_cell = result.info.raw_cell
        entry.error_before_exec = repr_if_not_none(result.error_before_exec)
        entry.error_in_exec = repr_if_not_none(result.error_in_exec)
        entry.result = repr_if_not_none(result.result)

        # Update optimization items.
        changed_vars = self._cr_planner.post_run_cell_update(entry.raw_cell, entry.end_time - entry.start_time)

        # Step forward internal data.
        self._last_execution_count += 1
        self._start_time = None

        self._commit_entry(entry, changed_vars)

    @staticmethod
    def kishu_sessions() -> List[KishuSession]:
        # List alive IPython sessions.
        alive_kernels = {session.kernel_id: session for session in JupyterRuntimeEnv.iter_sessions()}

        # List all Kishu sessions.
        sessions = []
        for notebook_key in KishuPath.iter_notebook_keys():
            cf = NotebookId.try_retrieve_connection(notebook_key)

            # Connection file not found.
            if cf is None:
                sessions.append(KishuSession(
                    notebook_key=notebook_key,
                    kernel_id=None,
                    notebook_path=None,
                    is_alive=False,
                ))
                continue

            # No matching alive kernel ID.
            if cf.kernel_id not in alive_kernels:
                sessions.append(KishuSession(
                    notebook_key=notebook_key,
                    kernel_id=cf.kernel_id,
                    notebook_path=cf.notebook_path,
                    is_alive=False,
                ))
                continue

            # No matching notebook with notebook key in its metadata.
            notebook_path = alive_kernels[cf.kernel_id].notebook_path
            written_notebook_key: Optional[str] = None
            try:
                written_notebook_key = NotebookId.parse_key_from_path(notebook_path)
            except (FileNotFoundError, MissingNotebookMetadataError):
                pass
            if notebook_key != written_notebook_key:
                sessions.append(KishuSession(
                    notebook_key=notebook_key,
                    kernel_id=cf.kernel_id,
                    notebook_path=cf.notebook_path,
                    is_alive=False,
                ))
                continue

            # Kernel ID is alive. Replace notebook path with the newest one.
            sessions.append(KishuSession(
                notebook_key=notebook_key,
                kernel_id=cf.kernel_id,
                notebook_path=str(notebook_path),
                is_alive=True,
            ))
        return sessions

    @staticmethod
    def disambiguate_commit(notebook_key: str, commit_id: str) -> str:
        kishu_commit = KishuCommit(notebook_key)
        possible_commit_ids = kishu_commit.keys_like(commit_id)
        if len(possible_commit_ids) == 0:
            raise ValueError(f"No commit with ID {repr(commit_id)}")
        if commit_id in possible_commit_ids:
            return commit_id
        if len(possible_commit_ids) > 1:
            raise ValueError(f"Ambiguous commit ID {repr(commit_id)}, having many choices {possible_commit_ids}.")
        return possible_commit_ids[0]

    def commit(self, message: Optional[str] = None) -> BareReprStr:
        entry = CommitEntry(kind=CommitEntryKind.manual)
        entry.execution_count = self._ip.execution_count
        entry.message = message if message is not None else f"Manual commit after {entry.execution_count} executions."
        self.save_notebook()
        self._commit_entry(entry)
        return BareReprStr(entry.commit_id)

    def _commit_entry(self, entry: CommitEntry, changed_vars: Optional[ChangedVariables] = None) -> None:
        # Generate commit ID.
        entry.commit_id = self._commit_id()
        entry.timestamp = time.time()

        # Observe all cells and extract notebook informations.
        entry.executed_cells = self._user_ns.ipython_in()
        entry.raw_nb, entry.formatted_cells = self._all_notebook_cells()
        if entry.formatted_cells is not None:
            code_cells = []
            for cell in entry.formatted_cells:
                code_cells.append(cell.cell_type)
                code_cells.append(cell.source)
            entry.code_version = hash(tuple(code_cells))

        # Plan for checkpointing and restoration.
        checkpoint_start_time = time.time()
        entry.restore_plan, entry.varset_version = self._checkpoint(entry)
        entry.active_vses_string = self._cr_planner.serialize_active_vses()
        #REVISION-----
        #entry.ahg_string = self._cr_planner.serialize_ahg()
        checkpoint_runtime_s = time.time() - checkpoint_start_time
        entry.checkpoint_runtime_s = checkpoint_runtime_s

        # Update other structures.
        self.total_commit_size += self._kishu_commit.store_commit(entry)
        self._kishu_graph.step(entry.commit_id)
        self._step_branch(entry.commit_id)

        # Update variable version tracker.
        self._variable_version_tracker.update_variable_version(entry.commit_id,
                                                               set() if changed_vars is None else changed_vars.added(),
                                                               set() if changed_vars is None else changed_vars.deleted())
        # store variable version and commit-variable-version into database
        self._kishu_variable_version.store_commit_variable_version_table(
            entry.commit_id, self._variable_version_tracker.get_variable_versions())
        if changed_vars is not None:
            self._kishu_variable_version.store_variable_version_table(changed_vars.added()
                                                                      | changed_vars.deleted(), entry.commit_id)

        # Record cumulative checkpoint size if logging is enabled.
        if Config.get('EXPERIMENT', 'record_results', False):
            file_sizes = sum(f.stat().st_size for f in Path(self.database_path()).parents[0].glob('**/*') if f.is_file())
            self._cr_planner.write_row("checkpoint-size", file_sizes)

    def _commit_id(self) -> str:
        if self._commit_id_mode == "counter":
            return str(self._session_id) + ":" + str(self._last_execution_count)
        return uuid.uuid4().hex

    def _checkpoint(self, cell_info: CommitEntry) -> Tuple[RestorePlan, int]:
        """
        Performs checkpointing and creates a matching restoration plan.

        TODO: Perform more intelligent checkpointing.
        """
        # Step 1: prepare a restoration plan using results from the optimizer.
        parent_commit_ids = [node.commit_id for node in self._kishu_graph.list_history()]
        checkpoint_plan, restore_plan = self._cr_planner.generate_checkpoint_restore_plans(
            self.database_path(), cell_info.commit_id, parent_commit_ids)

        # Step 2: checkpoint
        start = time.time()
        checkpoint_plan.run(self._user_ns)
        self._cr_planner.write_row("checkpoint-time", time.time() - start)
        try:
            self._cr_planner.write_row("commit-table-size", self.total_commit_size + sys.getsizeof(self._cr_planner._ahg.serialize()))
        except Exception as e:
            self._cr_planner.write_row_text("commit-table-error", e)

        # Extra: generate variable version.
        data_version = hash(pickle.dumps(self._cr_planner.get_ahg().get_variable_snapshots()))
        return restore_plan, data_version

    @staticmethod
    def _ipylab_frontend_app() -> ipylab.JupyterFrontEnd:
        app = ipylab.JupyterFrontEnd()
        run_ui_poll_loop(lambda: (  # This unblocks web UI to connect with app.
            None if app.commands.list_commands() == []
            else app.commands.list_commands()
        ))
        return app

    def _all_notebook_cells(self) -> Tuple[Optional[str], List[FormattedCell]]:
        nb = JupyterRuntimeEnv.read_notebook(self._notebook_id.path())
        nb_cells = []
        for cell in nb.cells:
            if cell.cell_type == "code":
                nb_cells.append(FormattedCell(
                    cell_type=cell.cell_type,
                    source=cell.source,
                    output=self._parse_cell_output(cell.outputs),
                    execution_count=cell.execution_count,
                ))
            elif cell.cell_type == "markdown" or cell.cell_type == "raw":
                nb_cells.append(FormattedCell(
                    cell_type=cell.cell_type,
                    source=cell.source,
                    output=None,
                    execution_count=None,
                ))
            else:
                raise ValueError(f"Unknown cell type: {cell.cell_type}")
        return nbformat.writes(nb), nb_cells

    def _parse_cell_output(self, cell_outputs: List[Dict[Any, Any]]) -> Optional[str]:
        if len(cell_outputs) == 0:
            return None
        for cell_output in cell_outputs:
            # Filter auto-saving output.
            if (
                cell_output["output_type"] == "display_data" and
                cell_output["data"].get("application/javascript", "") == KishuForJupyter.SAVE_CMD
            ):
                continue

            # Now parse output into text.
            if cell_output["output_type"] == "stream":
                return cell_output["text"]
            elif cell_output["output_type"] == "execute_result":
                if "text/plain" in cell_output["data"]:
                    return cell_output["data"].get("text/plain", "<execute_result>")
                else:
                    raise ValueError(f"Unknown output data structure: {cell_output['data']}")
            elif cell_output["output_type"] == "display_data":
                return cell_output["data"].get("text/plain", "<display_data>")
            elif cell_output["output_type"] == "error":
                return "\n".join([
                    *cell_output["traceback"],
                    f'{cell_output["ename"]}: {cell_output["evalue"]}',
                ])
            else:
                raise ValueError(f"Unknown output type: {cell_output}")
        return None

    def _step_branch(self, commit_id: str) -> None:
        head = self._kishu_branch.update_head(commit_id=commit_id)
        if self._enable_auto_branch and head.branch_name is None:
            new_branch_name = KishuBranch.random_branch_name()
            self._kishu_branch.upsert_branch(new_branch_name, commit_id)
            self._kishu_branch.update_head(new_branch_name, commit_id)
        elif head.branch_name is not None:
            self._kishu_branch.upsert_branch(head.branch_name, commit_id)

    def _checkout_notebook(self, raw_nb: str) -> None:
        nb_path = self._notebook_id.path()

        # Read current notebook cells.
        nb = JupyterRuntimeEnv.read_notebook(self._notebook_id.path())

        # Apply target cells.
        target_nb = nbformat.reads(raw_nb, JupyterRuntimeEnv.NBFORMAT_VERSION)
        nb.cells = target_nb.cells

        # Save change
        nbformat.write(nb, nb_path)

        # Reload frontend to reflect checked out notebook. This may prompts a confirmation dialog.
        self.reload_jupyter_frontend()

    def _checkout_namespace(self, user_ns: Namespace, target_ns: Namespace) -> None:
        user_ns.update(target_ns)
        for key in list(user_ns.keyset()):
            if key not in target_ns:
                del user_ns[key]


def repr_if_not_none(obj: Any) -> Optional[str]:
    if obj is None:
        return obj
    return repr(obj)


KISHU_INSTRUMENT = '_kishu'
KISHU_VARS = set(['kishu', 'init_kishu', KISHU_INSTRUMENT])
Namespace.register_kishu_vars(KISHU_VARS)


def init_kishu(notebook_path: Optional[str] = None) -> None:
    # Create notebook id.
    notebook_id = NotebookId.from_enclosing(None if notebook_path is None else Path(notebook_path))

    # Construct a kishu instrument.
    ip = eval('get_ipython()')
    kishu = KishuForJupyter(notebook_id, ip=ip)
    kishu.save_notebook()

    # Open notebook file after saving.
    nb = JupyterRuntimeEnv.read_notebook(notebook_id.path())

    # Update notebook metadata.
    metadata = notebook_id.create_kishu_metadata(nb)
    NotebookId.add_kishu_metadata(nb, metadata)
    nbformat.write(nb, notebook_id.path())
    kishu.set_session_id(metadata.session_count)

    # Attach Kishu instrumentation.
    kishu.install_kishu_hooks()
    kishu.reload_jupyter_frontend()


def detach_kishu(notebook_path: Optional[str] = None) -> None:
    # Create notebook id.
    notebook_id = NotebookId.from_enclosing(None if notebook_path is None else Path(notebook_path))

    # Remove all hooks.
    ip = eval('get_ipython()')
    if ip is not None and KISHU_INSTRUMENT in ip.user_ns:
        ip.user_ns[KISHU_INSTRUMENT].uninstall_kishu_hooks()

    # Open notebook file.
    nb = JupyterRuntimeEnv.read_notebook(notebook_id.path())

    # Remove metadata from notebook.
    try:
        NotebookId.remove_kishu_metadata(nb)
        nbformat.write(nb, notebook_id.path())
    except MissingNotebookMetadataError:
        # This means that kishu metadata is not in the notebook, so do nothing.
        pass
