"""
Product-related tools for the customer support system.

This module contains all tools for product search, details, inventory, and reviews.
"""

import logging

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

# Import database client
from customer_support_mas.database import db_client  # noqa: E402

# Import validation utilities
from customer_support_mas.validation import (  # noqa: E402
    validate_product_id,
    validate_search_query,
    validation_error_response,
)

# Import RAG search
try:
    from customer_support_mas.services import get_rag_search

    USE_RAG = True
    logger.debug("RAG search enabled in product_tools")
except Exception as e:
    logger.debug("RAG search not available in product_tools: %s", e)
    USE_RAG = False


def search_products(query: str, tool_context: ToolContext) -> dict:
    """Search for products using RAG (semantic) or keyword fallback.

    Automatically saves the first result to session state for follow-up questions.

    Args:
        query: Search query string
        tool_context: ADK ToolContext (automatically injected)
    """
    # Input validation
    is_valid, error_msg = validate_search_query(query)
    if not is_valid:
        return validation_error_response(error_msg)

    if USE_RAG:
        # Use RAG semantic search
        try:
            rag = get_rag_search()
            products = rag.search(query, limit=5)

            if products:
                # Save first product to persistent session state
                if len(products) > 0 and "id" in products[0]:
                    tool_context.state["last_product_id"] = products[0]["id"]
                    tool_context.state["last_product_name"] = products[0].get("name", "")
                    tool_context.state["last_search_query"] = query
                    logger.debug("Session state saved: %s - %s", products[0]["id"], products[0].get("name"))

                # Save ALL product IDs for multi-product details loop
                product_ids = [p["id"] for p in products if "id" in p]
                tool_context.state["products_to_detail"] = product_ids
                tool_context.state["detailed_product_ids"] = []  # Reset for new search
                logger.debug("Saved %d product IDs for multi-detail: %s", len(product_ids), product_ids)

                return {"status": "success", "products": products, "count": len(products), "method": "RAG"}
            # RAG returned empty - fall through to keyword search
            logger.debug("RAG returned no results, falling back to keyword search")

        except Exception as e:
            logger.warning("RAG search failed: %s, falling back to keyword", e)

    # Fallback: keyword search with plural/singular handling
    results = []
    query_lower = query.lower().strip()
    search_terms = [query_lower]
    if query_lower.endswith("s"):
        search_terms.append(query_lower[:-1])
    else:
        search_terms.append(query_lower + "s")

    for doc in db_client.collection("products").stream():
        data = doc.to_dict()
        name = data.get("name", "").lower()
        category = data.get("category", "").lower()
        keywords = data.get("keywords", [])

        match = any(term in name or term in category or term in keywords for term in search_terms)

        if match:
            results.append(
                {"id": doc.id, "name": data.get("name"), "price": data.get("price"), "category": data.get("category")}
            )

    if results:
        # Save first result to persistent session state
        if len(results) > 0:
            tool_context.state["last_product_id"] = results[0]["id"]
            tool_context.state["last_product_name"] = results[0]["name"]
            tool_context.state["last_search_query"] = query
            logger.debug("Session state saved: %s - %s", results[0]["id"], results[0]["name"])

        # Save ALL product IDs for multi-product details loop
        product_ids = [p["id"] for p in results if "id" in p]
        tool_context.state["products_to_detail"] = product_ids
        tool_context.state["detailed_product_ids"] = []  # Reset for new search
        logger.debug("Saved %d product IDs for multi-detail: %s", len(product_ids), product_ids)

        return {"status": "success", "products": results, "count": len(results), "method": "keyword"}
    return {"status": "no_results", "message": f"No products found matching '{query}'"}


def get_product_details(product_id: str) -> dict:
    """Get detailed information about a specific product by its ID.

    Args:
        product_id: The product ID (e.g., "PROD-001")
    """
    # Input validation
    is_valid, error_msg = validate_product_id(product_id)
    if not is_valid:
        return validation_error_response(error_msg)

    doc = db_client.collection("products").document(product_id).get()
    if doc.exists:
        data = doc.to_dict()
        # Remove embedding from response (too large)
        data.pop("embedding", None)
        return {"status": "success", "product": {"id": doc.id, **data}}
    return {"status": "not_found", "message": f"Product {product_id} not found"}


def get_last_mentioned_product(tool_context: ToolContext) -> dict:
    """IMPORTANT: Use this tool when customer asks for details about a product you just showed them.

    Triggers: "yes", "yes please", "sure", "ok", "tell me more", "details", "get details",
              "more info", "this one", "that one", "show me details", "I want details"

    This tool requires NO parameters - it automatically retrieves the last product from session state.
    DO NOT ask "which product?" - just call this tool directly!

    Args:
        tool_context: ADK ToolContext (automatically injected)
    """
    # Read from persistent session state (safe with default)
    last_product_id = tool_context.state.get("last_product_id")
    last_product_name = tool_context.state.get("last_product_name", "Unknown")

    logger.debug("get_last_mentioned_product: product_id=%s, product_name=%s", last_product_id, last_product_name)

    if not last_product_id:
        return {"status": "error", "message": "No product was recently discussed. Please search for a product first."}

    # Fetch the product details
    doc = db_client.collection("products").document(last_product_id).get()
    if doc.exists:
        data = doc.to_dict()
        data.pop("embedding", None)
        return {
            "status": "success",
            "product": {"id": doc.id, **data},
            "context_note": f"This is the {last_product_name} you asked about.",
        }

    return {"status": "not_found", "message": f"Product {last_product_id} not found"}


