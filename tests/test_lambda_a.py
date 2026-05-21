"""Unit tests for lambda_a pure helpers.

We import the module via importlib because `lambda` is a Python keyword
and the file would otherwise be unreachable as `lambda.lambda_a.lambda_function`.
boto3.client('devops-agent', ...) is called at module import time, so we
patch boto3.client to a stub before loading.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
LAMBDA_A_PATH = ROOT / "lambda" / "lambda_a" / "lambda_function.py"


def _load_lambda_a():
    """Import lambda_a/lambda_function.py as `lambda_a_module` with boto3
    stubbed so module-level _client = boto3.client(...) doesn't try to
    talk to AWS."""
    if "lambda_a_module" in sys.modules:
        return sys.modules["lambda_a_module"]
    with patch("boto3.client") as mock_client:
        mock_client.return_value = object()
        spec = importlib.util.spec_from_file_location(
            "lambda_a_module", LAMBDA_A_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["lambda_a_module"] = module
        spec.loader.exec_module(module)
    return module


lambda_a = _load_lambda_a()


class BuildStartingPointTests(unittest.TestCase):

    def test_extracts_namespace_metric_dimensions(self):
        detail = {
            "configuration": {
                "metrics": [{
                    "metricStat": {
                        "metric": {
                            "namespace": "AWS/EC2",
                            "name": "CPUUtilization",
                            "dimensions": {"InstanceId": "i-abc"},
                        }
                    }
                }]
            }
        }
        ns, name, dims = lambda_a._build_starting_point(detail)
        self.assertEqual(ns, "AWS/EC2")
        self.assertEqual(name, "CPUUtilization")
        self.assertEqual(dims, {"InstanceId": "i-abc"})

    def test_no_metrics_returns_empty(self):
        ns, name, dims = lambda_a._build_starting_point({})
        self.assertEqual((ns, name, dims), ("", "", {}))

    def test_missing_dimensions_returns_empty_dict(self):
        detail = {
            "configuration": {
                "metrics": [{
                    "metricStat": {
                        "metric": {"namespace": "AWS/RDS", "name": "CPU"}
                    }
                }]
            }
        }
        ns, name, dims = lambda_a._build_starting_point(detail)
        self.assertEqual(ns, "AWS/RDS")
        self.assertEqual(dims, {})


class BuildTitleTests(unittest.TestCase):

    def test_with_instance_id(self):
        title = lambda_a._build_title("petsite-cpu-high", {"InstanceId": "i-1"})
        self.assertEqual(title, "Investigate: petsite-cpu-high (i-1)")

    def test_with_db_instance_identifier(self):
        title = lambda_a._build_title(
            "petsite-rds-conn", {"DBInstanceIdentifier": "petsite-db"},
        )
        self.assertEqual(title, "Investigate: petsite-rds-conn (petsite-db)")

    def test_with_cluster_name(self):
        title = lambda_a._build_title(
            "petsite-eks-node-loss", {"ClusterName": "petsite"},
        )
        self.assertEqual(title, "Investigate: petsite-eks-node-loss (petsite)")

    def test_with_function_name(self):
        title = lambda_a._build_title(
            "lambda-errors", {"FunctionName": "petsite-fn"},
        )
        self.assertEqual(title, "Investigate: lambda-errors (petsite-fn)")

    def test_with_load_balancer(self):
        title = lambda_a._build_title(
            "alb-5xx", {"LoadBalancer": "app/petsite/abc"},
        )
        self.assertEqual(title, "Investigate: alb-5xx (app/petsite/abc)")

    def test_with_target_group(self):
        title = lambda_a._build_title("tg-unhealthy", {"TargetGroup": "tg/x"})
        self.assertEqual(title, "Investigate: tg-unhealthy (tg/x)")

    def test_no_known_dim_omits_suffix(self):
        title = lambda_a._build_title("custom-alarm", {"Foo": "bar"})
        self.assertEqual(title, "Investigate: custom-alarm")

    def test_empty_dimensions(self):
        self.assertEqual(
            lambda_a._build_title("x", {}), "Investigate: x",
        )


class FormatDescriptionTests(unittest.TestCase):

    _DEFAULT_DIM = object()

    def _call(self, namespace="AWS/EC2", dimensions=_DEFAULT_DIM):
        if dimensions is self._DEFAULT_DIM:
            dimensions = {"InstanceId": "i-1"}
        return lambda_a._format_description(
            alarm_name="petsite-cpu",
            account="111122223333",
            region="ap-northeast-1",
            namespace=namespace,
            metric_name="CPUUtilization",
            dimensions=dimensions,
            reason="Threshold crossed",
        )

    def test_starts_with_starting_point_block(self):
        text = self._call()
        self.assertIn("Investigation starting point:", text)
        self.assertIn("Source: CloudWatch Alarm 'petsite-cpu'", text)
        self.assertIn("Account: 111122223333", text)
        self.assertIn("Region: ap-northeast-1", text)
        self.assertIn("Namespace: AWS/EC2", text)
        self.assertIn("Metric: CPUUtilization", text)
        self.assertIn("- InstanceId: i-1", text)
        self.assertIn("Alarm reason: Threshold crossed", text)

    def test_default_hint_for_unknown_namespace(self):
        text = self._call(namespace="Custom/MyApp")
        self.assertIn("Investigation details:", text)
        self.assertIn(lambda_a.DEFAULT_NAMESPACE_HINT, text)

    def test_eks_namespace_uses_specific_hint(self):
        text = self._call(namespace="AWS/EKS")
        self.assertIn(lambda_a.NAMESPACE_HINTS["AWS/EKS"], text)
        self.assertNotIn(lambda_a.DEFAULT_NAMESPACE_HINT, text)

    def test_rds_namespace_uses_specific_hint(self):
        text = self._call(namespace="AWS/RDS")
        self.assertIn(lambda_a.NAMESPACE_HINTS["AWS/RDS"], text)

    def test_lambda_namespace_uses_specific_hint(self):
        text = self._call(namespace="AWS/Lambda")
        self.assertIn(lambda_a.NAMESPACE_HINTS["AWS/Lambda"], text)

    def test_alb_namespace_uses_specific_hint(self):
        text = self._call(namespace="AWS/ApplicationELB")
        self.assertIn(lambda_a.NAMESPACE_HINTS["AWS/ApplicationELB"], text)

    def test_autoscaling_namespace_uses_specific_hint(self):
        text = self._call(namespace="AWS/AutoScaling")
        self.assertIn(lambda_a.NAMESPACE_HINTS["AWS/AutoScaling"], text)

    def test_container_insights_uses_specific_hint(self):
        text = self._call(namespace="ContainerInsights")
        self.assertIn(lambda_a.NAMESPACE_HINTS["ContainerInsights"], text)

    def test_empty_namespace_renders_unknown(self):
        text = self._call(namespace="")
        self.assertIn("Namespace: (unknown)", text)
        # Falls back to default hint
        self.assertIn(lambda_a.DEFAULT_NAMESPACE_HINT, text)

    def test_no_dimensions_renders_placeholder(self):
        text = self._call(dimensions={})
        self.assertIn("(no dimensions)", text)


class HandlerTests(unittest.TestCase):

    def _alarm_event(self, state="ALARM"):
        return {
            "account": "111122223333",
            "region": "ap-northeast-1",
            "detail": {
                "alarmName": "petsite-cpu-high",
                "state": {"value": state, "reason": "test"},
                "configuration": {
                    "metrics": [{
                        "metricStat": {
                            "metric": {
                                "namespace": "AWS/EC2",
                                "name": "CPUUtilization",
                                "dimensions": {"InstanceId": "i-abc"},
                            }
                        }
                    }]
                },
            },
        }

    def test_skips_when_state_is_not_alarm(self):
        result = lambda_a.lambda_handler(self._alarm_event(state="OK"), None)
        self.assertEqual(result["statusCode"], 200)
        self.assertIn("Skipped", result["body"])

    def test_alarm_state_calls_create_backlog_task(self):
        event = self._alarm_event()
        with patch.object(lambda_a, "_client") as mock_client:
            mock_client.create_backlog_task.return_value = {
                "task": {
                    "taskId": "task-abc",
                    "executionId": "exec-abc",
                    "status": "PENDING",
                }
            }
            result = lambda_a.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        mock_client.create_backlog_task.assert_called_once()
        kwargs = mock_client.create_backlog_task.call_args.kwargs
        self.assertEqual(kwargs["taskType"], "INVESTIGATION")
        self.assertEqual(kwargs["priority"], "HIGH")
        self.assertIn("petsite-cpu-high", kwargs["title"])
        self.assertIn("AWS/EC2", kwargs["description"])


if __name__ == "__main__":
    unittest.main()
