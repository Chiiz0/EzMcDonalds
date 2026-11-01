from copy import deepcopy
import unittest
from unittest.mock import Mock

from ezmcd.catalog import cart_items, cart_regular_subtotal, checkout
from ezmcd.errors import EzMcdError
from ezmcd.models import ModifierSelection
from ezmcd.modifiers import cart_wire_body, configuration_signature, public_modifiers, selected_modification, verify_modification
from ezmcd.preferences import MealPreferences, configuration_extra, plan_meal, save_preferences
from ezmcd.products import cart_configuration, verify_cart_contents
from ezmcd.provider import McdProvider
import test_project_fulfillment as fulfillment_tests
import test_project_preferences as preference_tests


def milk_option():
    return {'code': '1001', 'type': 1, 'name': '示例咖啡', 'modification': {'items': [
        {'title': '换奶', 'minValues': 0, 'maxValues': 1, 'values': [
            {'code': '8001', 'name': '换燕麦奶', 'displayMode': 1, 'minQuantity': 0, 'maxQuantity': 1,
             'selectedQuantity': 0, 'selectedKey': '0-1', 'unselectedKey': '0-0', 'price': 200, 'soldOut': False}]}]}}


def selected():
    return selected_modification(milk_option(), [ModifierSelection(code='8001', quantity=1)])


def echo(price=200):
    return {'customization': {'options': [], 'items': [
        {'code': '8001', 'name': '换燕麦奶', 'key': '0-1', 'displayText': '换燕麦奶(换+¥2)',
         'values': [{'code': '2', 'name': '换', 'price': price, 'quantity': 1}]}]}}


class OptionalModifierTests(unittest.TestCase):
    def test_default_is_unselected_and_has_no_local_surcharge(self):
        self.assertEqual(cart_configuration(milk_option()),
            {'modification': {'values': [{'code': '8001', 'quantity': 1, 'key': '0-0'}]}})
        view = public_modifiers(milk_option())
        self.assertTrue(view['supported'])
        self.assertEqual(view['groups'][0]['kind'], 'optional_choice')
        self.assertEqual(view['groups'][0]['choices'][0]['extra_cny'], '2.00')

    def test_paid_choice_binds_live_fee_but_never_sends_local_metadata(self):
        config = selected()
        self.assertEqual(config['_ezmcd_modifier_prices'], {'8001': 200})
        draft = {'products': [{'code': '1001', **config, 'comboItems': [
            {'comboProducts': [{'code': '1002', **deepcopy(config)}]}]}]}
        transport = Mock()
        McdProvider({}, transport).write_cart(draft)
        sent = transport.send.call_args.kwargs['body']
        self.assertNotIn('_ezmcd_modifier_prices', sent['products'][0])
        self.assertNotIn('_ezmcd_modifier_prices', sent['products'][0]['comboItems'][0]['comboProducts'][0])
        self.assertIn('_ezmcd_modifier_prices', draft['products'][0])
        self.assertEqual(sent['products'][0]['modification']['values'][0]['quantity'], 1)

    def test_sold_out_can_be_left_unselected_but_not_added(self):
        product = milk_option(); product['modification']['items'][0]['values'][0]['soldOut'] = True
        self.assertTrue(public_modifiers(product)['supported'])
        self.assertEqual(cart_configuration(product)['modification']['values'][0]['key'], '0-0')
        with self.assertRaisesRegex(EzMcdError, 'unavailable'):
            selected_modification(product, [ModifierSelection(code='8001', quantity=1)])

    def test_optional_choice_does_not_accept_ambiguous_keys_or_partial_selection(self):
        for choices in ([], [ModifierSelection(code='8001', quantity=1, key='0-1')],
                        [ModifierSelection(code='8002', quantity=1)]):
            with self.assertRaises(EzMcdError): selected_modification(milk_option(), choices)

    def test_unobserved_optional_shapes_and_nonfree_defaults_stay_unsupported(self):
        for field, value in (('selectedQuantity', 1), ('price', -1), ('price', True), ('price', 100001),
                             ('unselectedKey', None), ('selectedKey', '1-1'), ('maxQuantity', 2)):
            product = milk_option(); product['modification']['items'][0]['values'][0][field] = value
            with self.subTest(field=field, value=value): self.assertFalse(public_modifiers(product)['supported'])
        product = milk_option(); product['modification']['items'][0]['values'] *= 2
        self.assertFalse(public_modifiers(product)['supported'])

    def test_exact_paid_echo_matches_and_changed_or_missing_fee_does_not(self):
        verify_modification(selected(), echo())
        for price in (0, 100, 300, True, -1, '200'):
            with self.subTest(price=price), self.assertRaises(EzMcdError): verify_modification(selected(), echo(price))
        with self.assertRaises(EzMcdError): verify_modification(cart_wire_body({'products': [selected()]})['products'][0], echo())
        with self.assertRaises(EzMcdError): verify_modification(selected(), {'modification': selected()['modification']})

    def test_paid_echo_requires_one_selected_value_and_selected_key(self):
        for case in ('key', 'quantity', 'duplicate', 'modern_conflict', 'missing'):
            value = echo(); item = value['customization']['items'][0]
            if case == 'key': item['key'] = '0-0'
            if case == 'quantity': item['values'][0]['quantity'] = 0
            if case == 'duplicate': item['values'] *= 2
            if case == 'modern_conflict': value.update(cart_configuration(milk_option()))
            if case == 'missing': value['customization']['items'] = []
            with self.subTest(case=case), self.assertRaises(EzMcdError): verify_modification(selected(), value)

    def test_cart_comparison_preserves_fee_binding_after_serialization(self):
        import json
        draft = {'products': [{'code': '1001', 'type': '1', 'quantity': 2, **selected()}]}
        cart = {'items': cart_items({'products': [{'code': '1001', 'type': 1, 'quantity': 2,
            'name': '示例咖啡', 'realSubTotalPrice': 2100, **echo()}]})}
        verify_cart_contents(json.loads(json.dumps(draft)), cart)
        for price in (0, 100, 300):
            wrong = deepcopy(cart); wrong['items'][0]['configuration'] = echo(price)
            with self.assertRaisesRegex(EzMcdError, 'cart_contents_do_not_match'): verify_cart_contents(draft, wrong)

    def test_local_evidence_is_never_accepted_from_received_cart(self):
        value = echo(100) | {'_ezmcd_modifier_prices': {'8001': 200}}
        with self.assertRaises(EzMcdError): verify_modification(selected(), value)
        self.assertEqual(configuration_signature(value)['paid'], {'8001': 100})

    def test_unselected_echo_lists_possible_fee_but_charges_zero(self):
        value = echo(); item = value['customization']['items'][0]
        item.update(key='0-0'); item.pop('displayText')
        item['values'][0].update(code='1', name='不换', quantity=0)
        verify_modification(cart_configuration(milk_option()), value)
        self.assertEqual(configuration_signature(value)['paid'], {})
        with self.assertRaises(EzMcdError): verify_modification(selected(), value)


