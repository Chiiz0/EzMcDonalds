from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from ezmcd.catalog import cart_items, menu_products
from ezmcd.demo import setup_demo
from ezmcd.errors import EzMcdError
from ezmcd.models import CheckoutInput, ItemSelection, SHANGHAI
from ezmcd.preferences import MealPreferences, PreferredItem, save_preferences, plan_meal
from ezmcd.products import verify_cart_contents
from ezmcd.service import Service
from ezmcd.storage import Store, write_private
from ezmcd.variants import resolve_variant
from test_project_variants import VariantDemo


def combo_detail():
    products = []
    for code, choice, price in (('100001', '1', 1000), ('100002', '2', 1600)):
        products.append({'code': code, 'name': '演示早餐组合', 'type': 7, 'price': price,
            'startingPrice': 1000, 'priceFormula': True, 'unsellable': False, 'matchedSpecsCode': '17-' + choice,
            'comboItems': [
                {'round': 1, 'isChoices': 0, 'choicesCode': '', 'minQuantity': 1, 'maxQuantity': 1,
                 'comboProducts': [{'code': '700001', 'name': '演示汉堡', 'quantity': 1, 'diffPrice': 0,
                    'comboMaxQuantity': -1, 'unsellable': False}]},
                {'round': 2, 'isChoices': 1, 'choicesCode': '9901', 'minQuantity': 1, 'maxQuantity': 1,
                 'comboProducts': [{'code': c, 'name': name, 'quantity': int((c == '700002') == (choice == '2')),
                    'diffPrice': extra, 'comboMaxQuantity': -1, 'unsellable': False}
                    for c, name, extra in (('700002', '演示美式', 600), ('700003', '演示豆浆', 0))]}]})
    return {'maxPurchaseQuantity': 20, 'product': {'code': 'G900', 'name': '演示早餐', 'type': 3,
        'limitedQuantity': 0, 'specTypes': [{'code': '17', 'name': '规格', 'specs': [
            {'code': '1', 'name': '配豆浆'}, {'code': '2', 'name': '配美式'}]}], 'products': products}}


class ComboVariantDemo(VariantDemo):
    def __init__(self, account, store):
        super().__init__(account, store)
        self.live = combo_detail()
        self.menu_raw['menu'][0]['productList'][0]['productName'] = '演示早餐'

    def write_cart(self, body):
        result = super().write_cart(body)
        state = self.read()
        state['draft'] = deepcopy(body.get('products', []))
        write_private(self.path, state)
        return result

    def cart(self, args):
        rows = [p | {'name': '演示早餐组合', 'realSubTotalPrice': 1600 * p['quantity']}
                for p in self.read().get('draft', [])]
        return {'simulation': True, 'items': cart_items({'products': rows}),
                'store_code': args.store_code, 'pickup_at': args.pickup_at}


class ComboVariantTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name))
        setup_demo(self.store)
        self.provider = ComboVariantDemo(self.store.account('demo'), self.store)
        self.provider.write_cart({'quantity': 0})
        self.when = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
        self.args = CheckoutInput(account='demo', store_code='9000001', pickup_at=self.when.isoformat(), max_amount_cny='20')
        self.offer = menu_products(self.provider.menu_raw)[0]
        self.selection = ItemSelection(product_code='100002', variant_of='G900', offer_id=self.offer['offer_id'], combo=[
            {'round': 1, 'items': [{'product_code': '700001'}]}, {'round': 2, 'items': [{'product_code': '700002'}]}])

    def plan(self, **changes):
        item = {'name': '演示早餐组合', 'variant_of': '演示早餐', 'variant_specifications': {'规格': '配美式'},
                'component_names': ['演示美式']}
        prefs = {'store_code': '9000001', 'pickup_time': '10:15', 'max_amount_cny': '20', 'items': [item]}
        save_preferences(self.store, MealPreferences.model_validate(prefs | changes))
        with patch.object(Service, 'provider', return_value=(self.store.account('demo'), self.provider)):
            return plan_meal(Service(self.store), 'demo', self.when.date().isoformat())

    def test_parent_exposes_combo_spec_and_separate_display_and_base_prices(self):
        parent = self.provider.product_detail(self.args, 'G900', self.offer['offer_id'])
        child = parent['variants'][1]
        self.assertEqual(child['menu_offers'], [])
        self.assertEqual(child['configuration_base_cny'], '10.00')
        self.assertEqual(child['specification_choices'][0]['choice_name'], '配美式')
        detail = self.provider.product_detail(self.args, **child['parent_detail_selection'])
        self.assertEqual(detail['display_price_cny'], '16.00')
        self.assertEqual(detail['configuration_base_cny'], '10.00')
        self.assertEqual(detail['default_components_extra_cny'], '6.00')
        self.assertEqual(len(detail['groups']), 2)

    def test_add_uses_explicit_child_and_rounds_without_price_or_parent_in_wire(self):
        draft = self.provider.configured_cart_draft(self.args, [self.selection])
        product = draft['products'][0]
        self.assertEqual(product['code'], '100002')
        self.assertEqual(product['type'], '7')
        self.assertEqual([g['comboProducts'][0]['code'] for g in product['comboItems']], ['700001', '700002'])
        for key in ('startingPrice', 'price', 'variant_of', 'specification_choices', 'configuration_base_cny'):
            self.assertNotIn(key, product)
        self.assertFalse(product['useRightCard'])

    def test_price_formula_and_default_component_accounting_must_match(self):
        for field, value in (('startingPrice', None), ('startingPrice', True), ('startingPrice', -1),
                             ('startingPrice', 1600), ('price', 1700), ('priceFormula', False), ('comboItems', []),
                             ('grills', [{}]), ('modification', {'items': [{}]})):
            raw = combo_detail()
            raw['product']['products'][1][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(EzMcdError, 'combo_schema_or_price'):
                resolve_variant(raw, self.offer, '100002')

    def test_invalid_round_shape_limits_or_duplicate_components_are_rejected(self):
        for case in ('round', 'minimum', 'maximum', 'choices', 'duplicate', 'default_quantity', 'difference', 'nested'):
            raw = combo_detail()
            groups = raw['product']['products'][1]['comboItems']
            g = groups[1]
            if case == 'round': g['round'] = groups[0]['round']
            if case == 'minimum': g['minQuantity'] = True
            if case == 'maximum': g['maxQuantity'] = 0
            if case == 'choices': g['choicesCode'] = ''
            if case == 'duplicate': g['comboProducts'].append(deepcopy(g['comboProducts'][0]))
            if case == 'default_quantity': g['comboProducts'][0]['quantity'] = 2
            if case == 'difference': g['comboProducts'][0]['diffPrice'] = -1
            if case == 'nested': g['comboProducts'][0]['comboItems'] = [{}]
            with self.subTest(case=case), self.assertRaisesRegex(EzMcdError, 'combo_schema_or_price'):
                resolve_variant(raw, self.offer, '100002')

    def test_component_promotions_are_not_borrowed_for_an_ordinary_variant(self):
        for field in ('cardId', 'couponId', 'couponCode', 'promotionId', 'productPromotions'):
            raw = combo_detail()
            raw['product']['products'][1]['comboItems'][1]['comboProducts'][0][field] = 'synthetic'
            with self.assertRaisesRegex(EzMcdError, 'combo_schema_or_price'):
                resolve_variant(raw, self.offer, '100002')

    def test_live_reload_stops_sold_out_selected_component_or_changed_variant(self):
        self.provider.product_detail(self.args, '100002', self.offer['offer_id'], variant_of='G900')
        self.provider.live['product']['products'][1]['comboItems'][1]['comboProducts'][0]['unsellable'] = True
        with self.assertRaisesRegex(EzMcdError, 'component_not_available'):
            self.provider.configured_cart_draft(self.args, [self.selection])
        self.assertEqual(self.provider.read()['cart_quantity'], 0)

    def test_each_round_and_component_limit_remains_enforced(self):
        for change in ('missing_round', 'excess_component', 'wrong_component'):
            selected = self.selection.model_dump()
            if change == 'missing_round': selected['combo'].pop()
            if change == 'excess_component': selected['combo'][1]['items'][0]['quantity'] = 2
            if change == 'wrong_component': selected['combo'][1]['items'][0]['product_code'] = '999999'
            with self.subTest(change=change), self.assertRaises(EzMcdError):
                self.provider.configured_cart_draft(self.args, [ItemSelection.model_validate(selected)])

    def test_cart_recovery_preserves_combo_selection_and_never_readds(self):
        service = Service(self.store)
        with patch.object(Service, 'provider', return_value=(self.store.account('demo'), self.provider)):
            first = service.add_configured_items(self.args, [self.selection], 'combo-variant')
            self.assertEqual(first['state'], 'MATCHED')
            with patch.object(self.provider, 'configured_cart_draft', side_effect=AssertionError('No repeat draft')):
                second = Service(Store(self.store.root)).add_configured_items(self.args, [self.selection], 'combo-variant')
            self.assertEqual(second['state'], 'MATCHED')
            self.assertFalse(second['add_attempted'])
        with self.store.connect() as db:
            self.assertEqual(dict(db.execute('SELECT stage,count(*) FROM writes GROUP BY stage').fetchall()), {'cart': 1})

    def test_server_substitution_or_component_count_change_is_detected(self):
        draft = self.provider.configured_cart_draft(self.args, [self.selection])
        self.provider.write_cart(draft)
        actual = self.provider.cart(self.args)
        verify_cart_contents(draft, actual)
        for field, value in (('code', '700003'), ('quantity', 2)):
            wrong = deepcopy(actual)
            wrong['items'][0]['components'][1]['comboProducts'][0][field] = value
            with self.assertRaisesRegex(EzMcdError, 'cart_contents_do_not_match'):
                verify_cart_contents(draft, wrong)

    def test_preferences_use_spec_labels_and_do_not_count_default_upcharge_twice(self):
        result = self.plan(max_amount_cny='16')
        self.assertTrue(result['selection_ready'])
        self.assertEqual(result['estimated_amount_cny'], '16.00')
        self.assertEqual(result['items'][0]['product_code'], '100002')
        self.assertEqual(result['items'][0]['combo'][1]['items'][0]['product_code'], '700002')
        self.assertFalse(result['cart_changed'])

    def test_budget_still_blocks_selected_combo_above_limit(self):
        result = self.plan(max_amount_cny='15')
        self.assertFalse(result['selection_ready'])
        self.assertIn('estimated_price_exceeds_preference_limit', [p['reason'] for p in result['issues']])

    def test_missing_ambiguous_or_wrong_specification_does_not_guess(self):
        for specifications in (None, {'规格': '不存在'}, {'未知分组': '配美式'}):
            item = {'name': '演示早餐组合', 'variant_of': '演示早餐', 'component_names': ['演示美式']}
            if specifications is not None: item['variant_specifications'] = specifications
            self.assertFalse(self.plan(items=[item])['selection_ready'])

    def test_saved_specification_requires_parent_and_valid_nonempty_names(self):
        for value in ({'规格': '配美式'}, {}, {'': '配美式'}, {'规格': ' '}, {'规格': 2}):
            item = {'name': '组合', 'variant_specifications': value}
            if value != {'规格': '配美式'}: item['variant_of'] = '父商品'
            with self.assertRaises(ValidationError): PreferredItem.model_validate(item)

    def test_old_preferences_without_specification_keep_existing_serialization(self):
        value = PreferredItem(name='演示汉堡', variant_of='演示规格').model_dump()
        self.assertNotIn('variant_specifications', value)


if __name__ == '__main__':
    unittest.main()
