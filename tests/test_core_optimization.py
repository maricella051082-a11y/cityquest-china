import os
import io
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("GEOAPIFY_API_KEY", "test-geo-key")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")

import main


class _Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return {
            "features": [
                {"properties": {
                    "place_id": "near",
                    "name": "近点",
                    "categories": ["tourism.sights"],
                    "lat": 30.005,
                    "lon": 120.0,
                }},
                {"properties": {
                    "place_id": "far",
                    "name": "远点",
                    "categories": ["tourism.sights"],
                    "lat": 30.2,
                    "lon": 120.0,
                }},
            ]
        }


class _Session:
    def __init__(self):
        self.params = None

    def get(self, _url, params):
        self.params = params
        return _Response()


class GeoTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_start_uses_circle_and_explicit_distance_filter(self):
        session = _Session()
        start = {"lat": 30.0, "lon": 120.0}
        city = {"lat": 30.1, "lon": 120.1, "place_id": "city-polygon"}

        places = await main.fetch_places_source(
            session,
            city,
            "history",
            ["tourism.sights"],
            bias_point=start,
            search_radius_m=2_000,
        )

        self.assertEqual(session.params["filter"], "circle:120.0,30.0,2000")
        self.assertEqual([place["place_id"] for place in places], ["near"])
        self.assertLessEqual(places[0]["distance_from_search_center_m"], 2_060)

    def test_location_thresholds_are_ordered(self):
        self.assertEqual(main.POI_SEARCH_RADII_M, (2_000, 5_000, 10_000, 15_000))
        self.assertLess(main.LOCATION_ACCEPT_DISTANCE_M, main.LOCATION_WARN_DISTANCE_M)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_ranked_route_can_win(self):
        pool = [
            {"name": "A", "lat": 30.000, "lon": 120.000},
            {"name": "B", "lat": 30.001, "lon": 120.000},
            {"name": "C", "lat": 30.003, "lon": 120.000},
        ]
        poor = {
            "distance_m": 20_000,
            "time_s": 10_000,
            "legs": [{"time_s": 5_000}],
            "verified": True,
        }
        good = {
            "distance_m": 1_000,
            "time_s": 900,
            "legs": [{"time_s": 450}],
            "verified": True,
        }

        with patch.object(
            main,
            "walking_route",
            AsyncMock(side_effect=[poor, good]),
        ) as routing:
            selected, route = await main.try_route_combinations(
                pool,
                [2],
                "2 часа",
                lambda _combo: True,
                relaxed=False,
            )

        self.assertEqual(routing.await_count, 2)
        self.assertIs(route, good)
        self.assertEqual(len(selected), 2)


class CopyTests(unittest.TestCase):
    def test_copy_is_cut_at_word_boundary(self):
        value = "Найди интересную деталь и внимательно сравни её с соседней деталью"
        shortened = main.clean_ai_mission_value(value, 40)
        self.assertFalse(shortened.endswith("соседн"))
        self.assertLessEqual(len(shortened), 40)

    def test_food_hint_is_not_padded_with_ai_paragraph(self):
        mission = {"tip": "Посмотри на меню.", "type": "menu"}
        place = {"categories": ["catering.restaurant"]}
        self.assertEqual(main.apply_human_mission_copy(place, mission), mission)

    def test_awful_russian_calques_are_rejected_for_editing(self):
        self.assertFalse(main.russian_editorial_ok("Форма в тени", "general"))
        self.assertFalse(main.russian_editorial_ok("Две загадки в меню", "general"))
        self.assertFalse(
            main.russian_editorial_ok(
                "Выбери два блюда и угадай, какие вкусы они могут носить.",
                "general",
            )
        )

    def test_descriptive_english_poi_gets_russian_display_name(self):
        place = {"name": "Light show (Dancing fountains)"}
        self.assertEqual(
            main.safe_russian_name(place),
            "Световое шоу «Танцующие фонтаны»",
        )


