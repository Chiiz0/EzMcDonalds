from copy import deepcopy
import json
import unittest

from pydantic import ValidationError

from ezmcd.models import ItemSelection
from ezmcd.preferences import MealPreferences, save_preferences, plan_meal
from ezmcd.products import configured_product, public_detail
import test_project_preferences as fixtures
import test_project_products as products


class ComponentPreferenceTests(unittest.TestCase):
    setUp = fixtures.PreferenceTests.setUp

    def save(self, choices, **changes):
        value=self.preferences.model_dump()
        value['items'][0].update(component_names=[],component_choices=choices,**changes)
        self.preferences=MealPreferences.model_validate(value)
        save_preferences(self.store,self.preferences)

    def plan(self):
        return plan_meal(self.service,'test')

    def double_round(self):
        group=self.detail['groups'][0]
        group.update(min_quantity=2,max_quantity=2)
        group['choices'][0]['max_quantity']=2
        return group

    def test_same_component_quantity_resolves_fresh_code_and_does_not_write(self):
        group=self.double_round()
        self.save([{'round':group['round'],'name':'咖啡','quantity':2}])
        first=self.plan()
        self.assertTrue(first['selection_ready'])
        self.assertEqual(first['items'][0]['combo'][0]['items'],[{'product_code':'200001','quantity':2}])
        group['choices'][0]['product_code']='200009'
        second=self.plan()
        self.assertEqual(second['items'][0]['combo'][0]['items'][0]['product_code'],'200009')
        self.assertEqual(first['idempotency_key'],second['idempotency_key'])
        self.assertEqual(second['products'][0]['component_choices'][0]['quantity'],2)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM writes').fetchone()[0],0)

    def test_live_group_and_component_limits_sold_out_and_invalid_limits_stop(self):
        group=self.double_round();choice=group['choices'][0]
        self.save([{'round':group['round'],'name':'咖啡','quantity':2}])
        original=deepcopy(group)
        for case in ('group','component','sold_out','bool','missing_limit','unsupported'):
            group.clear();group.update(deepcopy(original));choice=group['choices'][0]
            if case=='group':group['max_quantity']=1
            if case=='component':choice['max_quantity']=1
            if case=='sold_out':choice['available']=False
            if case=='bool':choice['max_quantity']=True
            if case=='missing_limit':choice['max_quantity']=None
            if case=='unsupported':choice['standard_configuration_supported']=False
            with self.subTest(case=case):
                result=self.plan()
                self.assertFalse(result['selection_ready']);self.assertEqual(result['items'],[])

    def test_component_upcharge_and_optional_fee_multiply_by_both_quantities(self):
        group=self.double_round();choice=group['choices'][0]
        choice['extra_cny']='1.50'
        choice['modifiers']={'supported':True,'groups':[{'kind':'optional_choice','min_quantity':0,'max_quantity':1,
            'choices':[{'code':'123','name':'换奶','default_quantity':0,'extra_cny':'2.00','available':True}]}]}
        self.save([{'round':group['round'],'name':'咖啡','quantity':2}],quantity=2,
            component_modifiers=[{'round':group['round'],'component_name':'咖啡','modifiers':[{'name':'换奶','quantity':1}]}])
        self.preferences.max_amount_cny='40.00';save_preferences(self.store,self.preferences)
        result=self.plan()
        self.assertTrue(result['selection_ready'])
        self.assertEqual(result['estimated_amount_cny'],'34.00')
        self.preferences.max_amount_cny='33.99';save_preferences(self.store,self.preferences)
        self.assertIn('estimated_price_exceeds_preference_limit',json.dumps(self.plan()))

    def test_same_name_in_different_rounds_requires_explicit_scope(self):
        second=deepcopy(self.detail['groups'][0]);second['round']+=1
        self.detail['groups'].append(second)
        result=self.plan()
        self.assertFalse(result['selection_ready'])
        self.assertIn('preferred_component_name_ambiguous_use_round_choices',json.dumps(result))
        self.save([{'round':g['round'],'name':'咖啡'} for g in self.detail['groups']])
        self.assertTrue(self.plan()['selection_ready'])

    def test_round_specific_modifiers_do_not_change_the_other_cup(self):
        group=self.detail['groups'][0]
        group['choices'][0]['modifiers']={'supported':True,'groups':[{'min_quantity':0,'max_quantity':1,
            'choices':[{'code':'123','name':'糖','default_quantity':1,'extra_cny':'0.00'}]}]}
        second=deepcopy(group);second['round']+=1;self.detail['groups'].append(second)
        self.save([{'round':g['round'],'name':'咖啡'} for g in self.detail['groups']],
            component_modifiers=[{'round':second['round'],'component_name':'咖啡','modifiers':[{'name':'糖','quantity':0}]}])
        result=self.plan();self.assertTrue(result['selection_ready'])
        first_cup,second_cup=[g['items'][0] for g in result['items'][0]['combo']]
        self.assertNotIn('modifiers',first_cup)
        self.assertEqual(second_cup['modifiers'],[{'code':'123','quantity':0}])

    def test_legacy_unscoped_modifiers_on_two_selected_rounds_stop(self):
        second=deepcopy(self.detail['groups'][0]);second['round']+=1;self.detail['groups'].append(second)
        self.save([{'round':g['round'],'name':'咖啡'} for g in self.detail['groups']],
            component_modifiers=[{'component_name':'咖啡','modifiers':[{'name':'糖','quantity':0}]}])
        self.assertFalse(self.plan()['selection_ready'])

    def test_missing_name_unknown_round_and_duplicate_live_round_stop(self):
        for choices in ([{'round':99,'name':'咖啡'}],[{'round':self.detail['groups'][0]['round'],'name':'不存在的饮品'}]):
            self.save(choices)
            self.assertFalse(self.plan()['selection_ready'])
        self.save([{'round':self.detail['groups'][0]['round'],'name':'咖啡'}])
        self.detail['groups'].append(deepcopy(self.detail['groups'][0]))
        self.assertFalse(self.plan()['selection_ready'])

    def test_same_name_twice_within_one_round_needs_a_different_selection(self):
        group=self.detail['groups'][0]
        group['choices'].append(group['choices'][0]|{'product_code':'200002'})
        self.save([{'round':group['round'],'name':'咖啡'}])
        self.assertFalse(self.plan()['selection_ready'])

    def test_schema_refuses_duplicate_mixed_legacy_and_overlapping_modifier_requests(self):
        value=self.preferences.model_dump()
        for patch in (
                {'component_choices':[{'round':1,'name':'咖啡'}]},
                {'component_names':[],'component_choices':[{'round':1,'name':'咖啡'},{'round':1,'name':'咖啡'}]},
                {'component_names':[],'component_choices':[{'round':1,'name':' ','quantity':2}]},
                {'component_names':[],'component_choices':[{'round':1,'name':'咖啡','quantity':True}]},
                {'component_names':[],'component_choices':[{'round':1,'name':'咖啡','quantity':0}]},
                {'component_modifiers':[{'component_name':'咖啡','modifiers':[{'name':'糖','quantity':0}]},
                     {'round':1,'component_name':'咖啡','modifiers':[{'name':'糖','quantity':1}]}]}):
            copy=deepcopy(value);copy['items'][0].update(patch)
            with self.subTest(patch=patch),self.assertRaises(ValidationError):MealPreferences.model_validate(copy)

    def test_omitted_optional_and_unique_required_one_portion_rounds_still_work(self):
        self.save([])
        # A sole mandatory component of quantity one is the only implicit choice.
        self.assertTrue(self.plan()['selection_ready'])
        self.detail['groups'][0].update(min_quantity=0,max_quantity=1)
        self.assertEqual(self.plan()['items'][0]['combo'][0]['items'],[])
        self.double_round()
        self.assertFalse(self.plan()['selection_ready'])

    def test_new_unset_fields_do_not_rewrite_legacy_preference_shape(self):
        value=self.preferences.model_dump()
        self.assertNotIn('component_choices',value['items'][0])
        value['items'][0]['component_modifiers']=[{'component_name':'咖啡','modifiers':[{'name':'糖','quantity':0}]}]
        self.assertNotIn('round',MealPreferences.model_validate(value).model_dump()['items'][0]['component_modifiers'][0])

    def test_public_quantity_limit_and_cart_configuration_agree(self):
        offer,detail,_=products.fixture()
        group=detail['product']['comboItems'][1]
        group.update(minQuantity=2,maxQuantity=2)
        group['comboProducts'][1]['comboMaxQuantity']=2
        public=public_detail(detail,offer)
        self.assertEqual(public['groups'][1]['choices'][1]['max_quantity'],2)
        self.detail.clear();self.detail.update(public)
        self.offer.update(name=public['name'],product_code=public['product_code'])
        value=self.preferences.model_dump();value['items'][0].update(name=public['name'],component_names=[],
            component_choices=[{'round':2,'name':'豆浆','quantity':2}])
        save_preferences(self.store,MealPreferences.model_validate(value))
        plan=self.plan();self.assertTrue(plan['selection_ready'])
        selected=configured_product(ItemSelection.model_validate(plan['items'][0]),offer,detail,1)
        self.assertEqual(selected['comboItems'][1]['comboProducts'][0]['quantity'],2)

    def test_unrecognized_promotion_is_reported_before_a_plan_claims_ready(self):
        offer,detail,_=products.fixture()
        for field in ('cardId','cardName','couponId','couponCode','promotionId'):
            offer.pop(field,None)
        offer['productPromotions']=[{'type':'unsupported-promotion'}]
        public=public_detail(detail,offer)
        self.assertFalse(public['standard_configuration_supported'])
        self.assertEqual(public['configuration_issues'],['promotion_configuration_requires_official_cart'])
        self.detail.clear();self.detail.update(public)
        self.offer.update(name=public['name'],product_code=public['product_code'])
        value=self.preferences.model_dump();value['items'][0].update(name=public['name'],component_names=['豆浆'])
        save_preferences(self.store,MealPreferences.model_validate(value))
        plan=self.plan()
        self.assertFalse(plan['selection_ready']);self.assertEqual(plan['items'],[])
        self.assertIn('promotion_configuration_requires_official_cart',json.dumps(plan))


if __name__=='__main__':unittest.main()
