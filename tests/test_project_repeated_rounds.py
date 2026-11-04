from copy import deepcopy
import json
import unittest

from ezmcd.combo_rounds import detail_groups
from ezmcd.errors import EzMcdError
from ezmcd.models import ItemSelection
from ezmcd.preferences import MealPreferences, save_preferences, plan_meal
from ezmcd.products import configured_product, public_detail, verify_cart_contents
import test_project_preferences as preferences


def fixture():
    groups=[]
    for number,code,name in ((1,'100001','鸡块4块'),(2,'200001','甜酸酱')):
        for _ in range(2):
            idx=len(groups)
            groups.append({'round':number,'idx':idx,'name':name,'minQuantity':1,'maxQuantity':1,
                'quantity':1,'isChoices':0,'choicesCode':'','isIncludeRound':1,'costInclusive':0,
                'comboProducts':[{'code':code,'name':name,'quantity':1,'comboMaxQuantity':-1,
                    'diffPrice':0,'unsellable':False,'forLocate':f'{idx}-{number}-{code}'}]})
    offer={'productCode':'900001','productType':7,'offer_id':'synthetic-offer','priceInfo':{'eatInPrice':1990}}
    detail={'maxPurchaseQuantity':10,'product':{'code':'900001','name':'鸡块8块套餐','type':7,'comboItems':groups}}
    selection=ItemSelection(product_code='900001',offer_id='synthetic-offer',combo=[
        {'round':1,'items':[{'product_code':'100001','quantity':2}]},
        {'round':2,'items':[{'product_code':'200001','quantity':2}]}])
    return offer,detail,selection


