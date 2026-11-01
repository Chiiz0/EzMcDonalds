from copy import deepcopy
import unittest

from ezmcd.catalog import checkout
from ezmcd.errors import EzMcdError
from ezmcd.models import ModifierSelection
from ezmcd.modifiers import public_modifiers, selected_modification, verify_modification, cart_wire_body
from ezmcd.preferences import MealPreferences, configuration_extra, plan_meal, save_preferences
import test_project_fulfillment as fulfillment_tests
import test_project_preferences as preference_tests
from test_project_optional_modifiers import milk_option


def espresso():
    return {'code':'1001','type':1,'name':'示例咖啡','modification':{'items':[
        {'title':'加一份浓缩(单选)','minValues':1,'maxValues':1,'values':[
            {'code':'8002','name':name,'displayMode':1,'minQuantity':0,'maxQuantity':1,
             'selectedQuantity':int(key=='0-0'),'selectedKey':key,'unselectedKey':None,
             'price':price,'soldOut':False}
            for key,name,price in (('0-0','不加',0),('0-1','加',500))]}]}}


def choice(key='0-1'):
    return [ModifierSelection(code='8002',quantity=1,key=key)]


def echo(price=500,key='0-1'):
    return {'customization':{'options':[],'items':[
        {'code':'8002','name':'加一份浓缩','key':key,'displayText':'加一份浓缩(加+¥5)',
         'values':[{'code':'2','name':'加','price':price,'quantity':1}]}]}}


class PaidChoiceTests(unittest.TestCase):
    def test_default_stays_free_while_detail_exposes_the_priced_option(self):
        result=selected_modification(espresso())
        self.assertEqual(result,{'modification':{'values':[{'code':'8002','quantity':1,'key':'0-0'}]}})
        paid=public_modifiers(espresso())['groups'][0]['choices'][1]
        self.assertTrue(paid['selection_supported']);self.assertEqual(paid['extra_cny'],'5.00')
        self.assertTrue(paid['paid_selection_supported'])
        self.assertFalse(public_modifiers(espresso())['groups'][0]['choices'][0]['paid_selection_supported'])

    def test_explicit_addon_keeps_exact_local_fee_and_native_quantity(self):
        selected=selected_modification(espresso(),choice())
        self.assertEqual(selected['_ezmcd_modifier_prices'],{'8002':500})
        sent=cart_wire_body({'products':[selected]})['products'][0]
        self.assertEqual(sent,{'modification':{'values':[{'code':'8002','quantity':1,'key':'0-1'}]}})
        verify_modification(selected,echo())

    def test_price_default_and_shape_changes_cannot_turn_into_implicit_paid_choices(self):
        for change in ('price_cap','paid_default','free_not_default','different_key','third_choice','two_paid'):
            p=espresso();values=p['modification']['items'][0]['values']
            if change=='price_cap':values[1]['price']=100001
            if change=='paid_default':values[1]['selectedQuantity']=1;values[0]['selectedQuantity']=0
            if change=='free_not_default':values[0]['selectedQuantity']=0
            if change=='different_key':values[1]['selectedKey']='0-2'
            if change=='third_choice':values.append(values[1]|{'selectedKey':'0-2'})
            if change=='two_paid':values[0]['price']=100
            with self.subTest(change=change),self.assertRaises(EzMcdError):
                selected_modification(p,choice(values[1]['selectedKey']))

    def test_sold_out_addon_is_not_silently_replaced_by_free_default(self):
        p=espresso();p['modification']['items'][0]['values'][1]['soldOut']=True
        self.assertTrue(public_modifiers(p)['supported'])
        with self.assertRaisesRegex(EzMcdError,'available_supported'):selected_modification(p,choice())

    def test_missing_changed_fee_wrong_choice_or_missing_echo_is_rejected(self):
        selected=selected_modification(espresso(),choice())
        for actual in (echo(0),echo(400),echo(600),echo(500,'0-0'),{}, {'modification':selected['modification']}):
            with self.subTest(actual=actual),self.assertRaises(EzMcdError):verify_modification(selected,actual)

    def test_combined_milk_and_espresso_keep_two_independent_surcharges(self):
        p=espresso();p['modification']['items'].extend(milk_option()['modification']['items'])
        selected=selected_modification(p,choice()+[ModifierSelection(code='8001',quantity=1)])
        self.assertEqual(selected['_ezmcd_modifier_prices'],{'8002':500,'8001':200})
        public=public_modifiers(p)
        selections=[s.model_dump() for s in choice()]+[{'code':'8001','quantity':1}]
        self.assertEqual(configuration_extra({'modifiers':public},selections,[]),700)
        selections[0]['key']='0-0'
        self.assertEqual(configuration_extra({'modifiers':public},selections,[]),200)


