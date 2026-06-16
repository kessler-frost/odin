
from odin.agent.prompt import (
    build_suggest_defaults_prompt,
    build_system_prompt,
    build_validate_prompt,
)
from odin.api.canvas import CanvasGraph


def test_build_system_prompt():
    prompt = build_system_prompt()
    assert "boto3" in prompt
    assert ".odin/infra/" in prompt
    assert "one file per resource" in prompt.lower() or ".odin/infra/" in prompt


def test_build_system_prompt_contains_workflow():
    prompt = build_system_prompt()
    assert "## Workflow" in prompt


def test_build_system_prompt_contains_validate_file():
    prompt = build_system_prompt()
    assert "validate_file" in prompt


def test_build_system_prompt_contains_output_rules():
    prompt = build_system_prompt()
    assert "ZERO text output" in prompt


def test_build_validate_prompt_with_nodes():
    graph = CanvasGraph(
        nodes=[
            {
                "id": "vpc-1",
                "type": "vpc",
                "position": {"x": 40, "y": 40},
                "size": {"width": 560, "height": 380},
                "data": {"label": "prod-vpc", "cidr": "10.0.0.0/16"},
            },
            {
                "id": "ec2-1",
                "type": "ec2",
                "position": {"x": 80, "y": 160},
                "data": {"label": "web-server", "instanceType": "t2.micro"},
            },
        ],
        edges=[],
    )
    prompt = build_validate_prompt(graph)
    assert "prod-vpc" in prompt
    assert "web-server" in prompt
    assert "10.0.0.0/16" in prompt


def test_build_validate_prompt_with_edges():
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


def test_build_validate_prompt_empty_graph():
    graph = CanvasGraph(nodes=[], edges=[])
    prompt = build_validate_prompt(graph)
    assert "no resources" in prompt.lower() or "empty" in prompt.lower() or "clean up" in prompt.lower()


def test_build_suggest_defaults_prompt_with_nodes():
    graph = CanvasGraph(
        nodes=[
            {
                "id": "vpc-1",
                "type": "vpc",
                "position": {"x": 0, "y": 0},
                "size": {"width": 600, "height": 400},
                "data": {"label": "prod-vpc", "cidr": "10.0.0.0/16"},
            },
            {
                "id": "ec2-1",
                "type": "ec2",
                "position": {"x": 100, "y": 100},
                "data": {"label": "web-server", "instanceType": "t2.micro"},
            },
        ],
        edges=[],
    )
    prompt = build_suggest_defaults_prompt(graph)
    assert "JSON array" in prompt
    assert "Smart default rules" in prompt
    assert "vpc-1" in prompt
    assert "ec2-1" in prompt
    assert "prod-vpc" in prompt


def test_build_suggest_defaults_prompt_empty_graph():
    graph = CanvasGraph(nodes=[], edges=[])
    prompt = build_suggest_defaults_prompt(graph)
    assert "empty" in prompt.lower()
    assert "no defaults" in prompt.lower()
