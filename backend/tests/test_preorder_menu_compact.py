"""Tests for LLM menu compaction (full catalog, nutrition, no UI noise)."""

from __future__ import annotations

import unittest

from app.services.preorder_service import (
    compact_menu_for_llm,
    parse_llm_menu_pick_json,
    preorder_menu_pick_response_format,
    slim_menu_item_for_llm,
)


def _sample_tree() -> dict:
    return {
        "items": [
            {
                "name": "Основное",
                "tree": [
                    {
                        "name": "Горячее",
                        "items": [
                            {
                                "menu_item_id": "aaa-111",
                                "title": "Салат Цезарь",
                                "price": 450,
                                "output": 250.0,
                                "output_measure": "г",
                                "product": {
                                    "name": "Салат Цезарь",
                                    "description": "С курицей",
                                    "measure_name": "порция",
                                    "cpfc": {
                                        "calories": 320,
                                        "proteins": 18,
                                        "fats": 22,
                                        "carbohydrates": 8,
                                    },
                                    "img": "https://example.com/x.jpg",
                                    "color": "#ff0000",
                                    "ingredients": [
                                        {
                                            "ingredient_product_class": {
                                                "name": "Курица",
                                                "evotor_id": "skip-me",
                                            }
                                        }
                                    ],
                                },
                            },
                            {
                                "menu_item_id": "bbb-222",
                                "title": "Вино",
                                "price": 900,
                                "product": {
                                    "name": "Вино",
                                    "Calories": 85,
                                    "Protein": 0.1,
                                    "Fat": 0,
                                    "Hydrocarbons": 2.5,
                                    "alcohol_by_volume": 12.5,
                                    "is_age_limited": True,
                                },
                            },
                        ],
                    }
                ],
            }
        ]
    }


class TestPreorderMenuCompact(unittest.TestCase):
    def test_compact_includes_all_positions(self) -> None:
        slim = compact_menu_for_llm(_sample_tree())
        self.assertEqual(len(slim), 2)
        ids = {r["id"] for r in slim}
        self.assertEqual(ids, {"aaa-111", "bbb-222"})

    def test_slim_keeps_nutrition_and_drops_ui_noise(self) -> None:
        item = _sample_tree()["items"][0]["tree"][0]["items"][0]
        row = slim_menu_item_for_llm(item, section="Основное", path="Горячее")
        self.assertEqual(row["nutrition"]["calories"], 320.0)
        self.assertEqual(row["nutrition"]["proteins"], 18.0)
        self.assertEqual(row["portion"]["output"], 250.0)
        self.assertEqual(row["ingredients"], ["Курица"])
        self.assertNotIn("img", row)
        self.assertNotIn("color", row)

    def test_pascal_case_nutrition_aliases(self) -> None:
        item = _sample_tree()["items"][0]["tree"][0]["items"][1]
        row = slim_menu_item_for_llm(item, section="Основное", path="Горячее")
        self.assertEqual(row["nutrition"]["calories"], 85.0)
        self.assertEqual(row["nutrition"]["carbohydrates"], 2.5)
        self.assertEqual(row["alcohol_by_volume"], 12.5)
        self.assertTrue(row["age_limited"])


class TestPreorderMenuPickParse(unittest.TestCase):
    def test_parse_markdown_wrapped_json_from_db_case(self) -> None:
        raw = """```json
{
  "items": [
    {
      "menu_item_id": "5f8f04f3-794a-4130-b501-d33628e25490",
      "quantity": 2
  },
    {
      "menu_item_id": "49fd3412-6160-4d82-b9fb-22e21996984f",
      "quantity": 2
    }
  ]
}
```"""
        picks = parse_llm_menu_pick_json(raw)
        self.assertEqual(len(picks), 2)
        self.assertEqual(picks[0]["menu_item_id"], "5f8f04f3-794a-4130-b501-d33628e25490")
        self.assertEqual(picks[0]["quantity"], 2)

    def test_preorder_menu_pick_response_format_schema(self) -> None:
        fmt = preorder_menu_pick_response_format(strict=True)
        self.assertEqual(fmt["response_format"]["type"], "json_schema")
        js = fmt["response_format"]["json_schema"]
        self.assertEqual(js["name"], "preorder_menu_pick")
        self.assertTrue(js["strict"])
        schema = js["schema"]
        self.assertEqual(schema["required"], ["items"])
        item_props = schema["properties"]["items"]["items"]["properties"]
        self.assertIn("menu_item_id", item_props)
        self.assertIn("quantity", item_props)


if __name__ == "__main__":
    unittest.main()
