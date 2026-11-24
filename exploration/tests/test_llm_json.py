"""extract_json_object recovers a JSON object from prose-wrapped model output."""

import json

from exploration.backend.llm import extract_json_object


def test_recovers_object_wrapped_in_prose():
    text = 'Here is the result: {"relationships": [], "gaps": []} hope that helps'
    assert json.loads(extract_json_object(text)) == {"relationships": [], "gaps": []}


def test_recovers_object_in_code_fence():
    text = "```json\n{\"a\": 1}\n```"
    assert json.loads(extract_json_object(text)) == {"a": 1}


def test_braces_inside_strings_do_not_end_the_object():
    text = '{"nested": {"x": "has } brace"}, "ok": true}'
    assert json.loads(extract_json_object(text)) == {"nested": {"x": "has } brace"}, "ok": True}


def test_plain_object_is_unchanged():
    assert json.loads(extract_json_object('{"a": 1}')) == {"a": 1}


# --------------------------------------------------------------------------- offline model output
# Live runs on 2026-11-14 and 2026-11-20 recorded "LLM extraction failed: JSONDecodeError".
# These cases replay malformed and truncated answers through the real client and extractor
# with an in-process HTTP stub. No request leaves the process.

PAGE = "NVIDIA has invested $2 billion in CoreWeave. The buildout adds GPU capacity."
QUESTION = "How do NVIDIA's investments reinforce capacity?"


def _stub(monkeypatch, content, *, finish_reason="stop", usage=None):
    import httpx

    sent = {}
    body = {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}
    if usage is not None:
        body["usage"] = usage

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return body

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, headers=None, json=None):
            sent["url"], sent["payload"] = url, json
            return _Response()

    monkeypatch.setattr(httpx, "Client", _Client)
    return sent


def _extract(content=PAGE):
    from exploration.backend.extraction import extract_document
    from exploration.backend.llm import OpenAICompatibleClient

    client = OpenAICompatibleClient(api_key="offline-test-placeholder", base_url="https://api.anthropic.com/v1", timeout=1)
    return extract_document(
        content,
        url="https://example.test/page",
        document_id="doc_t",
        version_id="ver_t",
        version_hash="hash_t",
        question=QUESTION,
        llm=client,
    )


def _rel(quote, obj="CoreWeave"):
    return {"subject": "NVIDIA", "predicate": "invested_in", "object": obj, "quote": quote, "kind": "factual"}


def test_truncated_model_output_reports_why_it_failed(monkeypatch):
    _stub(monkeypatch, '{"relationships": [{"subject": "NVIDIA", "quote": "NVIDIA has inv', finish_reason="length")
    batch = _extract("A page about GPU capacity with no disclosed investment sentence.")
    assert batch.live_extraction_blocked is True
    assert batch.relationships == []  # nothing is invented from a partial answer
    assert "LLMResponseNotJSON" in batch.blocker
    assert "finish_reason=length" in batch.blocker and "max_tokens=2048" in batch.blocker


import pytest  # noqa: E402


@pytest.mark.parametrize(
    "content",
    [
        '{"relationships": [{"subject": "NVIDIA",}], }',  # trailing commas
        "{'relationships': []}",  # single quotes
        "I could not find any relationships in this excerpt.",  # prose only
        "",  # empty answer
        '```json\n{"relationships": [\n```',  # fenced and unterminated
    ],
)
def test_malformed_model_output_is_a_labelled_blocker_not_a_crash(monkeypatch, content):
    _stub(monkeypatch, content)
    batch = _extract("A page about GPU capacity with no disclosed investment sentence.")
    assert batch.live_extraction_blocked is True
    assert batch.relationships == [] and batch.claims == []
    assert batch.blocker.startswith("LLM extraction failed: LLMResponseNotJSON")
    assert "finish_reason=stop" in batch.blocker


def test_null_message_content_is_a_blocker(monkeypatch):
    _stub(monkeypatch, None)
    batch = _extract("A page about GPU capacity with no disclosed investment sentence.")
    assert batch.live_extraction_blocked is True
    assert "LLMResponseNotJSON" in batch.blocker and "0 chars" in batch.blocker


def test_request_asks_for_a_bounded_answer_with_room_to_finish(monkeypatch):
    sent = _stub(monkeypatch, '{"relationships": []}')
    _extract("A page about GPU capacity with no disclosed investment sentence.")
    payload = sent["payload"]
    assert payload["max_tokens"] == 2048
    system = payload["messages"][0]["content"]
    assert "at most 8 relationships" in system and "300 characters" in system


def test_an_over_long_answer_is_capped_and_still_quote_verified(monkeypatch):
    import json as _json

    sentences = [f"NVIDIA has invested ${n} billion in Partner{n}." for n in range(1, 13)]
    page = " ".join(sentences)
    rels = [_rel(sentence, f"Partner{n}") for n, sentence in enumerate(sentences, start=1)]
    rels.append(_rel("NVIDIA has invested $99 billion in Nobody.", "Nobody"))  # not on the page
    _stub(monkeypatch, _json.dumps({"relationships": rels, "gaps": [{"text": f"gap {n}?", "importance": 0.5} for n in range(9)]}))
    batch = _extract(page)
    objects = {rel.object_name for rel in batch.relationships}
    assert objects == {f"Partner{n}" for n in range(1, 9)}  # the first 8, each exact-quote grounded
    assert "Nobody" not in objects
    assert len(batch.questions) <= 4


def test_token_usage_is_counted_when_only_the_parts_are_reported(monkeypatch):
    _stub(monkeypatch, '{"relationships": []}', usage={"prompt_tokens": 400, "completion_tokens": 120})
    batch = _extract("A page about GPU capacity with no disclosed investment sentence.")
    assert batch.llm_tokens == 520
