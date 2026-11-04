"""Normalize repeated identical mandatory singleton rows in official detail.

Only the observed expanded default shape is merged. Other repeated rounds
need distinct slot semantics and remain unsupported. Raw source is unchanged.
"""
from copy import deepcopy

from .errors import EzMcdError

ROW_FIELDS = {"round", "idx", "name", "minQuantity", "maxQuantity", "quantity",
              "isIncludeRound", "costInclusive", "isChoices", "choicesCode", "comboProducts",
              "classDefaultImg", "className", "classification", "plateShow"}
PART_FIELDS = {"classification", "code", "comboMaxQuantity", "customizationMode", "diffPrice",
               "diffPriceText", "forLocate", "gapPrice", "hasCustomization", "image", "isDefault",
               "name", "originalPrice", "price", "priceText", "quantity", "tags", "unsellable",
               "modification", "grills", "biasChoices"}


def detail_groups(product):
    groups = deepcopy(product.get("comboItems", []))
    if not isinstance(groups, list):
        raise EzMcdError("unsupported_combo_group_schema")
    by_round = {}
    for group in groups:
        if (not isinstance(group, dict) or type(group.get("round")) is not int
                or not 0 <= group["round"] <= 100 or "_source_entries" in group):
            raise EzMcdError("unsupported_combo_group_schema")
        by_round.setdefault(group["round"], []).append(group)
    result = []
    for repeated in by_round.values():
        group = repeated[0]
        if len(repeated) == 1:
            result.append(group)
            continue
        if len(repeated) > 20:
            raise EzMcdError("unsupported_combo_group_schema")
        signatures = []
        indices = []
        for row in repeated:
            parts = row.get("comboProducts")
            if (set(row) - ROW_FIELDS or any(type(row.get(k)) is not int or row[k] != 1 for k in
                    ("minQuantity", "maxQuantity", "quantity", "isIncludeRound"))
                    or type(row.get("costInclusive")) is not int or row["costInclusive"] != 0
                    or type(row.get("isChoices")) is not int or row["isChoices"] not in (0, 1)
                    or not isinstance(row.get("choicesCode"), str)
                    or type(row.get("idx")) is not int or row["idx"] < 0
                    or not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict)):
                raise EzMcdError("unsupported_combo_group_schema")
            part = parts[0]
            if (set(part) - PART_FIELDS or type(part.get("quantity")) is not int or part["quantity"] != 1
                    or type(part.get("diffPrice")) is not int or part["diffPrice"] != 0
                    or type(part.get("comboMaxQuantity")) is not int or part["comboMaxQuantity"] != -1
                    or part.get("unsellable") is not False
                    or not isinstance(part.get("code"), str) or not 1 <= len(part["code"]) <= 20
                    or not part["code"].isascii() or not part["code"].isdigit()
                    or part.get("forLocate") != f"{row['idx']}-{row['round']}-{part['code']}"):
                raise EzMcdError("unsupported_combo_group_schema")
            indices.append(row["idx"])
            signatures.append({k: v for k, v in row.items() if k not in ("idx", "comboProducts")} |
                {"comboProducts": [{k: v for k, v in part.items() if k != "forLocate"}]})
        if len(set(indices)) != len(indices) or any(s != signatures[0] for s in signatures[1:]):
            raise EzMcdError("unsupported_combo_group_schema")
        count = len(repeated)
        group.update(minQuantity=count, maxQuantity=count, quantity=count, _source_entries=count)
        group["comboProducts"][0]["quantity"] = count
        result.append(group)
    return result
