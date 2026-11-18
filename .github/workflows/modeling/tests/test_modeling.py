import copy
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from pydantic import ValidationError

import jobs
from design import Design, Task, synthesize

TASK = Task.model_validate_json((Path(__file__).parents[1] / "examples" / "task.json").read_bytes())
QUOTE = {"source_id": "capture", "quote": "stores each captured page"}
OUTPUT = {
    "components": [{"id": "collector", "purpose": "Capture pages.", "evidence": [QUOTE]},
                   {"id": "bucket", "purpose": "Store captured pages.", "evidence": [QUOTE]}],
    "relationships": [{"source": "collector", "target": "bucket", "description": "Writes pages.",
                       "evidence": [QUOTE]}],
    "assumptions": [], "open_questions": [],
}


class ContractTests(unittest.TestCase):
    def test_valid_design(self):
        Design.model_validate(OUTPUT).check_against(TASK)

    def test_rejects_broken_provenance_and_graph(self):
        for path, value in [
            (("components", 0, "evidence", 0, "quote"), "invented"),
            (("components", 0, "evidence", 0, "source_id"), "missing"),
            (("components", 0, "id"), "bucket"),
            (("relationships", 0, "target"), "missing"),
        ]:
            with self.subTest(path=path):
                obj = copy.deepcopy(OUTPUT)
                node = obj
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = value
                with self.assertRaises(ValueError):
                    Design.model_validate(obj).check_against(TASK)

    def test_rejects_invalid_input(self):
        for sources in [[], [TASK.sources[0]] * 2, [{"id": "a", "text": "💡" * 3000}]]:
            with self.subTest(sources=len(sources)), self.assertRaises(ValidationError):
                Task(goal="design", sources=sources)

    def test_invalid_output_is_retried_once(self):
        def completion(content):
            return httpx.Response(200, request=httpx.Request("POST", "http://model"),
                                  json={"choices": [{"message": {"content": content}}]})
        with patch("design.httpx.Client") as client:
            post = client.return_value.__enter__.return_value.post
            post.side_effect = [completion("{}"), completion(json.dumps(OUTPUT))]
            self.assertEqual(synthesize(TASK), Design.model_validate(OUTPUT))
            self.assertEqual(post.call_count, 2)
            post.reset_mock()
            post.side_effect = [completion("{}"), completion("{}")]
            with self.assertRaisesRegex(ValueError, "both attempts"):
                synthesize(TASK)
            self.assertEqual(post.call_count, 2)


class WorkflowTests(unittest.TestCase):
    def test_submit_execute_poll_rerun(self):
        objects = {"ear-modeling/evidence/capture.json": TASK.model_dump_json(exclude={"goal"}).encode()}
        store = Mock()
        store.read.side_effect = objects.__getitem__
        store.write.side_effect = objects.__setitem__
        run = {"status": "queued", "conclusion": None, "html_url": "https://github.com/example",
               "run_attempt": 1, "path": ".github/workflows/ear-model-design.yml"}
        with patch.dict(os.environ, EAR_GITHUB_TOKEN="test"), patch("jobs.Store", return_value=store), \
             patch("jobs.github") as github, patch("jobs.synthesize", return_value=Design.model_validate(OUTPUT)) as model:
            github.return_value = {"workflow_run_id": 101, "html_url": run["html_url"]}
            handle = jobs.submit(TASK.goal, "ear-modeling/evidence/capture.json")
            request_id = handle["request_id"]
            github.assert_called_once_with("POST", "/workflows/ear-model-design.yml/dispatches",
                                           json={"ref": "main", "inputs": {"request_id": request_id}})
            github.return_value = run
            self.assertNotIn("design", jobs.status(101))
            del objects["ear-modeling/evidence/capture.json"]
            jobs.work(request_id, 101, 1)
            model.assert_called_once_with(TASK)
            run.update(status="completed", conclusion="success")
            self.assertEqual(jobs.status(101)["design"], OUTPUT)
            run.update(run_attempt=2, status="in_progress", conclusion=None)
            self.assertNotIn("design", jobs.status(101))
            run.update(status="completed", conclusion="failure")
            self.assertNotIn("design", jobs.status(101))

    def test_other_workflow_and_private_paths(self):
        with patch("jobs.github", return_value={"path": ".github/workflows/build.yml"}), \
             patch("jobs.Store") as store:
            with self.assertRaises(ValueError):
                jobs.status(123)
            for key in ["ear-modeling/requests/a.json", "ear-modeling/evidence/../b.json", "https://example.org"]:
                with self.assertRaises(ValueError):
                    jobs.submit("goal", key)
            with self.assertRaises(ValueError):
                jobs.work("../../request", 1, 1)
            store.assert_not_called()

    def test_expired_result_preserves_run_status(self):
        run = {"status": "completed", "conclusion": "success", "run_attempt": 1,
               "html_url": "https://github.com/example", "path": ".github/workflows/ear-model-design.yml"}
        with patch("jobs.github", return_value=run), patch("jobs.Store") as store:
            store.return_value.read.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            result = jobs.status(101)
            self.assertEqual(result["conclusion"], "success")
            self.assertIn("result_error", result)
            self.assertNotIn("design", result)

    def test_unknown_dispatch_outcome_does_not_resubmit(self):
        with patch.dict(os.environ, EAR_GITHUB_TOKEN="test"), patch("jobs.Store") as store, \
             patch("jobs.github", side_effect=httpx.ReadTimeout("timeout")) as gh:
            store.return_value.read.return_value = TASK.model_dump_json(exclude={"goal"}).encode()
            with self.assertRaisesRegex(RuntimeError, "ear-modeling-[0-9a-f]{32}"):
                jobs.submit(TASK.goal, "ear-modeling/evidence/capture.json")
            self.assertEqual(gh.call_count, 1)

    def test_b2_size_limit_and_content(self):
        env = {"EAR_B2_REGION": "us-west-004", "EAR_B2_KEY_ID": "test", "EAR_B2_APPLICATION_KEY": "test",
               "EAR_B2_BUCKET": "private"}
        with patch.dict(os.environ, env):
            store = jobs.Store()
        with Stubber(store.client) as stub:
            for size in [2, jobs.MAX_OBJECT_BYTES + 1]:
                data = io.BytesIO(b"{}")
                stub.add_response("get_object", {"Body": StreamingBody(data, 2), "ContentLength": size},
                                  {"Bucket": "private", "Key": "key"})
                if size == 2:
                    self.assertEqual(store.read("key"), b"{}")
                else:
                    with self.assertRaises(ValueError):
                        store.read("key")
                self.assertTrue(data.closed)
            stub.add_response("put_object", {}, {"Bucket": "private", "Key": "key",
                                                "ContentType": "application/json", "Body": b"{}"})
            store.write("key", b"{}")
            stub.assert_no_pending_responses()


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_publish_structured_output_schemas(self):
        from server import mcp
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        self.assertEqual(set(tools), {"model_design", "model_design_local", "model_design_status"})
        for tool in tools.values():
            self.assertEqual(tool.outputSchema["type"], "object")
        self.assertIn("components", tools["model_design_local"].outputSchema["properties"])
