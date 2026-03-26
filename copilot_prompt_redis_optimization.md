# PROMPT: Optimize redis.py — Replace Custom Serde with JsonPlusSerializer

You are a senior Python engineer working on a LangGraph-based orchestration platform. You need to refactor `redis.py` — the async Redis checkpoint backend — to replace a large, hand-rolled serialization layer with LangGraph's native `JsonPlusSerializer`.

---

## CONTEXT

The file `redis.py` lives at `src/runtime/state/backends/redis.py`. It is the async Redis checkpoint backend keyed by `thread_id`, with fakeredis fallback for tests. The class is `RedisCheckpointBackend`.

The current code has a **~200-line custom serialization layer** that manually handles LangChain message objects (`HumanMessage`, `AIMessage`, `SystemMessage`, `ToolMessage`), LangGraph `Send` objects, tuples, sets, datetimes, UUIDs, bytes, and `lc_serializable` objects. This layer uses `dumpd`/`load` from `langchain_core.load` and a recursive `_deep_serialize`/`_deep_deserialize` approach.

**The problem**: The `save()` method calls `json.dumps()` on the serialized checkpoint. When the state contains LangChain message objects (e.g., `HumanMessage`), `json.dumps()` throws:
```
TypeError: Object of type HumanMessage is not JSON serializable
```

**The solution** (per the Distinguished Principal Engineer who architected this system): Replace all custom serialization with `JsonPlusSerializer` from `langgraph.checkpoint.serde.jsonplus`. This is the native LangGraph checkpointer serializer. It uses `dumps_typed()` / `loads_typed()` which returns a `(type_tag: str, payload: bytes)` tuple. The payload is msgpack-encoded binary, not JSON.

---

## WHAT TO REMOVE

Delete ALL of the following from the file. These are the hand-rolled serialization helpers that `JsonPlusSerializer` replaces entirely:

1. **Import**: `from langchain_core.load import dumpd, load`
2. **Import**: `from langchain_core.messages import (AIMessage, HumanMessage, SystemMessage, ToolMessage)`
3. **Constant**: `_MESSAGE_TYPE_MAP` — the dict mapping type name strings to message classes
4. **Constant**: `_SEND_MARKER = "__langgraph_send__"`
5. **Constant**: `_LC_MESSAGE_MARKER = "__lc_message__"`
6. **Function**: `_serialize_message(msg)` — serializes a single LangChain message to a dict with type marker, using `dumpd()` with legacy fallback
7. **Function**: `_deserialize_message(data)` — reconstructs a LangChain message from a serialized dict, using `load()` with legacy reconstruction from `_MESSAGE_TYPE_MAP`
8. **Function**: `_deep_serialize(obj)` — recursive serializer handling Send, LC messages, dict, list, tuple, set, frozenset, datetime, UUID, bytes/bytearray/memoryview, and `lc_serializable` objects
9. **Function**: `_deserialize_send(data)` — reconstructs a LangGraph `Send` object
10. **Function**: `_deserialize_tuple(data)` — reconstructs tuples
11. **Function**: `_deserialize_set(data)` — reconstructs sets
12. **Function**: `_deserialize_datetime_value(data)` — reconstructs datetimes
13. **Function**: `_deserialize_uuid_value(data)` — reconstructs UUIDs
14. **Function**: `_deserialize_bytes_value(data)` — reconstructs bytes from base64
15. **Function**: `_try_deserialize_special_dict(data)` — dispatch table that routes marker-tagged dicts to the correct deserializer
16. **Function**: `_deep_deserialize(obj)` — recursive deserializer that walks the object tree and reconstructs all special types

**Keep everything else**: the `RedisTLSConfig` dataclass, `_parse_datetime`, `_resolve_env_template`, `_coerce_string`, `_write_pem_to_tempfile`, URL scheme constants, redis/fakeredis imports, the `RedisCheckpointBackend` class structure including `__init__`, `_load_app_config`, `_coerce_bool`, `_normalize_url`, `_truthy`, `_should_emulate`, `_build_client`, `_build_mtls_client`, `_normalize_thread_id`, `_key`, `save`, `load`, `delete`.

---

## WHAT TO ADD

### 1. New import (replace the removed imports)

```python
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
```

### 2. Module-level serde instance

```python
_serde = JsonPlusSerializer()
```

### 3. Two new helper functions (replace the ~15 deleted functions)

```python
def _serialize_state(state: Any) -> dict[str, Any]:
    """Serialize checkpoint state via JsonPlusSerializer.

    Stores the type tag and base64-encoded payload bytes so the outer
    envelope can still be persisted as JSON in Redis.
    """
    type_name, payload_bytes = _serde.dumps_typed(state)
    return {
        "type": type_name,
        "payload_b64": base64.b64encode(payload_bytes).decode("ascii"),
    }


def _deserialize_state(data: Any) -> Any:
    """Deserialize checkpoint state from JsonPlusSerializer format.

    Also handles legacy format (produced by the old _deep_serialize path)
    by falling back to returning the raw data as-is.
    """
    if isinstance(data, dict) and "type" in data and "payload_b64" in data:
        type_name = data["type"]
        payload_bytes = base64.b64decode(data["payload_b64"])
        return _serde.loads_typed((type_name, payload_bytes))
    # Legacy fallback: return raw dict (old checkpoints before migration)
    return data
```

