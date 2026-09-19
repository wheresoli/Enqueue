import json
import sys
from pathlib import Path

from enqueue.engine import Engine
from enqueue.runstore import RunStore

from .test_graph import write_graph


def test_liveshell_graph_runs_and_resumes(tmp_path):
    graph_path = tmp_path / "shell.usda"
    output_path = tmp_path / "shell.run.usda"
    command = f'"{sys.executable}" -c "print(40 + 2)"'
    write_graph(graph_path, f'''
    def EnqueueStart "Start" (inherits=</EnqueueStart>) {{}}
    def EnqueueLiveShellCommand "Command" (inherits=</EnqueueLiveShellCommand>) {{
        string outputs:command = {json.dumps(command)}
        asset outputs:workingDirectory = @{tmp_path.as_posix()}@
        double outputs:timeoutSeconds = 30
        rel enqueue:requires = </Graph/Start>
    }}
    def EnqueueLiveShellSession "Session" (inherits=</EnqueueLiveShellSession>) {{
        string inputs:command.connect = </Graph/Command.outputs:command>
        asset inputs:workingDirectory.connect = </Graph/Command.outputs:workingDirectory>
        double inputs:timeoutSeconds.connect = </Graph/Command.outputs:timeoutSeconds>
        string outputs:stdout
        string outputs:stderr
        int outputs:exitCode
    }}
    def EnqueueEnd "End" (inherits=</EnqueueEnd>) {{
        rel enqueue:requires = </Graph/Session>
    }}
''')
    summary = Engine().run(graph_path, output=output_path)
    assert summary.succeeded
    store = RunStore.open(output_path)
    from pxr import Sdf
    assert "42" in store.result_for(Sdf.Path("/Graph/Session"))
    resumed = Engine().resume(output_path)
    assert resumed.succeeded


def test_task_source_renders_contract_for_iterator(tmp_path):
    graph_path = tmp_path / "tasks.usda"
    output_path = tmp_path / "tasks.run.usda"
    write_graph(graph_path, '''
    def EnqueueStart "Start" (inherits=</EnqueueStart>) {}
    def EnqueueTaskSource "Task" (inherits=</EnqueueTaskSource>) {
        string enqueue:taskId = "task:test"
        string enqueue:title = "Test task"
        string[] enqueue:goals = ["Produce evidence."]
        rel enqueue:requires = </Graph/Start>
    }
    def EnqueueIterator "Order" (inherits=</EnqueueIterator>) {
        string inputs:input.connect = [</Graph/Task.outputs:value>]
    }
    def EnqueueEnd "End" (inherits=</EnqueueEnd>) {
        rel enqueue:requires = </Graph/Order>
    }
''')
    summary = Engine().run(graph_path, output=output_path)
    assert summary.succeeded
    store = RunStore.open(output_path)
    from pxr import Sdf
    result = store.result_for(Sdf.Path("/Graph/Order"))
    assert "Test task" in result
    assert "Task ID: task:test" in result
    assert "Produce evidence." in result
