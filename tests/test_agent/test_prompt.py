from odin.agent.prompt import (
    build_suggest_defaults_prompt,
    build_system_prompt,
    build_validate_prompt,
)
from odin.api.canvas import CanvasGraph


def test_system_prompt_targets_terraform():
    prompt = build_system_prompt()
    assert "Terraform" in prompt or "OpenTofu" in prompt
    assert "main.tf" in prompt
    assert ".odin/tf" in prompt


def test_system_prompt_has_workflow_and_validate_tool():
    prompt = build_system_prompt()
    assert "## Workflow" in prompt
    assert "validate_infrastructure" in prompt


def test_system_prompt_output_rules():
    assert "ZERO text output" in build_system_prompt()


def test_validate_prompt_with_nodes():
    graph = CanvasGraph(
        nodes=[
            {
                "id": "vpc-1", "type": "vpc",
                "position": {"x": 40, "y": 40},
                "size": {"width": 560, "height": 380},
                "data": {"label": "prod-vpc", "cidr": "10.0.0.0/16"},
            },
            {
                "id": "ec2-1", "type": "ec2",
                "position": {"x": 80, "y": 160},
                "data": {"label": "web-server", "instanceType": "t2.micro"},
            },
        ],
    )
    prompt = build_validate_prompt(graph)
    assert "prod-vpc" in prompt
    assert "web-server" in prompt
    assert "10.0.0.0/16" in prompt


def test_validate_prompt_with_edges():
    graph = CanvasGraph(
        nodes=[
            {"id": "ec2-1", "type": "ec2", "position": {"x": 0, "y": 0}, "data": {"label": "web"}},
            {"id": "s3-1", "type": "s3", "position": {"x": 300, "y": 0}, "data": {"label": "data-bucket"}},
        ],
        edges=[{"id": "e1", "source": "ec2-1", "target": "s3-1"}],
    )
    prompt = build_validate_prompt(graph)
    assert "ec2-1" in prompt
    assert "s3-1" in prompt
    assert "edge" in prompt.lower() or "connection" in prompt.lower()


def test_validate_prompt_empty_graph():
    assert "empty" in build_validate_prompt(CanvasGraph()).lower()


def test_suggest_defaults_prompt_with_nodes():
    graph = CanvasGraph(
        nodes=[
            {"id": "vpc-1", "type": "vpc", "position": {"x": 0, "y": 0}, "data": {"label": "prod-vpc"}},
        ],
    )
    prompt = build_suggest_defaults_prompt(graph)
    assert "JSON array" in prompt
    assert "vpc-1" in prompt
    assert "prod-vpc" in prompt


def test_suggest_defaults_prompt_empty_graph():
    prompt = build_suggest_defaults_prompt(CanvasGraph())
    assert "empty" in prompt.lower()
    assert "no defaults" in prompt.lower()
