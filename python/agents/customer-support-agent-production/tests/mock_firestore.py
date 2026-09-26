"""
Mock Firestore Client for Deterministic Tests
================================================
In-memory fake Firestore that returns data from seed.py's get_sample_data().
"""

from customer_support_mas.database.fixtures import get_sample_data


class MockSnapshot:
    """Mimics a Firestore DocumentSnapshot."""

    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class MockDocument:
    """Mimics a Firestore DocumentReference."""

    def __init__(self, collection_data, doc_id):
        self._collection_data = collection_data
        self._doc_id = doc_id

    def get(self):
        data = self._collection_data.get(self._doc_id)
        return MockSnapshot(self._doc_id, data)

    def set(self, data):
        self._collection_data[self._doc_id] = data

    def delete(self):
        self._collection_data.pop(self._doc_id, None)


class MockQuery:
    """Mimics a Firestore Query (result of .where())."""

    def __init__(self, docs):
        self._docs = docs

    def stream(self):
        yield from self._docs

    def get(self):
        return list(self._docs)


class MockCollection:
    """Mimics a Firestore CollectionReference."""

    def __init__(self, data):
        # data is a dict of {doc_id: doc_data}
        self._data = data

    def document(self, doc_id):
        return MockDocument(self._data, doc_id)

    def stream(self):
        for doc_id, doc_data in self._data.items():
            yield MockSnapshot(doc_id, doc_data)

    def where(self, field_or_filter=None, op=None, value=None, *, filter=None, **kwargs):
        """Support both old-style and FieldFilter-style where queries.

        Old style: where("field", "==", "value")
        New style: where(filter=FieldFilter("field", "==", "value"))
        """
        # Handle FieldFilter-style (keyword argument)
        if filter is not None:
            field = filter.field_path
            op = filter.op_string
            value = filter.value
        # Handle old-style 3-argument form: where("field", "==", "value")
        elif op is not None and value is not None:
            field = field_or_filter
        # Handle FieldFilter passed as positional arg
        elif field_or_filter is not None and hasattr(field_or_filter, "field_path"):
            field = field_or_filter.field_path
            op = field_or_filter.op_string
            value = field_or_filter.value
        else:
            # Fallback for keyword args
            field = kwargs.get("field")
            op = kwargs.get("op")
            value = kwargs.get("value")

        results = []
        for doc_id, doc_data in self._data.items():
            if doc_data is None:
                continue
            doc_value = doc_data.get(field)
            if op == "==" and doc_value == value:
                results.append(MockSnapshot(doc_id, doc_data))

        return MockQuery(results)


class MockFirestoreClient:
    """In-memory Firestore client populated from seed data."""

    def __init__(self):
        self._collections = {}
        seed = get_sample_data()
        for name, docs in seed.items():
            self._collections[name] = dict(docs)  # copy

    def collection(self, name):
        if name not in self._collections:
            self._collections[name] = {}
        return MockCollection(self._collections[name])
