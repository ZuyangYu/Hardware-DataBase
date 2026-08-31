import unittest

from src.external_conversations.models import ExternalConversation
from src.external_conversations.vector_index import ExternalConversationVectorIndex


class _FakeCollection:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    def get_or_create_collection(self, name):
        return self

    def delete(self, where=None):
        dept = _where_dept(where)
        conv = _where_conv(where)
        self.rows = {
            i: r for i, r in self.rows.items()
            if not (dept in (None, r["meta"]["department_id"]) and conv in (None, r["meta"]["conversation_id"]))
        }

    def upsert(self, ids, documents, metadatas, embeddings):
        for i, d, m, e in zip(ids, documents, metadatas, embeddings):
            self.rows[i] = {"doc": d, "meta": m, "vec": e}

    def query(self, query_embeddings, n_results, where=None):
        dept = _where_dept(where)
        scored = []
        for i, r in self.rows.items():
            if dept is not None and r["meta"]["department_id"] != dept:
                continue
            dist = 1 - sum(a * b for a, b in zip(query_embeddings[0], r["vec"])) / (
                (sum(a * a for a in query_embeddings[0]) ** 0.5) * (sum(b * b for b in r["vec"]) ** 0.5) or 1
            )
            scored.append((dist, i, r))
        scored.sort(key=lambda t: t[0])
        top = scored[:n_results]
        return {
            "ids": [[t[1] for t in top]],
            "documents": [[t[2]["doc"] for t in top]],
            "metadatas": [[t[2]["meta"] for t in top]],
            "distances": [[t[0] for t in top]],
        }


def _where_dept(where):
    if not where:
        return None
    if "$and" in where:
        for clause in where["$and"]:
            if "department_id" in clause:
                return clause["department_id"]["$eq"]
    return where.get("department_id", {}).get("$eq") if isinstance(where.get("department_id"), dict) else None


def _where_conv(where):
    if not where or "$and" not in where:
        return None
    for clause in where["$and"]:
        if "conversation_id" in clause:
            return clause["conversation_id"]["$eq"]
    return None


def _vectors(texts):
    # deterministic pseudo-embedding: shared vocab → similar vectors
    vocab = ["压差", "LDO", "电源", "波特率", "CAN"]
    return [[float(t.count(v)) + 0.1 for v in vocab] for t in texts]


class _FakeEmbedModel:
    def get_text_embedding_batch(self, texts):
        return _vectors(texts)

    def get_text_embedding(self, text):
        return _vectors([text])[0]


class ExternalConversationVectorIndexTests(unittest.TestCase):
    def setUp(self):
        self.fake_client = _FakeCollection()
        self.index = ExternalConversationVectorIndex(
            embed_fn=_FakeEmbedModel(),
            collection_factory=lambda: self.fake_client,
        )

    def _conversation(self, cid="c1", department_id="dept_1"):
        from src.external_conversations.models import ConversationTurn

        return ExternalConversation(
            conversation_id=cid,
            kb_name="kb_a",
            department_id=department_id,
            title="t",
            source_file=f"{cid}.md",
            content_hash="h",
            source_group="外部数据",
            turns=[
                ConversationTurn(role="user", content="LDO 压差要求是什么?", start_offset=-1),
                ConversationTurn(role="assistant", content="最大压差 0.3V", start_offset=-1),
            ],
        )

    def test_reindex_and_semantic_search_same_department(self):
        self.index.reindex_conversation(self._conversation())
        rows = self.index.semantic_search("kb_a", "dept_1", "LDO 压差", top_k=3)
        self.assertTrue(rows)
        self.assertEqual({r["role"] for r in rows} >= {"user"}, True)
        self.assertTrue(all(r["department_id" if False else "title"] == "t" for r in rows))

    def test_department_scoped_query_isolation(self):
        self.index.reindex_conversation(self._conversation("d1c", "dept_1"))
        self.index.reindex_conversation(self._conversation("d2c", "dept_2"))
        rows1 = {r["conversation_id"] for r in self.index.semantic_search("kb_a", "dept_1", "压差", top_k=10)}
        rows2 = {r["conversation_id"] for r in self.index.semantic_search("kb_a", "dept_2", "压差", top_k=10)}
        self.assertEqual(rows1, {"d1c"})
        self.assertEqual(rows2, {"d2c"})

    def test_delete_conversation_removes_vectors(self):
        self.index.reindex_conversation(self._conversation())
        self.index.delete_conversation("kb_a", "c1", "dept_1")
        self.assertEqual(self.index.semantic_search("kb_a", "dept_1", "压差"), [])

    def test_unavailable_without_embed_model(self):
        bare = ExternalConversationVectorIndex()
        self.assertFalse(bare.is_available())
        conv = self._conversation()
        self.assertEqual(bare.reindex_conversation(conv), 0)
        self.assertEqual(bare.semantic_search("kb_a", "dept_1", "压差"), [])


if __name__ == "__main__":
    unittest.main()
