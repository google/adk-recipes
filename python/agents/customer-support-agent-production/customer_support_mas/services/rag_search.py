"""
RAG-based Semantic Product Search
==================================
Uses vector embeddings for intelligent product search.
Includes retry logic with exponential backoff for transient errors.

NOTE: Currently using fallback cosine similarity search instead of Firestore vector index.
The Firestore vector search API (find_nearest) returns 0 results despite having:
- Vector index in READY state
- Products with 768-dimensional embeddings
- Correct Vector wrapper implementation

The fallback method computes cosine similarity manually for all products and works correctly.
This provides the same semantic search functionality without requiring the vector index.

To re-enable vector search in the future, modify the search() method to use find_nearest.
"""

import logging
import os
from typing import Dict, List, Optional

from google.cloud import firestore

logger = logging.getLogger(__name__)
from google import genai  # noqa: E402
from google.api_core import exceptions, retry  # noqa: E402

# Configure retry policy for transient errors
# Handles: Rate limits (429), Server errors (500), Service unavailable (503), Gateway timeout (504)
RETRY_POLICY = retry.Retry(
    initial=1.0,  # Initial delay: 1 second
    maximum=60.0,  # Maximum delay: 60 seconds
    multiplier=2.0,  # Exponential backoff multiplier
    deadline=300.0,  # Total deadline: 5 minutes
    predicate=retry.if_exception_type(
        exceptions.ResourceExhausted,  # 429 Rate Limit
        exceptions.ServiceUnavailable,  # 503 Service Unavailable
        exceptions.DeadlineExceeded,  # 504 Gateway Timeout
        exceptions.InternalServerError,  # 500 Internal Server Error
        exceptions.TooManyRequests,  # 429 Too Many Requests
    ),
)


