"""Staff pin API for the homepage High return rail."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Property

URL = "/api/mls/studio/high-return/"


class StudioHighReturnTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user(
            username="hr-staff",
            password="x",
            is_staff=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff)
        self.listing = Property.objects.create(
            listing_key="HR-1",
            city="Toronto",
            state_or_province="ON",
            list_price=700000,
            standard_status="Active",
            unparsed_address="10 Yield Ave",
        )

    def test_patch_pins_and_get_lists(self):
        response = self.client.patch(
            URL,
            {
                "listing_key": "HR-1",
                "is_high_return": True,
                "high_return_order": 1,
                "high_return_until": None,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["is_high_return"])
        self.assertEqual(response.json()["high_return_order"], 1)

        listed = self.client.get(URL)
        self.assertEqual(listed.status_code, 200)
        pins = listed.json()["pins"]
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["listing_key"], "HR-1")

    def test_requires_staff(self):
        anon = APIClient()
        self.assertEqual(anon.get(URL).status_code, 401)
