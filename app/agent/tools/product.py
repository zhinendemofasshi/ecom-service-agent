from app.agent.tools.mock_data import PRODUCTS


def _match_score(product: dict, keywords: list[str], raw_query: str) -> int:
    """计算商品与关键词列表的匹配度（命中关键词数量）。"""
    searchable = " ".join([
        product["name"],
        product["category"],
        product.get("description", ""),
        " ".join(str(v) for v in product.get("specs", {}).values()),
    ]).lower()
    keyword_hits = sum(1 for kw in keywords if kw in searchable)
    product_name = product["name"].lower()
    name_bonus = 5 if raw_query in product_name or product_name in raw_query else 0
    category = product["category"].lower()
    category_bonus = 3 if category and category in raw_query else 0
    return keyword_hits + name_bonus + category_bonus


def query_product(keyword: str) -> dict:
    """根据商品名称关键词或商品ID查询商品信息，包括价格、库存、规格等。"""
    if keyword in PRODUCTS:
        return {"success": True, "products": [PRODUCTS[keyword]]}

    keywords = [kw.lower() for kw in keyword.split() if kw.strip()]
    if not keywords:
        keywords = [keyword.lower()]

    raw_query = keyword.lower()
    scored = [(p, _match_score(p, keywords, raw_query)) for p in PRODUCTS.values()]
    best_score = max((score for _, score in scored), default=0)
    results = [p for p, score in scored if score == best_score and score > 0]

    if not results:
        return {
            "success": False,
            "products": [],
            "error": f"未找到与「{keyword}」匹配的商品",
        }
    return {"success": True, "products": results}
