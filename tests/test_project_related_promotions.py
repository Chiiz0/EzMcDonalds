from copy import deepcopy
import json
import unittest

from ezmcd.errors import EzMcdError
from ezmcd.models import ItemSelection
from ezmcd.preferences import MealPreferences, save_preferences, plan_meal
from ezmcd.products import configured_product, public_detail, requires_official_promotion, verify_cart_contents
from ezmcd.promotions import ordinary_offer_with_unavailable_related_promotion
import test_project_products as products
import test_project_preferences as preferences


def fixture():
    offer, detail, selection = products.fixture()
    for key in ('cardId','cardName','cardType','couponId','couponCode','promotionId'):
        offer.pop(key, None)
    offer['priceInfo'] = {'eatInPrice':7300}
    offer['productPromotions'] = [{'productCode':'900099','promotionInfo':{
        'hasRights':False,'membershipCode':'106','pmtType':3,'promotionId':'synthetic-promotion',
        'promotionLimitNum':2,'specId':'synthetic-spec','withOrder':True}}]
    detail['product'].update(price=7300, startingPrice=7300)
    return offer, detail, selection


class RelatedPromotionTests(unittest.TestCase):
    def test_distinct_unavailable_related_offer_allows_only_original_item(self):
        offer, detail, selection = fixture()
        before = deepcopy((offer, detail))
        public = public_detail(detail, offer)
        self.assertTrue(public['standard_configuration_supported'])
        self.assertEqual(public['promotion_handling'], 'ordinary_price_only_related_member_offer_unavailable')
        product = configured_product(selection, offer, detail, 1)
        self.assertEqual(product['code'], selection.product_code)
        self.assertFalse(product['useRightCard'])
        self.assertFalse(any(k in product for k in ('cardId','couponId','couponCode','promotionId','productPromotions')))
        self.assertNotIn('synthetic-promotion', json.dumps(public))
        self.assertEqual((offer, detail), before)

    def test_same_product_or_available_rights_never_fall_back_to_regular_price(self):
        for change in ('self','rights','with_order','membership','type','limit','missing','unknown'):
            offer, detail, selection = fixture()
            row=offer['productPromotions'][0]; info=row['promotionInfo']
            if change=='self': row['productCode']=offer['productCode']
            elif change=='rights': info['hasRights']=True
            elif change=='with_order': info['withOrder']=False
            elif change=='membership': info['membershipCode']='unrecognized'
            elif change=='type': info['pmtType']=True
            elif change=='limit': info['promotionLimitNum']=0
            elif change=='missing': info.pop('hasRights')
            else: info['unrecognizedBenefit']=True
            with self.subTest(change=change):
                self.assertFalse(public_detail(detail,offer)['standard_configuration_supported'])
                with self.assertRaisesRegex(EzMcdError,'promotion_configuration_requires_official_cart'):
                    configured_product(selection,offer,detail,1)

    def test_differing_menu_detail_starting_prices_or_boolean_prices_stop(self):
        for where, key, value in (('offer','eatInPrice',4990),('product','price',11750),
                ('product','startingPrice',7000),('product','price',True),('offer','eatInPrice',None)):
            offer, detail, selection = fixture()
            (offer['priceInfo'] if where=='offer' else detail['product'])[key]=value
            self.assertTrue(requires_official_promotion(offer,detail))
            with self.assertRaises(EzMcdError): configured_product(selection,offer,detail,1)

    def test_unknown_multiple_empty_or_malformed_related_rows_are_not_assumed_safe(self):
        offer,detail,_=fixture()
        for rows in ([{}],offer['productPromotions']*2,{'productCode':'900099'},[None],
                     [{'productCode':'900099','promotionInfo':None}],
                     [offer['productPromotions'][0]|{'automatic':True}]):
            self.assertFalse(ordinary_offer_with_unavailable_related_promotion(offer|{'productPromotions':rows},detail))
            self.assertTrue(requires_official_promotion(offer|{'productPromotions':rows},detail))
        self.assertTrue(requires_official_promotion(offer))
        for altered in (None,{}, {'product':[]}, detail|{'product':None}):
            self.assertFalse(ordinary_offer_with_unavailable_related_promotion(offer,altered))

    def test_top_level_promotion_or_coupon_still_requires_its_own_flow(self):
        for key in ('couponId','couponCode','promotionId'):
            offer,detail,selection=fixture();offer[key]='synthetic-other'
            with self.assertRaisesRegex(EzMcdError,'promotion_configuration'):
                configured_product(selection,offer,detail,1)
        offer,detail,selection=products.fixture()
        product=configured_product(selection,offer,detail,1)
        self.assertEqual(product['cardId'],'private-card')

    def test_no_promotion_ordinary_item_keeps_existing_path(self):
        offer,detail,selection=fixture();offer['productPromotions']=[]
        product=configured_product(selection,offer,detail,1)
        self.assertFalse(product['useRightCard'])
        self.assertIsNone(public_detail(detail,offer)['promotion_handling'])

    def test_multiple_copies_in_a_round_serialize_and_require_exact_cart_quantities(self):
        offer,detail,selection=fixture();group=detail['product']['comboItems'][1]
        group.update(minQuantity=2,maxQuantity=2)
        group['comboProducts'][1]['comboMaxQuantity']=2
        value=selection.model_dump();value['combo'][1]['items'][0]['quantity']=2
        product=configured_product(ItemSelection.model_validate(value),offer,detail,1)
        cart={'items':[{'product_code':product['code'],'product_type':'7','quantity':1,
            'components':product['comboItems'],'configuration':{},'coupon_fingerprint':None}]}
        verify_cart_contents({'products':[product]},cart)
        changed=deepcopy(cart);changed['items'][0]['components'][1]['comboProducts'][0]['quantity']=1
        with self.assertRaisesRegex(EzMcdError,'cart_contents_do_not_match'):
            verify_cart_contents({'products':[product]},changed)
        changed=deepcopy(cart);changed['items'][0]['coupon_fingerprint']='unexpected-discount'
        with self.assertRaisesRegex(EzMcdError,'cart_contents_do_not_match'):
            verify_cart_contents({'products':[product]},changed)


