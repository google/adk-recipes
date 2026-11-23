# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for `tools/ingestion/trajectory_files.py`."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from pathlib import Path

import pytest
from agentplatform._genai.types import EvalCase
from agentplatform._genai.types.evals import (
    AgentData,
    AgentEvent,
    ConversationTurn,
)
from ambient_quality_agent.standalone.files import FileObjectStore
from ambient_quality_agent.tools.ingestion import trajectory_files
from ambient_quality_agent.tools.ingestion.trajectory_files import (
    TRAJECTORY_FILES_PREFIX,
    TrajectoryFileStore,
    build_case_without_trajectory_files,
    store_trajectory_files,
)
from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore
from ambient_quality_agent.tools.objects.store import StorageNotConfiguredError
from google.genai.types import (
    Blob,
    Content,
    FileData,
    FunctionResponse,
    FunctionResponseBlob,
    FunctionResponseFileData,
    FunctionResponsePart,
    Part,
)

from .conftest import FakeGcsClient

_AGENT_KEY = "acme-prod__support"
_DAY = dt.date(2026, 11, 10)
_PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(40))


def _compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_placeholder(data: bytes, mime_type: str = "image/png") -> str:
    return (
        f"<attachment: {mime_type}, {len(data)} bytes, "
        f"sha256:{_compute_sha256(data)[:8]}>"
    )


def _build_case(*parts: Part) -> EvalCase:
    """Builds a case with one turn holding one user event per part.

    Args:
        *parts: The parts, each in its own event.

    Returns:
        The case.
    """
    return EvalCase(
        eval_case_id="case-1",
        agent_data=AgentData(
            turns=[
                ConversationTurn(
                    turn_index=0,
                    events=[
                        AgentEvent(
                            author="user",
                            content=Content(role="user", parts=[part]),
                        )
                        for part in parts
                    ],
                )
            ]
        ),
    )


def _list_parts(case: EvalCase) -> list[Part]:
    assert case.agent_data is not None
    return [
        part
        for turn in case.agent_data.turns or []
        for event in turn.events or []
        for part in (event.content.parts if event.content else None) or []
    ]


def _build_inline_part(data: bytes, mime_type: str = "image/png") -> Part:
    return Part(inline_data=Blob(data=data, mime_type=mime_type))


@pytest.fixture
def objects(tmp_path: Path) -> FileObjectStore:
    return FileObjectStore(tmp_path / "jobs")


@pytest.fixture
def store(objects: FileObjectStore) -> TrajectoryFileStore:
    return TrajectoryFileStore(objects, _AGENT_KEY, today=lambda: _DAY)


def test_inline_bytes_become_a_link_named_by_agent_date_and_digest(
    store: TrajectoryFileStore, objects: FileObjectStore
) -> None:
    case = _build_case(Part(text="Look:"), _build_inline_part(_PNG))

    report = store_trajectory_files(case, store)

    name = (
        f"aqa-trajectory-files/{_AGENT_KEY}/2026-11-10/{_compute_sha256(_PNG)}"
    )
    text, link = _list_parts(case)
    assert text.text == "Look:"
    assert link.inline_data is None
    assert link.file_data == FileData(
        file_uri=objects.uri(name), mime_type="image/png"
    )
    assert objects.read_bytes(name) == _PNG
    assert (report.stored, report.not_stored, report.failed) == (1, 0, 0)


def test_the_same_bytes_twice_give_one_link_and_one_object(
    store: TrajectoryFileStore, objects: FileObjectStore
) -> None:
    first = _build_case(_build_inline_part(_PNG))
    second = _build_case(_build_inline_part(_PNG), _build_inline_part(_PNG))

    store_trajectory_files(first, store)
    report = store_trajectory_files(second, store)

    links = {
        part.file_data.file_uri if part.file_data else None
        for part in _list_parts(first) + _list_parts(second)
    }
    assert len(links) == 1
    assert objects.list_names(TRAJECTORY_FILES_PREFIX) == [
        f"aqa-trajectory-files/{_AGENT_KEY}/2026-11-10/{_compute_sha256(_PNG)}"
    ]
    assert report.stored == 2