class PaidChoicePriceTests(unittest.TestCase):
    def setUp(self):
        fulfillment_tests.OrdinaryCartPriceTests.setUp(self)
        self.args=self.args.model_copy(update={'max_amount_cny':'30'})
        self.product.update(realPrice=1350,realSubTotalPrice=2700,**echo())
        self.cart.update(realTotalPrice=2700,totalPrice=2700)
        self.row.update(price='13.50',subtotal='27.00',realSubtotal='27.00',
            comboItemList=[{'name':'加一份浓缩(加+¥5)','quantity':1}])
        self.confirmation['productPriceInfo'].update(realTotalAmount='27.00',totalAmount='27.00')

    def test_quantity_price_and_budget_include_the_addon(self):
        result=checkout(self.args,self.cart,self.confirmation,'2')
        self.assertEqual(result['amount_cents'],2700)
        with self.assertRaisesRegex(EzMcdError,'exceeds_limit'):
            checkout(self.args.model_copy(update={'max_amount_cny':'26'}),self.cart,self.confirmation,'2')

    def test_confirmation_cannot_omit_or_replace_the_paid_label(self):
        for labels in ([],[{'name':'不加','quantity':1}],[{'name':'加一份浓缩(加+¥5)','quantity':2}]):
            self.row['comboItemList']=labels
            with self.assertRaises(EzMcdError):checkout(self.args,self.cart,self.confirmation,'2')


class PaidChoicePreferenceTests(unittest.TestCase):
    def setUp(self):
        preference_tests.PreferenceTests.setUp(self)
        self.offer.update(product_type=1,display_price_cny='10.00')
        self.detail.update(groups=[],modifiers=public_modifiers(espresso()),standard_configuration_supported=True)
        self.preferences=MealPreferences(store_code='9000001',pickup_time='10:15',max_amount_cny='35',
            items=[{'name':'示例组合','quantity':2,'modifiers':[{'name':'加','group_name':'加一份浓缩(单选)','quantity':1}]}])
        save_preferences(self.store,self.preferences)

    def test_named_paid_choice_counts_each_product_once_not_both_same_code_options(self):
        result=plan_meal(self.service,'test')
        self.assertTrue(result['selection_ready']);self.assertEqual(result['estimated_amount_cny'],'30.00')
        self.assertEqual(result['items'][0]['modifiers'],[{'code':'8002','quantity':1,'key':'0-1'}])

    def test_budget_stops_before_add_when_base_fits_but_fee_does_not(self):
        self.preferences.max_amount_cny='25';save_preferences(self.store,self.preferences)
        result=plan_meal(self.service,'test')
        self.assertFalse(result['selection_ready'])
        self.assertIn('estimated_price_exceeds_preference_limit',[i['reason'] for i in result['issues']])

    def test_omit_or_choose_free_option_does_not_charge_hidden_extra(self):
        for modifiers in ([],[{'name':'不加','quantity':1}]):
            raw=self.preferences.model_dump();raw['items'][0]['modifiers']=modifiers
            save_preferences(self.store,MealPreferences.model_validate(raw))
            result=plan_meal(self.service,'test')
            self.assertTrue(result['selection_ready']);self.assertEqual(result['estimated_amount_cny'],'20.00')

    def test_sold_out_unsupported_or_ambiguous_preference_stops(self):
        for change in ('sold_out','unsupported','unverified_fee','ambiguous'):
            self.detail['modifiers']=public_modifiers(espresso())
            group=self.detail['modifiers']['groups'][0]
            if change=='sold_out':group['choices'][1]['available']=False
            if change=='unsupported':group['choices'][1]['selection_supported']=False
            if change=='unverified_fee':group['choices'][1]['paid_selection_supported']=False
            if change=='ambiguous':group['choices'].append(deepcopy(group['choices'][1]))
            self.assertFalse(plan_meal(self.service,'test')['selection_ready'])

    def test_combo_component_addon_uses_component_quantity(self):
        detail={'groups':[{'round':1,'choices':[{'product_code':'1001','modifiers':public_modifiers(espresso())}]}]}
        groups=[{'round':1,'items':[{'product_code':'1001','quantity':2,'modifiers':[s.model_dump() for s in choice()]}]}]
        self.assertEqual(configuration_extra(detail,None,groups),1000)


if __name__=='__main__':unittest.main()