class RelatedPromotionPlanTests(unittest.TestCase):
    setUp = preferences.PreferenceTests.setUp

    def prepare(self):
        offer,detail,_=fixture()
        group=detail['product']['comboItems'][1];group.update(minQuantity=2,maxQuantity=2)
        group['comboProducts'][1]['diffPrice']=0
        public=public_detail(detail,offer)
        self.detail.clear();self.detail.update(public)
        self.offer.update(name=public['name'],product_code=public['product_code'],display_price_cny='73.00')
        value=self.preferences.model_dump();value['max_amount_cny']='73.00'
        value['items'][0].update(name=public['name'],component_names=[],
            component_choices=[{'round':2,'name':'豆浆','quantity':2}])
        prefs=MealPreferences.model_validate(value);save_preferences(self.store,prefs)
        return offer,detail,prefs

    def test_plan_reports_ordinary_price_handling_and_budget_before_cart(self):
        offer,detail,prefs=self.prepare()
        plan=plan_meal(self.service,'test')
        self.assertTrue(plan['selection_ready']);self.assertEqual(plan['estimated_amount_cny'],'73.00')
        self.assertEqual(plan['products'][0]['promotion_handling'],'ordinary_price_only_related_member_offer_unavailable')
        product=configured_product(ItemSelection.model_validate(plan['items'][0]),offer,detail,1)
        self.assertEqual(product['comboItems'][1]['comboProducts'][0]['quantity'],2)
        prefs.max_amount_cny='72.99';save_preferences(self.store,prefs)
        plan=plan_meal(self.service,'test');self.assertFalse(plan['selection_ready'])
        self.assertEqual(plan['items'],[])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM writes').fetchone()[0],0)

    def test_eligibility_change_after_plan_is_rechecked_before_cart_draft(self):
        offer,detail,_=self.prepare();plan=plan_meal(self.service,'test')
        offer['productPromotions'][0]['promotionInfo']['hasRights']=True
        with self.assertRaisesRegex(EzMcdError,'promotion_configuration'):
            configured_product(ItemSelection.model_validate(plan['items'][0]),offer,detail,1)
        self.detail.clear();self.detail.update(public_detail(detail,offer))
        self.assertFalse(plan_meal(self.service,'test')['selection_ready'])

if __name__=='__main__':unittest.main()