def test_a_file_ingested_on_another_day_gets_its_own_object(
    objects: FileObjectStore,
) -> None:
    for day in (_DAY, _DAY + dt.timedelta(days=1)):
        store_trajectory_files(
            _build_case(_build_inline_part(_PNG)),
            TrajectoryFileStore(objects, _AGENT_KEY, today=lambda day=day: day),
        )

    assert objects.list_prefixes(f"{TRAJECTORY_FILES_PREFIX}{_AGENT_KEY}/") == [
        f"aqa-trajectory-files/{_AGENT_KEY}/2026-11-10/",
        f"aqa-trajectory-files/{_AGENT_KEY}/2026-11-11/",
    ]


def test_the_gcs_store_writes_create_only_and_links_a_gs_uri() -> None:
    client = FakeGcsClient()
    gcs = GcsObjectStore("jobs-bucket", client_factory=lambda: client)
    store = TrajectoryFileStore(gcs, _AGENT_KEY, today=lambda: _DAY)
    case = _build_case(_build_inline_part(_PNG), _build_inline_part(_PNG))

    report = store_trajectory_files(case, store)

    name = (
        f"aqa-trajectory-files/{_AGENT_KEY}/2026-11-10/{_compute_sha256(_PNG)}"
    )
    assert [
        part.file_data.file_uri if part.file_data else None
        for part in _list_parts(case)
    ] == [f"gs://jobs-bucket/{name}"] * 2
    assert [upload[2] for upload in client.uploads] == [0, 0]
    assert client.contents == {("jobs-bucket", name): _PNG}
    assert report.stored == 2
    assert report.failed == 0