class NavigationTests(unittest.TestCase):
    def test_archive_media_navigation_has_no_dead_end(self):
        markup = main.passport_media_keyboard(12, 34)
        callbacks = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        }
        self.assertEqual(
            callbacks,
            {"passport_quest:12", "passport_city:34", "my_quests", "home"},
        )

    def test_new_quest_launches_first_mission_from_bottom(self):
        quest = {"stops": [{"name_ru": "Первая"}]}
        markup = main.quest_launch_keyboard(quest)
        callbacks = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        }
        self.assertEqual(callbacks, {"quest_stop:0", "show_checklist", "home"})

    def test_checklist_has_route_back_to_mission_and_home(self):
        quest = {"stops": [{"name_ru": "Первая"}, {"name_ru": "Вторая"}]}
        markup = main.checklist_keyboard(quest, [0])
        callbacks = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        }
        self.assertIn("quest_stop:1", callbacks)
        self.assertIn("home", callbacks)


class ProgressUiTests(unittest.TestCase):
    def test_active_summary_and_checklist_do_not_show_xp(self):
        quest = {
            "title": "Тест",
            "stops": [{"name_ru": "Первая", "mission": {"xp": 50}}],
        }
        data = {
            "quest": quest,
            "city": {"city": "Beijing"},
            "completed": [0],
            "bonuses": [],
            "photos": {"0": "file-id"},
        }
        self.assertNotIn("XP", main.active_quest_summary(data))
        self.assertNotIn(
            "XP",
            main.checklist_text(quest, [0], [], {"0": "file-id"}),
        )

    def test_passport_groups_collect_photo_count(self):
        records = [{
            "id": 1,
            "city": "Пекин",
            "xp": 500,
            "photos": 3,
            "payload": {"quest": {"stops": []}, "completed": []},
        }]
        group = main.passport_city_groups(records)[0]
        self.assertEqual(group["photos"], 3)


class TravelCardFrameTests(unittest.TestCase):
    def test_only_new_png_styles_are_public(self):
        self.assertEqual(
            set(main.TRAVEL_CARD_STYLES),
            {"none", "chinese_seal", "ink_travel", "china_journal"},
        )
        self.assertEqual(
            main.normalize_travel_card_style("chinese"),
            "chinese_seal",
        )
        self.assertEqual(
            main.normalize_travel_card_style("journal"),
            "china_journal",
        )

    def test_frame_assets_have_expected_normal_names(self):
        for filename in main.TRAVEL_CARD_FRAME_FILES.values():
            self.assertTrue(
                os.path.isfile(os.path.join(main.FRAME_ASSETS_DIR, filename)),
                filename,
            )
            self.assertFalse(filename.endswith(".png.png"))

    def test_none_keeps_pixels_and_png_overlay_keeps_canvas_size(self):
        base = Image.new("RGB", (1080, 1350), (12, 34, 56))
        no_frame = main.apply_travel_card_frame(base, "none")
        framed = main.apply_travel_card_frame(base, "chinese_seal")
        self.assertEqual(no_frame.size, base.size)
        self.assertEqual(framed.size, base.size)
        self.assertEqual(no_frame.getpixel((540, 675))[:3], (12, 34, 56))
        # Transparent centre must reveal the original card.
        self.assertEqual(framed.getpixel((540, 675))[:3], (12, 34, 56))

    def test_byte_overlay_returns_rgb_jpeg_at_same_size(self):
        source = io.BytesIO()
        Image.new("RGB", (320, 400), "white").save(source, format="JPEG")
        output = main.apply_travel_card_frame_bytes(
            source.getvalue(),
            "ink_travel",
        )
        with Image.open(io.BytesIO(output)) as result:
            self.assertEqual(result.format, "JPEG")
            self.assertEqual(result.mode, "RGB")
            self.assertEqual(result.size, (320, 400))


if __name__ == "__main__":
    unittest.main()