class RAGProductSearch:
    """Semantic search for products using vector embeddings with retry logic."""

    def __init__(self, database_id: str, location: str = "us-central1"):
        self.database_id = database_id
        self.location = location

        # Initialize Firestore (uses ADC for project)
        self.db = firestore.Client(database=database_id)

        # Initialize genai client for embeddings
        self._genai_client = genai.Client(vertexai=True, location=location)

    def _generate_embedding_with_retry(self, text: str) -> List[float]:
        """
        Generate embedding with automatic retry on transient errors.

        Implements exponential backoff for:
        - Rate limiting (429)
        - Service unavailability (503)
        - Gateway timeouts (504)
        - Internal server errors (500)

        Args:
            text: Text to generate embedding for

        Returns:
            List of embedding values
        """

        @RETRY_POLICY
        def _generate():
            result = self._genai_client.models.embed_content(model="text-embedding-004", contents=[text])
            return result.embeddings[0].values

        try:
            return _generate()
        except Exception as e:
            logger.debug(f"[RAG] Embedding generation failed after retries: {e}")
            raise

    def _extract_category_keywords(self, query: str) -> List[str]:
        """
        Extract category keywords from query to filter results.

        Returns list of keywords that should match product name/category.
        """
        query_lower = query.lower()

        # Category keyword mapping
        category_keywords = {
            "laptop": ["laptop", "notebook", "computer"],
            "keyboard": ["keyboard"],
            "mouse": ["mouse"],
            "monitor": ["monitor", "display", "screen"],
            "desk": ["desk"],
            "chair": ["chair", "seating"],
            "headset": ["headset", "headphone"],
            "webcam": ["webcam", "camera"],
            "microphone": ["microphone", "mic"],
        }

        # Find matching categories
        keywords = []
        for key, vals in category_keywords.items():
            if key in query_lower:
                keywords.extend(vals)

        return keywords

    def _filter_by_category(self, products: List[Dict], query: str) -> List[Dict]:
        """
        Filter products by category relevance to query.

        Strictly filters - only returns products matching category keywords.
        """
        category_keywords = self._extract_category_keywords(query)

        if not category_keywords:
            return products  # No category filtering needed

        logger.debug(f"[RAG] Filtering by category keywords: {category_keywords}")

        # Score products by category match (STRICT filtering)
        filtered = []
        for product in products:
            name = product.get("name", "").lower()
            category = product.get("category", "").lower()
            description = product.get("description", "").lower()

            # Check if any category keyword matches in name (primary)
            match_score = 0
            for keyword in category_keywords:
                # Prioritize matches in product name
                if keyword in name:
                    match_score += 3  # Strong match
                elif keyword in description:
                    match_score += 2  # Medium match
                elif keyword in category:
                    match_score += 1  # Weak match

            # STRICT: Only include products with category match
            if match_score > 0:
                product["category_match_score"] = match_score
                filtered.append(product)
                logger.debug(f"[RAG]   ✓ Included: {name} (score={match_score})")
            else:
                logger.debug(f"[RAG]   ✗ Excluded: {name} (no keyword match)")

        # Sort by category match first, then similarity
        filtered.sort(key=lambda x: (x.get("category_match_score", 0), x.get("similarity", 0)), reverse=True)

        logger.debug(f"[RAG] Category filter: {len(products)} → {len(filtered)} products")

        return filtered

    def _extract_price_constraint(self, query: str) -> Optional[float]:
        """
        Extract maximum price constraint from query.

        Examples:
        - "laptops under $600" → 600.0
        - "products below 500" → 500.0
        - "under $1000" → 1000.0
        """
        import re

        query_lower = query.lower()

        # Pattern: "under $X", "below $X", "less than $X", "cheaper than $X"
        patterns = [
            r"under\s*\$?(\d+)",
            r"below\s*\$?(\d+)",
            r"less than\s*\$?(\d+)",
            r"cheaper than\s*\$?(\d+)",
            r"max\s*\$?(\d+)",
            r"maximum\s*\$?(\d+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, query_lower)
            if match:
                return float(match.group(1))

        return None  # No price constraint found

    def search(self, query: str, limit: int = 5, max_price: Optional[float] = None) -> List[Dict]:
        """
        Semantic search for products with intelligent category and price filtering.

        Args:
            query: Natural language search query
            limit: Maximum number of results
            max_price: Optional maximum price filter (extracted from query if not provided)

        Returns:
            List of matching products with scores
        """
        # Extract price constraint from query if not provided
        if max_price is None:
            max_price = self._extract_price_constraint(query)

        if max_price:
            logger.debug(f"[RAG] Filtering by price: max ${max_price}")

        logger.debug(f"[RAG] Search query: '{query}'")

        # Generate query embedding with retry logic
        try:
            query_embedding = self._generate_embedding_with_retry(query)
        except Exception as e:
            logger.debug(f"[RAG] Failed to generate embedding, using fallback: {e}")
            # Fallback: return empty results rather than crashing
            return []

        # Try vector search (requires Firestore vector index)
        # TEMPORARILY DISABLED - using fallback until vector index issue resolved
        logger.debug("[RAG] Using fallback cosine similarity search (vector index not working)")
        return self._fallback_search(query_embedding, limit, query, max_price)

    def _fallback_search(
        self, query_embedding: List[float], limit: int, query: str, max_price: Optional[float] = None
    ) -> List[Dict]:
        """
        Fallback search using manual cosine similarity with category filtering.
        Use this if Firestore vector index isn't set up yet.
        """
        import numpy as np

        all_products = []
        query_vec = np.array(query_embedding)

        # Get all products with embeddings
        for doc in self.db.collection("products").stream():
            data = doc.to_dict()

            if "embedding" not in data:
                continue

            # Calculate cosine similarity
            product_vec = np.array(data["embedding"])
            similarity = np.dot(query_vec, product_vec) / (np.linalg.norm(query_vec) * np.linalg.norm(product_vec))

            all_products.append(
                {
                    "id": doc.id,
                    "name": data.get("name"),
                    "price": data.get("price"),
                    "category": data.get("category"),
                    "description": data.get("description"),
                    "similarity": float(similarity),
                }
            )

        # Sort by similarity
        all_products.sort(key=lambda x: x["similarity"], reverse=True)

        # Get more results for filtering
        top_products = all_products[: limit * 3]

        # Filter by category
        filtered = self._filter_by_category(top_products, query)

        # Filter by price if constraint exists
        if max_price:
            before_price_filter = len(filtered)
            filtered = [p for p in filtered if p.get("price", float("inf")) <= max_price]
            logger.debug(
                f"[RAG] After price filter: {before_price_filter} → {len(filtered)} products under ${max_price}"
            )
            if filtered:
                logger.debug(f"[RAG] Products under ${max_price}:")
                for p in filtered:
                    logger.debug(f"[RAG]   - {p.get('name')}: ${p.get('price')}")

        # If filtering removed all results, fall back to original
        if not filtered:
            logger.debug("[RAG] Filters removed all results, using unfiltered")
            return all_products[:limit]

        return filtered[:limit]


# Global instance (initialized lazily)
_rag_search = None


def get_rag_search() -> RAGProductSearch:
    """Get or create RAG search instance."""
    global _rag_search
    if _rag_search is None:
        database_id = os.environ.get("FIRESTORE_DATABASE", "customer-support-db")
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
        _rag_search = RAGProductSearch(database_id, location)
    return _rag_search
