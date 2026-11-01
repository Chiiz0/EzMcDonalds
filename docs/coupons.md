# 按门店和取餐时间选券

`membership_assets` 展示卡券概况。要判断某张券在这次点餐能否使用，先选门店和具体取餐时间，再调用 `coupons_list(request, keyword)`。

返回券名、分类、有效期说明、优惠展示、剩余次数和本次查询的可用状态。即使接口返回了一张券，`available_for_request=false` 时也不能继续选择。返回的 `coupon_ref` 仅适用于当前账号、门店、取餐时间和方式；不包含原始券号、卡号或登录凭据。

## 选择对应商品

1. 从 `coupons_list` 选择明确可用的券。
2. 调用 `coupon_products(request, coupon_ref)`，查看券对应的商品和展示价。
3. 对支持配置的商品调用 `product_detail`，同时传入 `product_code`、`offer_id` 和 `coupon_ref`。详情再次检查数量和可选配料。
4. 用 `cart_add_configured` 加入空购物车，`items` 中仍保留同一 `coupon_ref`，并明确套餐、配料选择。
5. `checkout_quote` 核对最终商品、券名和实付金额。只有本地消费策略允许时才调用 `checkout_submit`。

加购选择的结构示例（所有代码及引用均需来自本次实时返回）：

```json
{
  "product_code": "900011",
  "offer_id": "copy-from-coupon-products",
  "coupon_ref": "coupon-000000000000000000000000",
  "quantity": 1,
  "modifiers": [{"code": "222", "quantity": 1, "key": "1-1"}]
}
```

配料例子表示选择实时详情里的某个单选项，不能跨商品复制 key。原始券 ID、券码及促销 ID 由项目重新查询并构造，Agent 不提供这些字段。相同券不能在一份草案中重复用于不同条目；数量还受剩余次数和实时详情限制。

每次详情和加购都会重新查券。券被使用、失效、换了门店/时段或不再对应商品时停止，不回退到普通价。加购后读取购物车，核对商品、数量和券身份；缺失或变更返回 `cart_coupon_not_confirmed_review_cart`，不得继续付款或换 key 重试。报价后券身份变动也会改变内容指纹。

## 权益卡和特殊券

- `configuration_path=menu_card_offer`：早餐卡等权益继续从实时菜单选择相应卡报价，不把卡券当作普通单品券提交。
- `configuration_path=official_app`：预付券、条件券、多商品绑定券或未识别活动需要官方 App 配置。
- 当前配置路径覆盖返回普通单品/套餐、明确一份商品、正金额且剩余次数可确认的普通券。收费定制、零元券及复杂组合仍未完整适配。
- 接口的预约标记含义尚未完整核对，因此不会只根据一个数值宣布可预约或不可预约；本次门店时段查询和最终报价都必须通过。

商品详情可能有收费或售罄配料，不能因为券本身可用就忽略这些限制。`cart_configuration_supported` 表示券商品结构可进入配置流程，仍须检查 `product_detail.standard_configuration_supported`。

已真实完成原门店早餐时段可用券、对应商品及详情查询：7张券中5张报告可用，普通冰美式券展示价¥9.90。**新增券加购、冰量修改及券结算尚待真实验收。** 此功能没有自动领券、绑定券、转赠或充值行为。
