"""Short-term conversation memory: Redis for speed, PostgreSQL as the durable copy."""

from redis.exceptions import ConnectionError as RedisConnectionError

from app.models import ChatSession
from app.services.memory_service import ConversationMemory


class FakeRedis:
    """Just the list commands the memory uses, kept in a dict."""

    def __init__(self, *, broken: bool = False) -> None:
        self.lists: dict[str, list[str]] = {}
        self.ttl: dict[str, int] = {}
        self.broken = broken

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        if self.broken:
            raise RedisConnectionError("Redis is down")
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start : end + 1]

    def pipeline(self, transaction: bool = True) -> "FakePipeline":
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis, self.ops = redis, []

    async def __aenter__(self) -> "FakePipeline":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __getattr__(self, name: str):
        return lambda *args: self.ops.append((name, args))

    async def execute(self) -> None:
        if self.redis.broken:
            raise RedisConnectionError("Redis is down")
        lists = self.redis.lists
        for name, args in self.ops:
            key = args[0]
            if name == "delete":
                lists.pop(key, None)
            elif name == "rpush":
                lists.setdefault(key, []).extend(args[1:])
            elif name == "rpushx" and key in lists:
                lists[key].extend(args[1:])
            elif name == "ltrim" and key in lists:
                lists[key] = lists[key][args[1] :]
            elif name == "expire" and key in lists:
                self.redis.ttl[key] = args[1]


async def _conversation(session_factory, session_id: str = "s-1") -> str:
    async with session_factory() as session:
        session.add(ChatSession(id=session_id, shop_id="SHOP-001"))
        await session.commit()
    return session_id


async def test_messages_survive_without_redis(session_factory) -> None:
    session_id = await _conversation(session_factory)
    memory = ConversationMemory(session_factory, None, turns=3)

    for n in range(5):
        await memory.append(session_id, "user" if n % 2 == 0 else "assistant", f"message {n}")

    recent = await memory.recent(session_id)
    assert [m["content"] for m in recent] == ["message 2", "message 3", "message 4"]
    assert [m["role"] for m in recent] == ["user", "assistant", "user"]


async def test_redis_is_filled_from_the_database_and_then_used(session_factory) -> None:
    session_id = await _conversation(session_factory)
    redis = FakeRedis()
    memory = ConversationMemory(session_factory, redis, turns=4, ttl_seconds=60)

    await memory.append(session_id, "user", "first")  # no list in Redis yet: not cached
    assert redis.lists == {}
    assert [m["content"] for m in await memory.recent(session_id)] == ["first"]  # refilled
    await memory.append(session_id, "assistant", "second")  # now added to the cached list

    key = ConversationMemory.key(session_id)
    assert len(redis.lists[key]) == 2 and redis.ttl[key] == 60
    redis.lists[key][0] = redis.lists[key][0].replace("first", "from redis")
    assert [m["content"] for m in await memory.recent(session_id)] == ["from redis", "second"]


async def test_a_redis_outage_falls_back_to_the_database(session_factory) -> None:
    session_id = await _conversation(session_factory)
    memory = ConversationMemory(session_factory, FakeRedis(broken=True), turns=4)

    await memory.append(session_id, "user", "still saved")

    assert [m["content"] for m in await memory.recent(session_id)] == ["still saved"]


async def test_conversations_are_kept_apart(session_factory) -> None:
    first = await _conversation(session_factory, "s-a")
    second = await _conversation(session_factory, "s-b")
    memory = ConversationMemory(session_factory, FakeRedis())

    await memory.append(first, "user", "about CUST-0001")
    await memory.append(second, "user", "about CUST-0002")

    assert [m["content"] for m in await memory.recent(first)] == ["about CUST-0001"]
    assert [m["content"] for m in await memory.recent(second)] == ["about CUST-0002"]