def test_the_default_clock_is_the_utc_date(objects: FileObjectStore) -> None:
    store = TrajectoryFileStore(objects, _AGENT_KEY)
    case = _build_case(_build_inline_part(_PNG))

    store_trajectory_files(case, store)

    today = dt.datetime.now(dt.UTC).date().isoformat()
    [name] = objects.list_names(TRAJECTORY_FILES_PREFIX)
    # The date can roll over between the write and this read.
    assert name.split("/")[2] in {
        today,
        (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat(),
    }


class _RefusingStore:
    """A store that raises `error` on every write."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def write_bytes(
        self, name: str, data: bytes, *, overwrite: bool = True
    ) -> bool:
        raise self.error

    def uri(self, name: str) -> str:
        return f"refused://{name}"


def _build_store_raising(error: Exception) -> TrajectoryFileStore:
    return TrajectoryFileStore(
        _RefusingStore(error),
        _AGENT_KEY,
        today=lambda: _DAY,
    )


@pytest.mark.parametrize(
    "build_store",
    [
        pytest.param(lambda: None, id="no-store"),
        pytest.param(
            lambda: _build_store_raising(StorageNotConfiguredError("none")),
            id="storage-not-configured",
        ),
    ],
)
def test_a_file_that_is_not_stored_becomes_the_digest_placeholder(
    build_store,
) -> None:
    case = _build_case(_build_inline_part(_PNG))

    report = store_trajectory_files(case, build_store())

    [part] = _list_parts(case)
    assert part.text == _build_placeholder(_PNG)
    assert part.inline_data is None
    assert (report.stored, report.not_stored, report.failed) == (0, 1, 0)


def test_a_file_above_the_size_limit_is_not_stored(
    store: TrajectoryFileStore,
    objects: FileObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        trajectory_files, "MAX_TRAJECTORY_FILE_BYTES", len(_PNG) - 1
    )
    case = _build_case(_build_inline_part(_PNG))

    report = store_trajectory_files(case, store)

    [part] = _list_parts(case)
    assert part.text == _build_placeholder(_PNG)
    assert objects.list_names("") == []
    assert (report.stored, report.not_stored, report.failed) == (0, 1, 0)


def test_a_file_at_the_size_limit_is_stored(
    store: TrajectoryFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        trajectory_files, "MAX_TRAJECTORY_FILE_BYTES", len(_PNG)
    )
    case = _build_case(_build_inline_part(_PNG))

    assert store_trajectory_files(case, store).stored == 1


def test_a_write_error_gives_the_placeholder_and_counts_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _build_store_raising(OSError("disk on fire: secret-user-text"))
    case = _build_case(_build_inline_part(_PNG, "application/pdf"))

    with caplog.at_level(logging.WARNING, logger=trajectory_files.__name__):
        report = store_trajectory_files(case, store)

    [part] = _list_parts(case)
    assert part.text == _build_placeholder(_PNG, "application/pdf")
    assert (report.stored, report.not_stored, report.failed) == (0, 0, 1)
    [record] = caplog.records
    message = record.getMessage()
    assert "application/pdf" in message
    assert str(len(_PNG)) in message
    assert "OSError" in message
    assert "secret-user-text" not in message
    assert _compute_sha256(_PNG) not in message


def _build_tool_result_case(nested: FunctionResponsePart) -> EvalCase:
    return _build_case(
        Part(
            function_response=FunctionResponse(
                name="render_chart",
                response={"status": "ok"},
                parts=[nested],
            )
        )
    )


def test_nested_function_response_files_are_stored(
    store: TrajectoryFileStore, objects: FileObjectStore
) -> None:
    case = _build_tool_result_case(
        FunctionResponsePart(
            inline_data=FunctionResponseBlob(data=_PNG, mime_type="image/png")
        )
    )

    report = store_trajectory_files(case, store)

    [part] = _list_parts(case)
    assert part.function_response is not None
    assert part.function_response.response == {"status": "ok"}
    [nested] = part.function_response.parts or []
    name = (
        f"aqa-trajectory-files/{_AGENT_KEY}/2026-11-10/{_compute_sha256(_PNG)}"
    )
    assert nested.inline_data is None
    assert nested.file_data is not None
    assert nested.file_data.file_uri == objects.uri(name)
    assert nested.file_data.mime_type == "image/png"
    assert report.stored == 1


def test_nested_function_response_files_stay_unchanged_when_not_stored() -> (
    None
):
    nested = FunctionResponsePart(
        inline_data=FunctionResponseBlob(data=_PNG, mime_type="image/png")
    )
    case = _build_tool_result_case(nested)

    report = store_trajectory_files(case, None)

    [part] = _list_parts(case)
    assert part.function_response is not None
    assert part.function_response.parts == [nested]
    assert (report.stored, report.not_stored, report.failed) == (0, 1, 0)


def test_an_agent_sent_link_and_a_case_without_files_are_unchanged(
    store: TrajectoryFileStore, objects: FileObjectStore
) -> None:
    link = Part(
        file_data=FileData(file_uri="gs://agent/a.pdf", mime_type="x/y")
    )
    case = _build_case(Part(text="Hello."), link)
    before = case.model_dump()

    report = store_trajectory_files(case, store)

    assert case.model_dump() == before
    assert report.total == 0
    assert objects.list_names("") == []


def test_a_case_without_agent_data_is_unchanged(
    store: TrajectoryFileStore,
) -> None:
    case = EvalCase(eval_case_id="empty")

    assert store_trajectory_files(case, store).total == 0


def test_an_empty_file_is_not_stored(
    store: TrajectoryFileStore, objects: FileObjectStore
) -> None:
    nested = FunctionResponsePart(
        inline_data=FunctionResponseBlob(data=b"", mime_type="image/png")
    )
    case = _build_case(_build_inline_part(b""))
    tool_case = _build_tool_result_case(nested)

    report = store_trajectory_files(case, store)
    tool_report = store_trajectory_files(tool_case, store)

    [part] = _list_parts(case)
    assert part.text == _build_placeholder(b"")
    assert "0 bytes" in (part.text or "")
    [tool_part] = _list_parts(tool_case)
    assert tool_part.function_response is not None
    assert tool_part.function_response.parts == [nested]
    assert (report.not_stored, tool_report.not_stored) == (1, 1)
    assert objects.list_names("") == []


# --- what the evaluation service gets -----------------------------------------


_GS_LINK = f"gs://jobs-bucket/aqa-trajectory-files/{_AGENT_KEY}/2026-11-10/{_compute_sha256(_PNG)}"
_FILE_PATH_LINK = (
    f"/home/dev/project/.aqua/job/aqa-trajectory-files/{_AGENT_KEY}/2026-11-10/"
    f"{_compute_sha256(_PNG)}"
)


def _build_link(file_uri: str, mime_type: str | None = "image/png") -> Part:
    return Part(file_data=FileData(file_uri=file_uri, mime_type=mime_type))


@pytest.mark.parametrize(
    "file_uri",
    [
        pytest.param(_GS_LINK, id="gs"),
        pytest.param(_FILE_PATH_LINK, id="file-path"),
        pytest.param(
            f"aqa-trajectory-files/{_AGENT_KEY}/2026-11-10/{_compute_sha256(_PNG)}",
            id="bare-name",
        ),
    ],
)
def test_a_trajectory_file_link_becomes_the_bare_placeholder(
    file_uri: str,
) -> None:
    case = _build_case(Part(text="Look:"), _build_link(file_uri))

    cleaned = build_case_without_trajectory_files(case)

    assert cleaned is not case
    assert [part.text for part in _list_parts(cleaned)] == [
        "Look:",
        "<attachment: image/png>",
    ]
    assert _list_parts(case)[1].file_data == FileData(
        file_uri=file_uri, mime_type="image/png"
    ), "the input case keeps its link"


def test_a_trajectory_file_link_without_a_mime_type_names_the_default() -> None:
    case = _build_case(_build_link(_GS_LINK, mime_type=None))

    [part] = _list_parts(build_case_without_trajectory_files(case))

    assert part.text == "<attachment: application/octet-stream>"


def _build_nested_link(file_uri: str) -> FunctionResponsePart:
    return FunctionResponsePart(
        file_data=FunctionResponseFileData(
            file_uri=file_uri, mime_type="image/png"
        )
    )


def test_a_nested_trajectory_file_link_is_removed() -> None:
    case = _build_tool_result_case(_build_nested_link(_GS_LINK))

    cleaned = build_case_without_trajectory_files(case)

    [part] = _list_parts(cleaned)
    assert part.function_response is not None
    assert part.function_response.parts is None
    assert part.function_response.response == {"status": "ok"}
    [original] = _list_parts(case)
    assert original.function_response is not None
    assert original.function_response.parts == [_build_nested_link(_GS_LINK)]


def test_a_nested_agent_link_stays_beside_a_removed_trajectory_file_link() -> (
    None
):
    case = _build_case(
        Part(
            function_response=FunctionResponse(
                name="render_chart",
                response={},
                parts=[
                    _build_nested_link(_FILE_PATH_LINK),
                    _build_nested_link("gs://agent/chart.png"),
                ],
            )
        )
    )

    [part] = _list_parts(build_case_without_trajectory_files(case))

    assert part.function_response is not None
    assert part.function_response.parts == [
        _build_nested_link("gs://agent/chart.png")
    ]


@pytest.mark.parametrize(
    "file_uri",
    [
        pytest.param("gs://agent/report.pdf", id="agent-sent"),
        pytest.param(
            f"gs://b/aqa-trajectory-files/{_AGENT_KEY}/2026-11-10/{'A' * 64}",
            id="uppercase-digest",
        ),
        pytest.param(
            f"gs://b/aqa-trajectory-files/{_AGENT_KEY}/2026-11-10/{'a' * 63}",
            id="short-digest",
        ),
        pytest.param(
            f"gs://b/aqa-trajectory-files/{_AGENT_KEY}/x/2026-11-10/{'a' * 64}",
            id="extra-segment",
        ),
        pytest.param(
            f"gs://b/my-aqa-trajectory-files/{_AGENT_KEY}/2026-11-10/{'a' * 64}",
            id="prefix-not-at-segment-start",
        ),
    ],
)
def test_a_case_without_trajectory_file_links_is_returned_as_is(
    file_uri: str,
) -> None:
    case = _build_case(Part(text="Hello."), _build_link(file_uri))

    assert build_case_without_trajectory_files(case) is case


def test_a_case_without_files_is_returned_as_is() -> None:
    case = _build_case(Part(text="Hello."))

    assert build_case_without_trajectory_files(case) is case
    empty = EvalCase(eval_case_id="empty")
    assert build_case_without_trajectory_files(empty) is empty


# --- names, links and logs ---------------------------------------------------


def test_a_slash_in_the_agent_key_stays_one_name_segment(
    objects: FileObjectStore,
) -> None:
    store = TrajectoryFileStore(objects, "acme/support", today=lambda: _DAY)
    case = _build_case(_build_inline_part(_PNG))

    store_trajectory_files(case, store)

    assert objects.list_names(TRAJECTORY_FILES_PREFIX) == [
        f"aqa-trajectory-files/acme_support/2026-11-10/{_compute_sha256(_PNG)}"
    ]
    [part] = _list_parts(build_case_without_trajectory_files(case))
    assert part.text == "<attachment: image/png>"


def test_a_link_the_file_store_produces_is_a_trajectory_file_link(
    store: TrajectoryFileStore,
) -> None:
    case = _build_case(_build_inline_part(_PNG))
    store_trajectory_files(case, store)

    [part] = _list_parts(build_case_without_trajectory_files(case))

    assert part.text == "<attachment: image/png>"


def test_a_link_the_gcs_store_produces_is_a_trajectory_file_link() -> None:
    client = FakeGcsClient()
    gcs = GcsObjectStore("jobs-bucket", client_factory=lambda: client)
    store = TrajectoryFileStore(gcs, _AGENT_KEY, today=lambda: _DAY)
    case = _build_tool_result_case(
        FunctionResponsePart(
            inline_data=FunctionResponseBlob(data=_PNG, mime_type="image/png")
        )
    )
    store_trajectory_files(case, store)

    [part] = _list_parts(build_case_without_trajectory_files(case))

    assert part.function_response is not None
    assert part.function_response.parts is None


@pytest.mark.parametrize(
    ("mime_type", "logged"),
    [
        pytest.param("application/pdf", "application/pdf", id="plain"),
        pytest.param("image/svg+xml", "image/svg+xml", id="with-plus"),
        pytest.param("text/plain\nsecret: x", "invalid", id="line-break"),
        pytest.param("text/plain;charset=utf-8", "invalid", id="parameters"),
        pytest.param("bild/über", "invalid", id="not-ascii"),
    ],
)
def test_the_failure_warning_logs_only_a_well_formed_mime_type(
    caplog: pytest.LogCaptureFixture, mime_type: str, logged: str
) -> None:
    store = _build_store_raising(OSError("refused"))
    case = _build_case(_build_inline_part(_PNG, mime_type))

    with caplog.at_level(logging.WARNING, logger=trajectory_files.__name__):
        store_trajectory_files(case, store)

    [record] = caplog.records
    assert record.getMessage() == (
        f"Failed to store a trajectory file ({logged}, {len(_PNG)} bytes): OSError."
    )


def test_a_write_error_for_a_tool_result_file_keeps_its_bytes() -> None:
    nested = FunctionResponsePart(
        inline_data=FunctionResponseBlob(data=_PNG, mime_type="image/png")
    )
    case = _build_tool_result_case(nested)

    report = store_trajectory_files(case, _build_store_raising(OSError("x")))

    [part] = _list_parts(case)
    assert part.function_response is not None
    assert part.function_response.parts == [nested]
    assert (report.stored, report.not_stored, report.failed) == (0, 0, 1)
