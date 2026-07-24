"""S3b -- the translation agent (canvas -> TF, refined).

`_FakeClient` drives the REAL `create_sdk_mcp_server`/`@tool` dispatch
(`mcp.server.lowlevel.Server.request_handlers`) with canned args instead of
spawning the Claude Code CLI -- it exercises the exact tool-registration code
`translate.py` uses in production, not a reimplementation of it. Guardrail
tests call `validate_refinement` directly (no SDK involved at all); a handful
that need real `tofu` skip cleanly when it's not on PATH, matching
`tests/agent/test_hcl.py`'s convention.
"""
from __future__ import annotations

import asyncio
import shutil

import pytest
from mcp import types as mcp_types

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.agent.hcl import generate_tf
from odin.agent.translate import translate, validate_refinement
from odin.spec.models import ResourceDesired, Stack
from odin.spec.store import rev_of

_NO_TOFU = shutil.which("tofu") is None


class _FakeClient:
    """Test double for `ClaudeSDKClient`: records the prompt it received and,
    on `receive_response()`, drives the real MCP `call_tool` handler with
    `canned_args` (or does nothing, simulating "the agent never called the
    tool") -- subclass-configured via `_client_with` since `translate()`
    constructs `client_cls(options=...)` itself."""

    canned_args: dict | None = None
    raises: Exception | None = None

    def __init__(self, options) -> None:
        self.options = options
        self.prompt: str | None = None

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.prompt = prompt
        if self.raises is not None:
            raise self.raises

    async def receive_response(self):
        if self.canned_args is not None:
            server = self.options.mcp_servers["translate"]["instance"]
            handler = server.request_handlers[mcp_types.CallToolRequest]
            request = mcp_types.CallToolRequest(
                method="tools/call",
                params=mcp_types.CallToolRequestParams(name="emit_terraform", arguments=self.canned_args),
            )
            await handler(request)
        return
        yield  # pragma: no cover -- keeps this an async generator


class _NeverConstructed:
    def __init__(self, options) -> None:
        raise AssertionError("the SDK client should not be constructed when there's nothing to refine")


def _client_with(canned_args: dict | None = None, raises: Exception | None = None) -> type:
    return type("FakeClient", (_FakeClient,), {"canned_args": canned_args, "raises": raises})


_S3_STACK = Stack(resources=(ResourceDesired(id="uploads", kind="s3"),))
_RDS_ONLY_STACK = Stack(resources=(ResourceDesired(id="db", kind="rds"),))
# Release finding #1: a lambda's zip'd deployment package (TfProject.binary_files)
# must survive translate()'s TranslateResult on every path -- the agent never
# sees or touches it (only main.tf is in its prompt), so it must come back
# verbatim from the skeleton every time.
_LAMBDA_STACK = Stack(resources=(ResourceDesired(id="fn", kind="lambda"),))


# --- validate_refinement (the guardrail) -------------------------------------


async def test_resource_set_mismatch_is_rejected_without_needing_tofu():
    # Pure Python: the resource-SET check runs before any tofu subprocess,
    # so this must fail even with tofu absent from PATH.
    skeleton = generate_tf(_S3_STACK).files
    agent_files = {"main.tf": skeleton["main.tf"] + '\nresource "aws_sqs_queue" "extra" {\n  name = "extra"\n}\n'}
    reason, formatted = await validate_refinement(agent_files, skeleton)
    assert reason is not None and "resource set changed" in reason
    assert formatted is None


async def test_resource_removal_is_rejected():
    stack = Stack(resources=(
        ResourceDesired(id="uploads", kind="s3"), ResourceDesired(id="jobs", kind="sqs"),
    ))
    skeleton = generate_tf(stack).files
    assert len(hcl.parse_tf(skeleton)) == 2  # sanity: the fixture really has both resources

    # The agent "forgot" the queue -- same file set, one fewer resource block.
    agent_files = {"main.tf": generate_tf(Stack(resources=(ResourceDesired(id="uploads", kind="s3"),))).files["main.tf"]}
    reason, formatted = await validate_refinement(agent_files, skeleton)
    assert reason is not None and "resource set changed" in reason
    assert formatted is None


async def test_malformed_hcl_is_rejected():
    skeleton = generate_tf(_S3_STACK).files
    reason, formatted = await validate_refinement({"main.tf": "not { valid hcl"}, skeleton)
    assert reason is not None and "failed to parse" in reason
    assert formatted is None