### 4. Modify `_serialize()` in `RedisCheckpointBackend`

Change the `"state"` field from:
```python
"state": _deep_serialize(checkpoint.state),
```
To:
```python
"state": _serialize_state(checkpoint.state),
```

Also serialize metadata through the same path:
```python
"metadata": _serialize_state(checkpoint.metadata) if checkpoint.metadata else {},
```

### 5. Modify `_deserialize()` in `RedisCheckpointBackend`

Change the state reconstruction from:
```python
state = _deep_deserialize(payload.get("state")) if payload.get("state") else {}
```
To:
```python
state = _deserialize_state(payload.get("state")) if payload.get("state") else {}
```

Also deserialize metadata:
```python
raw_metadata = payload.get("metadata") or {}
metadata = _deserialize_state(raw_metadata) if raw_metadata else {}
if not isinstance(metadata, dict):
    metadata = {}
```

### 6. `save()` and `load()` — NO CHANGES NEEDED

The `save()` method still uses `json.dumps()` on the outer envelope, but this is now safe because `_serialize_state()` converts the state to a plain `{type, payload_b64}` dict before `json.dumps` ever sees it. No LangChain objects reach `json.dumps`.

The `load()` method still uses `json.loads()` and passes the result to `_deserialize()`, which now calls `_deserialize_state()` to reconstruct via `loads_typed`.

---

## DESIGN DECISIONS TO PRESERVE

1. **Legacy backwards compatibility**: The `_deserialize_state()` function checks for the new `{type, payload_b64}` format. If it doesn't find those keys, it returns the raw data as-is. This means old checkpoints stored with the `_deep_serialize` format will still load. They just won't round-trip through the new path until re-saved.

2. **Base64 encoding**: Since `dumps_typed()` returns raw bytes and the outer envelope uses `json.dumps()` (which can't handle bytes), the payload is base64-encoded. This is the pattern recommended by the architect. If Redis can store binary directly, a future optimization could use two Redis hash fields (`checkpoint:type` → `"msgpack"` and `checkpoint:data` → raw bytes) to skip the base64 overhead.

3. **No `json.dumps()` on LangChain objects**: This is the critical invariant. The `json.dumps()` call in `save()` only ever sees: strings, ints, None, and plain dicts containing strings. Never a `HumanMessage` or any Pydantic model.

4. **Thread safety**: `JsonPlusSerializer` instances are safe to share. The module-level `_serde` instance is fine.

---

## VALIDATION CHECKLIST

After making the changes, verify:

- [ ] `from langchain_core.load import dumpd, load` is GONE
- [ ] `from langchain_core.messages import ...` is GONE  
- [ ] `_MESSAGE_TYPE_MAP`, `_SEND_MARKER`, `_LC_MESSAGE_MARKER` are GONE
- [ ] All `_serialize_message`, `_deserialize_message`, `_deep_serialize`, `_deep_deserialize`, `_try_deserialize_special_dict`, and the 6 type-specific deserializers are GONE
- [ ] `from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer` is present
- [ ] `_serde = JsonPlusSerializer()` exists at module level
- [ ] `_serialize_state()` and `_deserialize_state()` exist and use `dumps_typed`/`loads_typed`
- [ ] `_serialize()` uses `_serialize_state()` for both state and metadata
- [ ] `_deserialize()` uses `_deserialize_state()` for both state and metadata
- [ ] `save()` still works with `json.dumps()` (no LangChain objects reach it)
- [ ] `load()` still works with `json.loads()` → `_deserialize()`
- [ ] Legacy checkpoints (without `type`/`payload_b64` keys) still load via fallback
- [ ] `ormsgpack` is in the project dependencies (required by `JsonPlusSerializer`)
- [ ] `langgraph-checkpoint >= 3.0.0` (patches CVE-2025-64439 RCE vulnerability in the json fallback mode)

---

## SECURITY NOTE

Older `langgraph-checkpoint` versions (< 3.0.0) had a critical RCE vulnerability (CVE-2025-64439) in the `JsonPlusSerializer`'s `"json"` fallback mode. The fix in 3.0.0 introduces an allow-list for constructor deserialization. Ensure your `pyproject.toml` or `requirements.txt` pins `langgraph-checkpoint >= 3.0.0`.

---

## EXPECTED RESULT

The refactored file should be roughly **200 lines shorter** than the original. The entire custom serialization/deserialization layer is replaced by two small functions (~20 lines total) that delegate to `JsonPlusSerializer`. The `RedisCheckpointBackend` class itself is unchanged except for the `_serialize()` and `_deserialize()` static methods.