class RepeatedRoundTests(unittest.TestCase):
    def test_identical_expanded_rows_merge_required_quantities_without_changing_source(self):
        offer,detail,selection=fixture();before=deepcopy(detail)
        public=public_detail(detail,offer)
        self.assertTrue(public['standard_configuration_supported'])
        self.assertEqual([(g['round'],g['min_quantity'],g['max_quantity'],g['source_entries']) for g in public['groups']],[(1,2,2,2),(2,2,2,2)])
        self.assertEqual([g['choices'][0]['current_quantity'] for g in public['groups']],[2,2])
        cart=configured_product(selection,offer,detail,1)
        self.assertEqual([g['comboProducts'][0]['quantity'] for g in cart['comboItems']],[2,2])
        self.assertEqual(detail,before)
        self.assertNotIn('_source_entries',json.dumps(cart));self.assertNotIn('forLocate',json.dumps(cart))

    def test_reordered_source_rows_keep_exact_counts_by_round(self):
        offer,detail,selection=fixture()
        detail['product']['comboItems'].reverse()
        groups=detail_groups(detail['product'])
        self.assertEqual({g['round']:g['minQuantity'] for g in groups},{1:2,2:2})
        cart=configured_product(selection,offer,detail,1)
        self.assertEqual({g['round']:g['comboProducts'][0]['quantity'] for g in cart['comboItems']},{1:2,2:2})

    def test_one_item_choices_flag_is_preserved_when_all_rows_match(self):
        offer,detail,selection=fixture()
        for g in detail['product']['comboItems']:
            if g['round']==1:g.update(isChoices=1,choicesCode='900002')
        cart=configured_product(selection,offer,detail,1)
        self.assertEqual((cart['comboItems'][0]['isChoices'],cart['comboItems'][0]['choicesCode']),(1,'900002'))

    def test_missing_excess_or_wrong_component_does_not_satisfy_merged_round(self):
        offer,detail,selection=fixture()
        for quantity in (1,3):
            value=selection.model_dump();value['combo'][0]['items'][0]['quantity']=quantity
            with self.assertRaisesRegex(EzMcdError,'combo_round_quantity'):
                configured_product(ItemSelection.model_validate(value),offer,detail,1)
        value=selection.model_dump();value['combo'][0]['items'][0]['product_code']='200001'
        with self.assertRaises(EzMcdError):configured_product(ItemSelection.model_validate(value),offer,detail,1)

    def test_different_slots_limits_fees_choices_or_modifiers_are_not_collapsed(self):
        for field,value in (('minQuantity',0),('quantity',2),('isIncludeRound',0),('costInclusive',1),
                ('isChoices',1),('choicesCode','different'),('unknownRule',True)):
            offer,detail,selection=fixture();detail['product']['comboItems'][1][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(EzMcdError,'unsupported_combo_group_schema'):
                configured_product(selection,offer,detail,1)
            self.assertFalse(public_detail(detail,offer)['standard_configuration_supported'])
        for field,value in (('code','100002'),('diffPrice',100),('comboMaxQuantity',1),('unsellable',True),
                ('quantity',2),('modification',{'items':[{'extra':'different'}]})):
            offer,detail,selection=fixture();detail['product']['comboItems'][1]['comboProducts'][0][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(EzMcdError,'unsupported_combo_group_schema'):
                configured_product(selection,offer,detail,1)

    def test_unique_position_binding_and_strict_numeric_types_are_required(self):
        for change in ('index','locate','bool','extra_choice','missing_position','marker'):
            offer,detail,selection=fixture();row=detail['product']['comboItems'][1]
            if change=='index':row['idx']=0;row['comboProducts'][0]['forLocate']='0-1-100001'
            elif change=='locate':row['comboProducts'][0]['forLocate']='unknown'
            elif change=='bool':row['minQuantity']=True
            elif change=='extra_choice':row['comboProducts'].append(deepcopy(row['comboProducts'][0]))
            elif change=='marker':row['_source_entries']=2
            else:row.pop('idx')
            with self.subTest(change=change),self.assertRaisesRegex(EzMcdError,'unsupported_combo_group_schema'):
                configured_product(selection,offer,detail,1)

    def test_more_than_twenty_rows_cannot_generate_unrepresentable_selection(self):
        _,detail,_=fixture();base=detail['product']['comboItems'][0];rows=[]
        for i in range(21):
            row=deepcopy(base);row['idx']=i;row['comboProducts'][0]['forLocate']=f'{i}-1-100001';rows.append(row)
        with self.assertRaises(EzMcdError):detail_groups({'comboItems':rows})

    def test_unknown_rule_on_all_rows_is_not_assumed_safe_to_merge(self):
        for level in ('row','component'):
            offer,detail,selection=fixture()
            for row in detail['product']['comboItems']:
                target=row if level=='row' else row['comboProducts'][0]
                target['newLimitRule']=1
            with self.subTest(level=level),self.assertRaisesRegex(EzMcdError,'unsupported_combo_group_schema'):
                configured_product(selection,offer,detail,1)

    def test_malformed_groups_are_reported_without_rendering_unvalidated_choices(self):
        for value in (None, {}, ['invalid'], [{'round':False}]):
            offer,detail,_=fixture();detail['product']['comboItems']=value
            public=public_detail(detail,offer)
            self.assertFalse(public['standard_configuration_supported'])
            self.assertEqual(public['configuration_issues'],['unsupported_combo_group_schema'])
            self.assertEqual(public['groups'],[])

    def test_non_repeated_round_is_unchanged_and_no_combo_stays_empty(self):
        _,detail,_=fixture();row=detail['product']['comboItems'][0]
        self.assertEqual(detail_groups({'comboItems':[row]}),[row])
        self.assertEqual(detail_groups({}),[])

    def test_cart_readback_must_preserve_every_merged_component(self):
        offer,detail,selection=fixture();product=configured_product(selection,offer,detail,1)
        cart={'items':[{'product_code':'900001','product_type':'7','quantity':1,'coupon_fingerprint':None,
            'components':deepcopy(product['comboItems']),'configuration':{}}]}
        verify_cart_contents({'products':[product]},cart)
        cart['items'][0]['components'][1]['comboProducts'][0]['quantity']=1
        with self.assertRaisesRegex(EzMcdError,'cart_contents_do_not_match'):
            verify_cart_contents({'products':[product]},cart)


class RepeatedRoundPlanTests(unittest.TestCase):
    setUp=preferences.PreferenceTests.setUp

    def prepare(self):
        offer,detail,_=fixture();public=public_detail(detail,offer)
        self.detail.clear();self.detail.update(public)
        self.offer.update(name=public['name'],product_code=public['product_code'],display_price_cny='19.90')
        value=self.preferences.model_dump();value['max_amount_cny']='19.90'
        value['items'][0].update(name=public['name'],component_names=[],component_choices=[
            {'round':1,'name':'鸡块4块','quantity':2},{'round':2,'name':'甜酸酱','quantity':2}])
        prefs=MealPreferences.model_validate(value);save_preferences(self.store,prefs)
        return offer,detail,prefs

    def test_explicit_mandatory_copies_plan_at_combo_price_and_honor_budget(self):
        offer,detail,prefs=self.prepare();plan=plan_meal(self.service,'test')
        self.assertTrue(plan['selection_ready']);self.assertEqual(plan['estimated_amount_cny'],'19.90')
        cart=configured_product(ItemSelection.model_validate(plan['items'][0]),offer,detail,1)
        self.assertEqual([g['comboProducts'][0]['quantity'] for g in cart['comboItems']],[2,2])
        prefs.max_amount_cny='19.89';save_preferences(self.store,prefs)
        self.assertFalse(plan_meal(self.service,'test')['selection_ready'])

    def test_implicit_single_copy_does_not_drop_the_second_mandatory_copy(self):
        _,_,prefs=self.prepare();value=prefs.model_dump()
        value['items'][0].pop('component_choices');value['items'][0]['component_names']=['鸡块4块','甜酸酱']
        save_preferences(self.store,MealPreferences.model_validate(value))
        result=plan_meal(self.service,'test');self.assertFalse(result['selection_ready']);self.assertEqual(result['items'],[])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM writes').fetchone()[0],0)

if __name__=='__main__':unittest.main()