async def test_non_portable_output_is_rejected_without_needing_tofu():
    # Pure Python: the portability scan runs before any tofu subprocess.
    skeleton = generate_tf(_S3_STACK).files
    agent_files = {"main.tf": skeleton["main.tf"].replace(
        'resource "aws_s3_bucket" "uploads" {',
        'resource "aws_s3_bucket" "uploads" {\n  # points at 127.0.0.1 for local testing',
    )}
    reason, formatted = await validate_refinement(agent_files, skeleton)
    assert reason is not None and "not portable" in reason
    assert formatted is None


async def test_no_files_is_rejected():
    skeleton = generate_tf(_S3_STACK).files
    reason, formatted = await validate_refinement({}, skeleton)
    assert reason == "agent returned no files"
    assert formatted is None


@pytest.mark.skipif(_NO_TOFU, reason="tofu not on PATH")
async def test_comment_and_tag_only_refinement_passes_and_gets_formatted():
    skeleton = generate_tf(_S3_STACK).files
    main_tf = skeleton["main.tf"].replace(
        'resource "aws_s3_bucket" "uploads" {\n  bucket = "uploads"\n\n  tags = {\n    "odin:node" = "uploads"\n  }\n}',
        '# uploaded user content\nresource "aws_s3_bucket" "uploads" {\n bucket="uploads"\n\n  tags = {\n    "odin:node" = "uploads"\n  }\n}',
    )
    reason, formatted = await validate_refinement({"main.tf": main_tf}, skeleton)
    assert reason is None
    assert "# uploaded user content" in formatted["main.tf"]
    assert 'bucket = "uploads"' in formatted["main.tf"]  # tofu fmt re-aligned it


@pytest.mark.skipif(_NO_TOFU, reason="tofu not on PATH")
async def test_tofu_validate_rejects_an_unsupported_argument():
    stack = Stack(resources=(ResourceDesired(id="items", kind="dynamodb"),))
    skeleton = generate_tf(stack).files
    # Same resource identity (type+name) -- passes the resource-SET gate --
    # but the agent invented an argument that doesn't exist on the provider
    # schema, which only `tofu validate` (not the resource-set check) catches.
    broken = (
        f"{hcl.HEADER}\n\n{hcl.provider_block()}\n\n"
        'resource "aws_dynamodb_table" "items" {\n'
        '  name                = "items"\n'
        '  billing_mode        = "PAY_PER_REQUEST"\n'
        '  hash_key            = "id"\n'
        '  totally_bogus_field = "nope"\n'
        "\n"
        '  attribute {\n'
        '    name = "id"\n'
        '    type = "S"\n'
        "  }\n"
        "}\n"
    )
    reason, formatted = await validate_refinement({"main.tf": broken}, skeleton)
    assert reason is not None and "tofu validate failed" in reason
    assert formatted is None


# --- translate() orchestration ------------------------------------------------


async def test_no_supported_resources_never_invokes_the_agent():
    result = await translate(_RDS_ONLY_STACK, client_cls=_NeverConstructed)
    assert result.refined is False
    assert result.files == generate_tf(_RDS_ONLY_STACK).files
    assert result.unsupported == generate_tf(_RDS_ONLY_STACK).unsupported


async def test_sdk_failure_falls_back_to_skeleton():
    fake = _client_with(raises=RuntimeError("boom"))
    result = await translate(_S3_STACK, client_cls=fake)
    assert result.refined is False
    assert result.files == generate_tf(_S3_STACK).files


async def test_sdk_timeout_falls_back_to_skeleton():
    class _Hangs(_FakeClient):
        async def query(self, prompt: str, session_id: str = "default") -> None:
            await asyncio.sleep(10)

    result = await translate(_S3_STACK, client_cls=_Hangs, timeout=0.05)
    assert result.refined is False
    assert result.files == generate_tf(_S3_STACK).files


async def test_agent_calling_no_tool_falls_back_to_skeleton():
    result = await translate(_S3_STACK, client_cls=_client_with(canned_args=None))
    assert result.refined is False
    assert result.files == generate_tf(_S3_STACK).files
    assert "no refinement" in result.notes[0]


