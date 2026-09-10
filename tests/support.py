import importlib
import sys
import types
from pathlib import Path

package = types.ModuleType("knowledge_test_plugin")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault(package.__name__, package)
models = importlib.import_module(package.__name__ + ".models")
store_mod = importlib.import_module(package.__name__ + ".store")
service_mod = importlib.import_module(package.__name__ + ".service")


class Backend:
    def __init__(self):
        self.uploads = 0
        self.docs = {}
        self.fail_verify = False
        self.fail_delete = False
        self.helper = types.SimpleNamespace(kb=types.SimpleNamespace(kb_id="kb-test"))

    async def ensure(self, project, **kwargs):
        return self.helper

    async def catalog(self, blocked_ids=()):
        return (
            [{"id": "kb-test", "name": "Test model config"}] if "kb-test" not in blocked_ids else []
        )

    async def upload(self, helper, filename, body):
        if filename not in self.docs:
            self.uploads += 1
            self.docs[filename] = body
        return filename

    async def verify(self, helper, doc_id, query):
        if self.fail_verify or doc_id not in self.docs:
            raise models.KnowledgeError("索引验证失败")

    async def delete(self, helper, doc_id):
        if self.fail_delete:
            raise OSError("simulated failure")
        self.docs.pop(doc_id, None)

    async def search(self, helper, query, allowed, limit=5):
        return [
            {"doc_id": d, "content": body, "score": 1.0}
            for d, body in self.docs.items()
            if d in allowed
        ][:limit]


def topic(
    mid="msg1",
    cid="topic-A",
    scope="scope1",
    actor="alice",
    request="保存到 Idol Empire，仅 AppLovin",
):
    return models.TopicContext(
        scope, cid, "owner", mid, actor, request, [{"role": "user", "content": "已经确认标准"}], []
    )


def plan(**changes):
    values = dict(
        project="Idol Empire",
        platform="AppLovin",
        kind="rule",
        title="测试标准",
        content="已确认的测试标准。",
        description="测试判定标准",
        reason="讨论确认",
        create_project=True,
    )
    values.update(changes)
    return models.Plan.parse(values)


async def allow(*args):
    return {"allow": True, "explicit_project": True}
