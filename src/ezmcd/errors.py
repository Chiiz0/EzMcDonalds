class EzMcdError(Exception):
    """A bounded, non-secret error code suitable for CLI/MCP responses."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def error_result(code):
    """Actionable explanations from a fixed catalogue, never upstream secrets."""
    help_text = {
        "insufficient_wallet_balance": ("麦钱包余额不足。", "报价仍可查看；由用户在官方入口处理余额后重新报价，本工具不会自动充值。"),
        "session_expired_reconnect_locally": ("账号登录已失效。", "有刷新令牌时调用 account_refresh；否则在本机重新短信登录或更新会话。"),
        "runtime_expired_reconnect_official_session": ("缺少可用的下单运行字段。", "仍可浏览和准备购物车；按凭据指引更新本人官方运行样本后再下单。"),
        "local_checkout_policy_required": ("此账号尚未授权自动消费。", "用户在本机通过 account policy 设置门店与限额后才能购买；保存偏好不会授权消费。"),
        "account_not_connected": ("找不到这个已连接账号。", "先调用 accounts_list；尚未接入时查看 account_connection_instructions。"),
        "cart_not_empty_use_existing_cart_or_clear_in_official_app": ("购物车已有商品。", "先调用 cart_get 核对，使用现有购物车报价；不要自动覆盖已有选择。"),
        "city_not_unique_use_cities_list_and_select_code": ("城市名称不明确或未找到。", "调用 cities_list 搜索并选择准确的城市代码。"),
        "session_refresh_received_resume_verification": ("新会话已保存，核验尚未完成。", "再次调用 account_refresh，只核验已保存的新会话。"),
        "session_refresh_already_attempted_relogin_locally": ("先前刷新已尝试，不能安全重发。", "在本机重新短信登录；不要清除记录后重复刷新。"),
        "transport_unknown_do_not_retry_writes": ("网络结果未知。", "查询原订单或购物车状态；不要重发购买请求或更换幂等键。"),
        "select_account_for_meal_plan": ("早餐偏好尚未绑定账号。", "从 accounts_list 选择账号并传给 meal_plan，不会自动选择其他会员。"),
        "meal_preferences_missing_save_preferences_first": ("尚未保存点餐偏好。", "先选择门店、餐品、套餐组件、取餐时间和金额上限，再调用 preferences_save。"),
        "official_client_build_not_supported": ("当前官方客户端构建尚未适配。", "运行 doctor 检查环境，保留现有账号数据；需要适配对应构建。"),
        "runtime_requires_same_verified_official_device": ("两个账号的官方设备或客户端上下文不一致。", "仅可复用同一官方设备的近期样本；其他设备请导入自己的同会话样本，不能修改设备字段强行匹配。"),
        "runtime_sample_expired": ("官方运行样本已超过项目允许的时限，或时间不正确。", "导入新的本人官方样本；不要修改采样时间。"),
        "runtime_readonly_verification_failed": ("该运行上下文没有通过当前账号的只读验证。", "原账号配置未修改。先用 account check 核对登录，再检查自己的官方运行样本，不自动重发或更换设备字段。"),
        "runtime_headers_require_private_terminal": ("运行请求头只能在本机私有终端输入。", "直接在终端运行 account runtime ALIAS --paste-headers；不要把凭据放进参数、管道或聊天。"),
        "runtime_header_value_invalid": ("粘贴的请求头值为空或格式不正确。", "逐项粘贴官方请求的 sid、token、x-mcd-sign 的值，不含字段名或换行。"),
        "cart_modifier_selection_not_confirmed_review_cart": ("服务器购物车没有确认所选配料。", "先调用 cart_get 核对；不要自动重新加购或继续付款，必要时在官方 App 修正。"),
        "select_every_modifier_exactly_once": ("配料选择不完整或重复。", "重新读取 product_detail，对所有活动组的代码各选择一次；保留/去除用0或1，单选组用数量1及所选项的实时 key。"),
        "select_one_available_free_modifier_choice": ("单选配料不明确、售罄或需要加价。", "从 product_detail 的对应组选择一个免费且可用的选项，使用其 key 和数量1；收费定制交回官方 App。"),
        "coupon_not_found_for_account_store_time": ("这张券不属于当前账号、门店或取餐时间的结果。", "重新调用 coupons_list 选择，不沿用其他请求的 coupon_ref，也不自动改成原价。"),
        "coupon_unavailable_for_selected_pickup": ("当前门店或取餐时间无法使用这张券，或次数已用完。", "重新查询券的适用状态和规则，由用户选择其他券或调整安排；不自动换券或原价购买。"),
        "coupon_configuration_or_quantity_requires_review": ("该券的商品配置或数量不在支持范围内。", "重新查询 coupon_products 与 product_detail；需要特殊兑换流程的券交回官方 App。"),
        "cart_coupon_not_confirmed_review_cart": ("服务器购物车没有确认所选优惠券。", "先调用 cart_get 核对；不要重新加购或继续付款，必要时在官方 App 修正。"),
    }
    message, action = help_text.get(code, ("本次操作未完成。", "查看错误码与账号诊断；涉及订单的未知结果只查询，不重复提交。"))
    return {"ok": False, "error": code, "message": message, "next_action": action, "automatic_write_retry": False}
