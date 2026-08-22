from app.agent.tools.product import query_product


def test_category_words_in_natural_language_match_catalog_product() -> None:
    result = query_product("性能好的手机")

    assert result["success"] is True
    assert [product["product_id"] for product in result["products"]] == [
        "PHONE-MI14U-BK"
    ]


def test_more_specific_product_query_filters_partial_matches() -> None:
    result = query_product("AirPods Pro 2")

    assert result["success"] is True
    assert [product["product_id"] for product in result["products"]] == [
        "ELEC-APP-002"
    ]


def test_unknown_product_does_not_fabricate_catalog_data() -> None:
    result = query_product("露营帐篷")

    assert result == {
        "success": False,
        "products": [],
        "error": "未找到与「露营帐篷」匹配的商品",
    }
