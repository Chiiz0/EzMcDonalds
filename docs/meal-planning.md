# 从偏好到选餐方案

用户可通过个人 Agent 调用 `preferences_save`，保存店铺、餐品名称、饮品等套餐组件、数量、星期、取餐时间和金额上限。偏好只存在本机，不改变消费策略，也不创建定时任务。可用 `preferences_get` 查看。

## 一次选择，之后复用

1. `stores_search` 使用城市和关键词查店，或使用用户明确提供的经纬度查附近店；返回分页结果、地址、营业时间。不会擅自获取位置。
2. `pickup_slots` 返回北京时间的具体可预约时间，已过去的时间会过滤；门店营业不代表任意时刻都能自取。
3. `menu_list` 返回菜单分类、图片、展示价格和卡优惠；`product_detail` 返回套餐各轮可选组件。
4. `preferences_save` 保存用户选定的名称和条件；账号可以暂不绑定，之后 `meal_plan` 显式传入账号。
5. `meal_plan` 从当前可预约时段中选择符合偏好的下一次时间，并重新读取菜单、优惠和套餐详情，输出 `CheckoutInput` 与 `ItemSelection` 草案。

`meal_plan` 不加购、不下单、不付款。指定 `date` 时只检查这一天，不会悄悄改到其他日期；不指定时在官方当前开放的预约范围内查找。星期以北京时间计算，0 表示周一、6 表示周日。

## 匹配规则

- 按餐品完整名称匹配；名称相同但商品不同，需要明确选择。
- 菜单中的商品组入口不作为可直接购买的套餐。
- 同一商品有多个报价入口且展示价格均已知时，选择最低展示价；最终金额仍以结算为准。
- `require_card_discount=true` 时只选择有卡优惠的入口，缺卡优惠时停止，不自动改成原价。
- 用户指定的饮品必须在该轮可用且支持当前配置；不会自动替换成其他饮品。
- 必选轮次只有唯一选项时可以补齐；存在多个选项却没有偏好时返回待选择项。
- 可选轮次允许明确选择空列表；后续加购仍按实时最少数量校验。
- 套餐组件加价和份数纳入估算；已知估算超限即停止。展示价或估算不等于最终报价。
- 不支持的定制、缺货、名称歧义或时段不可用会返回结构化 `issues`，不会返回可直接执行的半份购物车草案。

## 记住配料偏好

先读取 `product_detail`，使用其中支持的免费 0/1 配料完整名称。单品的配料偏好放在 `modifiers`；套餐放在对应的 `component_modifiers`，例如：

```json
{
  "name": "从菜单复制的套餐名称",
  "component_names": ["从详情复制的咖啡名称"],
  "component_modifiers": [{
    "component_name": "从详情复制的咖啡名称",
    "modifiers": [{"name": "从详情复制的糖包名称", "quantity": 0}]
  }]
}
```

这是 `preferences_save.preferences.items` 中的一项。`quantity=0` 去除、`1` 保留；未指定的其他配料沿用本次官方详情的默认值。方案会重新按名称找到当前代码并补齐全部配料选择，不保存可失效的配料代码。

要求去除的配料找不到、同名选项不唯一、选择违反实时数量限制或该组件没有被选中时，方案会返回问题并停止。不会把“不加糖”忽略掉，也不会因为设置配料就额外加入另一杯饮品。收费及复杂定制仍交回官方 App。新增配料写入路径尚待真实加购验收。

## Agent 执行顺序

`meal_plan → cart_get → cart_add_configured（购物车为空时）→ checkout_quote → checkout_submit → order_status`。

使用方案提供的稳定 `idempotency_key`，同一次购买不要反复生成新 key。已有购物车先核对，不覆盖。`selection_ready=true` 只说明选餐草案完整，不代表已经授权消费或具备下单运行字段。

## 本地 CLI

```sh
ezmcd preferences show
ezmcd preferences save --file /absolute/path/preferences.json
ezmcd preferences plan --account personal
ezmcd preferences plan --account personal --date 2026-11-02
```

JSON 文件权限需为 0600。日期仅为格式示例；可预约时间取自实时接口。自动调度由用户另行设置，本项目不会因为偏好选择“每天”就开始购买。