async def test_binary_files_survive_sdk_failure_fallback():
    fake = _client_with(raises=RuntimeError("boom"))
    result = await translate(_LAMBDA_STACK, client_cls=fake)
    assert result.refined is False
    skeleton = generate_tf(_LAMBDA_STACK)
    assert skeleton.binary_files  # sanity: the lambda builder really zipped something
    assert result.binary_files == skeleton.binary_files


async def test_binary_files_survive_a_rejected_refinement():
    skeleton = generate_tf(_LAMBDA_STACK)
    tampered = skeleton.files["main.tf"] + '\nresource "aws_sqs_queue" "extra" {\n  name = "extra"\n}\n'
    fake = _client_with(canned_args={"files": [{"path": "main.tf", "content": tampered}], "notes": []})
    result = await translate(_LAMBDA_STACK, client_cls=fake)
    assert result.refined is False
    assert result.binary_files == skeleton.binary_files


async def test_agent_adding_a_resource_is_rejected_end_to_end():
    skeleton = generate_tf(_S3_STACK).files["main.tf"]
    tampered = skeleton + '\nresource "aws_sqs_queue" "extra" {\n  name = "extra"\n}\n'
    fake = _client_with(canned_args={"files": [{"path": "main.tf", "content": tampered}], "notes": ["added a queue"]})
    result = await translate(_S3_STACK, client_cls=fake)
    assert result.refined is False
    assert result.files == generate_tf(_S3_STACK).files
    assert any("refinement rejected" in n for n in result.notes)


@pytest.mark.skipif(_NO_TOFU, reason="tofu not on PATH")
async def test_binary_files_survive_a_successful_refinement():
    skeleton = generate_tf(_LAMBDA_STACK)
    main_tf = skeleton.files["main.tf"].replace(
        'resource "aws_lambda_function"', '# the function\nresource "aws_lambda_function"',
    )
    fake = _client_with(canned_args={"files": [{"path": "main.tf", "content": main_tf}], "notes": ["comment"]})
    result = await translate(_LAMBDA_STACK, client_cls=fake)
    assert result.refined is True
    assert result.binary_files == skeleton.binary_files


def test_for_display_drops_binary_files_and_is_json_serializable():
    # Release finding #1: the /translate response projection must exclude the
    # raw zip bytes (non-UTF8 -> not JSON-serializable) that broke the route
    # for every Lambda canvas, while keeping the .tf text + metadata.
    import json

    result = translate_mod.TranslateResult(
        files={"main.tf": "resource {}"}, notes=["n"], unsupported=["rds"], refined=True,
        binary_files={"fn.zip": b"PK\x03\x04\xff\xfe not utf-8"},
    )
    display = result.for_display()
    assert "binary_files" not in display
    assert display == {"files": {"main.tf": "resource {}"}, "notes": ["n"], "unsupported": ["rds"], "refined": True}
    json.dumps(display)  # must not raise -- the whole point of the fix
    assert result.binary_files  # the object still carries the bytes /apply-full needs


@pytest.mark.skipif(_NO_TOFU, reason="tofu not on PATH")
async def test_happy_path_refinement_is_kept():
    skeleton = generate_tf(_S3_STACK).files["main.tf"]
    refined_tf = skeleton.replace(
        'resource "aws_s3_bucket" "uploads" {\n  bucket = "uploads"\n\n  tags = {\n    "odin:node" = "uploads"\n  }\n}',
        '# user uploads\nresource "aws_s3_bucket" "uploads" {\n  bucket = "uploads"\n\n  tags = {\n    "odin:node" = "uploads"\n  }\n}',
    )
    fake = _client_with(canned_args={"files": [{"path": "main.tf", "content": refined_tf}], "notes": ["added a comment"]})
    result = await translate(_S3_STACK, client_cls=fake)
    assert result.refined is True
    assert result.notes == ["added a comment"]
    assert "# user uploads" in result.files["main.tf"]
    assert hcl.resource_set(result.files) == hcl.resource_set(generate_tf(_S3_STACK).files)


# --- real SDK (drives the actual Claude Code CLI subprocess) -----------------