def check_inventory(product_id: str) -> dict:
    """Check inventory levels.

    Args:
        product_id: The product ID to check inventory for
    """
    # Input validation
    is_valid, error_msg = validate_product_id(product_id)
    if not is_valid:
        return validation_error_response(error_msg)

    doc = db_client.collection("inventory").document(product_id).get()
    if doc.exists:
        return {"status": "success", "inventory": {"product_id": doc.id, **doc.to_dict()}}
    return {"status": "not_found"}


def get_product_reviews(product_id: str) -> dict:
    """Get customer reviews for a product.

    Args:
        product_id: The product ID to get reviews for
    """
    # Input validation
    is_valid, error_msg = validate_product_id(product_id)
    if not is_valid:
        return validation_error_response(error_msg)

    doc = db_client.collection("reviews").document(product_id).get()
    if doc.exists:
        return {"status": "success", "reviews": {"product_id": doc.id, **doc.to_dict()}}
    return {"status": "not_found"}


def get_all_saved_products_info(tool_context: ToolContext) -> dict:
    """
    Get comprehensive information for ALL products from the last search.

    This tool retrieves all product IDs saved in session state and fetches
    comprehensive information (details + inventory + reviews) for each.

    **Use this tool when:**
    - User asks for "details on all of them", "all three", "both", "show me all"
    - User wants information about multiple products from the previous search

    This is MORE EFFICIENT than using LoopAgent because it fetches directly
    without iteration overhead and timeout issues.

    Args:
        tool_context: ADK ToolContext (automatically injected)

    Returns:
        Dictionary with comprehensive info for all saved products
    """
    products_to_detail = tool_context.state.get("products_to_detail", [])

    if not products_to_detail:
        return {"status": "error", "message": "No products were recently searched. Please search for products first."}

    logger.debug("Fetching info for %d products: %s", len(products_to_detail), products_to_detail)

    results = {"status": "success", "count": len(products_to_detail), "products": []}

    # Fetch comprehensive info for each product
    for product_id in products_to_detail:
        product_info = get_product_info(product_id)
        if product_info.get("status") == "success":
            results["products"].append(product_info)
        else:
            results["products"].append(
                {"product_id": product_id, "status": "not_found", "message": f"Product {product_id} not found"}
            )

    logger.debug("Successfully fetched %d products", len(results["products"]))

    return results


def get_product_info(
    product_id: str, include_details: bool = True, include_inventory: bool = True, include_reviews: bool = True
) -> dict:
    """
    Smart unified product information fetcher with automatic comprehensive data retrieval.

    **DEFAULT BEHAVIOR**: Fetches ALL information (details + inventory + reviews) for complete product info.
    This is the RECOMMENDED tool for most product queries as it provides comprehensive information efficiently.

    **Use this tool when:**
    - User asks for product information (any details about a product)
    - User mentions "full details", "everything", "complete info"
    - User explicitly asks for inventory, reviews, or stock levels
    - User wants comprehensive product data

    **Only use individual tools (get_product_details, check_inventory, get_product_reviews) when:**
    - User explicitly says "ONLY details" or "JUST the basic info"
    - User specifically requests a single piece of information

    Args:
        product_id: The product ID (e.g., "PROD-001")
        include_details: Whether to fetch product details (default: True)
        include_inventory: Whether to fetch inventory levels (default: True)
        include_reviews: Whether to fetch customer reviews (default: True)

    Returns:
        Comprehensive product information with all requested data
    """
    logger.debug(
        "get_product_info called for %s (details=%s, inventory=%s, reviews=%s)",
        product_id,
        include_details,
        include_inventory,
        include_reviews,
    )

    result = {"status": "success", "product_id": product_id, "data_fetched": [], "fetch_method": "comprehensive"}

    # Fetch details
    if include_details:
        details = get_product_details(product_id)
        if details.get("status") == "success":
            result["details"] = details.get("product", {})
            result["data_fetched"].append("details")
        else:
            result["details_error"] = "Product not found"

    # Fetch inventory
    if include_inventory:
        inventory = check_inventory(product_id)
        if inventory.get("status") == "success":
            result["inventory"] = inventory.get("inventory", {})
            result["data_fetched"].append("inventory")
        else:
            result["inventory_error"] = "Inventory not found"

    # Fetch reviews
    if include_reviews:
        reviews = get_product_reviews(product_id)
        if reviews.get("status") == "success":
            result["reviews"] = reviews.get("reviews", {})
            result["data_fetched"].append("reviews")
        else:
            result["reviews_error"] = "Reviews not found"

    # Update status if nothing was found
    if not result["data_fetched"]:
        result["status"] = "not_found"
        result["message"] = f"No information found for product {product_id}"

    logger.debug("Successfully fetched: %s", ", ".join(result["data_fetched"]))

    return result
