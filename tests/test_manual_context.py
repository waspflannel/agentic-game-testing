"""A complete controls guide fits both bounded gameplay selections in a dense UI."""
import json

import pytest

from ashad.ai import OpenAI, ProviderError
from ashad.models import MODEL
from ashad.store import Store
from ashad.structured import prompt_observation
from test_decisions import Backend, answers, context


@pytest.mark.asyncio
async def test_full_manual_survives_dense_target_and_action_selections(tmp_path):
    store = Store(tmp_path)
    store.put("runs", {"id": "manual-context", "model": MODEL})
    value = context()
    line = "Use exit_menu once; wait for the UI animation and verify closure. Source: scripts/ui/player_tablet.gd:62.\n"
    manual = (line * 400)[:32000]
    assert len(manual.encode("utf-8")) == 32000
    value["game_controls"] = manual
    entities = [{"id": str(i), "version": 1, "label": f"Inventory slot {i}: " + "x" * 1190,
                 "actions": ["ui_click", "ui_drag"], "state": {"contents": "item details " * 50}}
                for i in range(512)]
    value["observation"] = prompt_observation({**value["observation"], "entities": entities})
    assert value["observation"]["context_truncated"]
    assert 26000 <= len(json.dumps(value["observation"])) <= 28000

    def respond(payload):
        evidence = json.loads(payload["input"].split("\nSelected target:")[0])
        assert evidence["game_controls"] == manual
        request = {key: payload[key] for key in ("input", "questions")}
        assert len(json.dumps(request, ensure_ascii=False).encode("utf-8")) + 2048 <= 128000
        question = payload["questions"][0]
        assert len(question["choices"]) >= 512
        selected = "511" if question["name"] == "target" else question["choices"][-1]["value"]
        return answers(payload, {question["name"]: selected})

    backend = Backend(respond)
    ai = OpenAI(store, backend=backend)
    dispatched = []

    async def dispatch(name, args):
        if name == "list_entities":
            end = args["offset"] + args["limit"]
            return {"entities": entities[args["offset"]:end], "next_offset": end if end < len(entities) else None}
        dispatched.append((name, args))
        return {"status": "running"}

    try:
        result = await ai.player_turn(value, None, dispatch, lambda: True, "manual-context")
        assert result["status"] == "dispatched" and len(backend.calls) == 2
        assert len(dispatched) == 1
        assert dispatched[0][1]["target_id"] == "511"
        assert dispatched[0][1]["verb"] == "ui_drag"
        assert dispatched[0][1]["parameters"]["destination_id"] == "510"
        with pytest.raises(ProviderError) as failure:
            await ai.decisions("x" * 128000, backend.calls[0]["questions"])
        assert failure.value.status == "context_limit" and len(backend.calls) == 2
    finally:
        store.close()