class OptionalModifierPriceTests(unittest.TestCase):
    def setUp(self):
        fulfillment_tests.OrdinaryCartPriceTests.setUp(self)
        self.args = self.args.model_copy(update={'max_amount_cny': '30'})
        self.product.update(realPrice=1050, realSubTotalPrice=2100, **echo())
        self.cart.update(realTotalPrice=2100, totalPrice=2100)
        self.row.update(price='10.50', subtotal='21.00', realSubtotal='21.00',
                        comboItemList=[{'name': '换燕麦奶(换+¥2)', 'quantity': 1}])
        self.confirmation['productPriceInfo'].update(realTotalAmount='21.00', totalAmount='21.00')

    def test_unit_base_plus_fee_is_multiplied_by_product_quantity(self):
        result = checkout(self.args, self.cart, self.confirmation, '2')
        self.assertEqual(result['amount_cents'], 2100)
        self.assertEqual(result['body']['cartItems'][0]['productPrice'], '10.50')
        self.assertEqual(result['body']['cartItems'][0]['quantity'], 2)

    def test_budget_includes_fee_and_cannot_use_the_base_price_only(self):
        with self.assertRaisesRegex(EzMcdError, 'exceeds_limit'):
            checkout(self.args.model_copy(update={'max_amount_cny': '20'}), self.cart, self.confirmation, '2')

    def test_inconsistent_price_fee_or_unknown_customization_stops_quote(self):
        for change in ({'originPrice': 800}, {'realPrice': 850}, {'quantity': True}, {'realSubTotalPrice': 1050},
                       {'customization': echo(100)['customization']}, {'coupon': {'id': 'synthetic'}},
                       {'modification': {'values': []}}, {'subTotalPrice': None}):
            with self.subTest(change=change), self.assertRaises(EzMcdError): cart_regular_subtotal(self.product | change)

    def test_confirmation_labels_amount_and_quantity_must_match(self):
        for change in ({'price': '8.50'}, {'subtotal': '17.00'}, {'realSubtotal': '17.00'},
                       {'comboItemList': []}, {'comboItemList': [{'name': '不换', 'quantity': 1}]},
                       {'comboItemList': [{'name': '换燕麦奶(换+¥2)', 'quantity': 2}]}):
            confirmation = deepcopy(self.confirmation)
            confirmation['productPriceInfo']['cartProductList'][0].update(change)
            with self.subTest(change=change), self.assertRaises(EzMcdError): checkout(self.args, self.cart, confirmation, '2')

    def test_unselected_option_has_no_confirmation_label_or_extra_cost(self):
        item = self.product['customization']['items'][0]
        item['key'] = '0-0'; item.pop('displayText'); item['values'][0]['quantity'] = 0
        self.product.update(realPrice=850, realSubTotalPrice=1700)
        self.cart.update(realTotalPrice=1700, totalPrice=1700)
        self.row.update(price='8.50', subtotal='17.00', realSubtotal='17.00', comboItemList=[])
        self.confirmation['productPriceInfo'].update(realTotalAmount='17.00', totalAmount='17.00')
        self.assertEqual(checkout(self.args, self.cart, self.confirmation, '2')['amount_cents'], 1700)
        self.row['comboItemList'] = [{'name': '不换', 'quantity': 1}]
        with self.assertRaises(EzMcdError): checkout(self.args, self.cart, self.confirmation, '2')

    def test_extra_global_regular_amount_cannot_masquerade_as_a_discount(self):
        self.cart.update(totalPrice=2300, discountAmount=200)
        self.confirmation['productPriceInfo'].update(totalAmount='23.00', discountAmount='2.00')
        with self.assertRaisesRegex(EzMcdError, 'unmodeled_order_level_discount_or_fee'):
            checkout(self.args, self.cart, self.confirmation, '2')