@pytest.mark.integration
@pytest.mark.skipif(_NO_TOFU, reason="tofu not on PATH")
async def test_real_sdk_refines_a_three_node_canvas():
    stack = Stack(resources=(
        ResourceDesired(id="uploads", kind="s3"),
        ResourceDesired(id="jobs", kind="sqs"),
        ResourceDesired(id="items", kind="dynamodb"),
    ))
    result = await translate(stack)
    assert result.refined is True
    assert result.files  # the agent-refined (guardrail-passed) files, not empty
    # The guardrail's own invariant, re-asserted here against the real agent:
    # arguments/comments may change, the resource SET may not.
    assert hcl.resource_set(result.files) == hcl.resource_set(generate_tf(stack).files)


# --- release finding #5: the SDK timeout default + the unchanged-canvas cache


def test_default_timeout_reads_the_env_var(monkeypatch):
    monkeypatch.setenv("ODIN_TRANSLATE_TIMEOUT", "12")
    assert translate_mod._default_timeout() == 12.0


def test_default_timeout_falls_back_to_45_seconds(monkeypatch):
    monkeypatch.delenv("ODIN_TRANSLATE_TIMEOUT", raising=False)
    assert translate_mod._default_timeout() == 45.0


def test_shipped_default_timeout_constant_is_45_seconds():
    # The 120s default meant a timed-out translate() (most applies, in
    # practice, per the release sweep) wasted two full minutes before ever
    # falling back to the skeleton. Guards against a future edit silently
    # creeping the default back up.
    assert translate_mod._TIMEOUT_S == 45.0


@pytest.mark.skipif(_NO_TOFU, reason="tofu not on PATH")
async def test_unchanged_stack_skips_the_sdk_pass_after_a_successful_refinement():
    construct_count: list[int] = []

    class _Counting(_FakeClient):
        canned_args = {
            "files": [{"path": "main.tf", "content": generate_tf(_S3_STACK).files["main.tf"]}],
            "notes": ["ok"],
        }

        def __init__(self, options) -> None:
            construct_count.append(1)
            super().__init__(options)

    cache: dict = {}
    first = await translate(_S3_STACK, client_cls=_Counting, cache=cache)
    assert first.refined is True
    assert len(construct_count) == 1
    assert rev_of(_S3_STACK) in cache

    # SAME stack content, a fresh cache lookup -- the SDK must not be
    # constructed a second time. _NeverConstructed asserts this for free.
    second = await translate(_S3_STACK, client_cls=_NeverConstructed, cache=cache)
    assert second == first


async def test_a_fallback_result_is_never_cached_so_the_next_call_retries():
    cache: dict = {}
    failing = _client_with(raises=RuntimeError("boom"))
    first = await translate(_S3_STACK, client_cls=failing, cache=cache)
    assert first.refined is False
    assert cache == {}  # only a SUCCESSFUL refinement is cached

    construct_count: list[int] = []

    class _Counting(_FakeClient):
        def __init__(self, options) -> None:
            construct_count.append(1)
            super().__init__(options)

    await translate(_S3_STACK, client_cls=_Counting, cache=cache)
    assert len(construct_count) == 1  # the SDK really was retried, not skipped


async def test_a_rejected_refinement_is_never_cached_so_the_next_call_retries():
    cache: dict = {}
    tampered = generate_tf(_S3_STACK).files["main.tf"] + '\nresource "aws_sqs_queue" "extra" {\n  name = "extra"\n}\n'
    rejecting = _client_with(canned_args={"files": [{"path": "main.tf", "content": tampered}], "notes": []})
    first = await translate(_S3_STACK, client_cls=rejecting, cache=cache)
    assert first.refined is False
    assert cache == {}

    construct_count: list[int] = []

    class _Counting(_FakeClient):
        def __init__(self, options) -> None:
            construct_count.append(1)
            super().__init__(options)

    await translate(_S3_STACK, client_cls=_Counting, cache=cache)
    assert len(construct_count) == 1


async def test_a_different_stack_is_a_cache_miss():
    cache: dict = {rev_of(_S3_STACK): "unrelated cached entry"}
    result = await translate(_RDS_ONLY_STACK, client_cls=_NeverConstructed, cache=cache)
    assert result.refined is False  # untouched by the unrelated cache entry


async def test_translate_works_with_no_cache_given():
    # cache=None (the default) must behave exactly as before finding #5 --
    # no caching, no crash on the missing dict.
    result = await translate(_RDS_ONLY_STACK, client_cls=_NeverConstructed)
    assert result.refined is False