class OptionalModifierPreferenceTests(unittest.TestCase):
    def setUp(self):
        preference_tests.PreferenceTests.setUp(self)
        self.offer.update(product_type=1, display_price_cny='10.00')
        self.detail.update(groups=[], modifiers=public_modifiers(milk_option()), standard_configuration_supported=True)
        self.preferences = MealPreferences(store_code='9000001', pickup_time='10:15', max_amount_cny='30',
            items=[{'name': '示例组合', 'quantity': 2, 'modifiers': [{'name': '换燕麦奶', 'quantity': 1}]}])
        save_preferences(self.store, self.preferences)

    def test_named_option_adds_fee_for_each_item_and_preserves_explicit_choice(self):
        result = plan_meal(self.service, 'test')
        self.assertTrue(result['selection_ready'])
        self.assertEqual(result['estimated_amount_cny'], '24.00')
        self.assertEqual(result['items'][0]['modifiers'], [{'code': '8001', 'quantity': 1}])

    def test_estimate_blocks_when_base_fits_but_selected_extra_does_not(self):
        self.preferences.max_amount_cny = '22'; save_preferences(self.store, self.preferences)
        result = plan_meal(self.service, 'test')
        self.assertFalse(result['selection_ready'])
        self.assertIn('estimated_price_exceeds_preference_limit', [i['reason'] for i in result['issues']])

    def test_omit_or_explicitly_remove_optional_choice_never_adds_its_price(self):
        for modifiers in ([], [{'name': '换燕麦奶', 'quantity': 0}]):
            self.preferences.items[0].modifiers = []
            value = self.preferences.model_dump(); value['items'][0]['modifiers'] = modifiers
            save_preferences(self.store, MealPreferences.model_validate(value))
            result = plan_meal(self.service, 'test')
            self.assertTrue(result['selection_ready'])
            self.assertEqual(result['estimated_amount_cny'], '20.00')

    def test_sold_out_explicit_choice_blocks_instead_of_dropping_replacement(self):
        self.detail['modifiers']['groups'][0]['choices'][0]['available'] = False
        self.assertFalse(plan_meal(self.service, 'test')['selection_ready'])

    def test_component_fee_uses_component_quantity_and_does_not_replace_base_difference(self):
        detail = {'groups': [{'round': 2, 'choices': [{'product_code': '1001', 'modifiers': public_modifiers(milk_option())}]}]}
        groups = [{'round': 2, 'items': [{'product_code': '1001', 'quantity': 2,
                                        'modifiers': [{'code': '8001', 'quantity': 1}]}]}]
        self.assertEqual(configuration_extra(detail, None, groups), 400)


if __name__ == '__main__':
    unittest.main()
