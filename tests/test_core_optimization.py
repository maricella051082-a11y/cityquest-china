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

    def test_manual_start_confirmation_shows_address_not_coordinates(self):
        candidate = {
            "name": "The Temple House",
            "category": "accommodation.hotel",
            "street": "Bitieshi Street",
            "housenumber": "81",
            "district": "Jinjiang District",
            "city": "Chengdu",
            "state": "Sichuan",
            "lat": 30.65987,
            "lon": 104.06332,
            "distance_from_city_m": 10_000,
        }
        text = main.manual_start_candidate_text(
            candidate,
            {"city": "Chengdu", "lat": 30.57, "lon": 104.06},
        )
        self.assertIn("Отель:</b> The Temple House", text)
        self.assertIn("Адрес:</b> Bitieshi Street 81", text)
        self.assertIn("Город:</b> Chengdu, Сычуань", text)
        self.assertNotIn("Jinjiang District", text)
        self.assertNotIn("30.65987", text)
        self.assertNotIn("Координаты", text)

    def test_internal_corridor_is_not_shown_as_postal_address(self):
        candidate = {
            "name": "Zhu's Family Garden",
            "address_line1": "Corridor of Little Rainbow",
            "state": "Yunnan",
            "distance_from_city_m": 2_000,
        }
        text = main.manual_start_candidate_text(
            candidate,
            {"city": "Jianshui", "verified_name_ru": "Цзяньшуй", "state": "Yunnan"},
        )
        self.assertIn("Точка старта найдена", text)
        self.assertIn("Цзяньшуй, Юньнань", text)
        self.assertNotIn("Corridor of Little Rainbow", text)


class StatusMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_message_is_reused_for_later_status_updates(self):
        class Chat:
            id = 101

        class Message:
            chat = Chat()
            message_id = 202

        original = Message()
        replacement = Message()
        replacement.message_id = 203
        original.edit_text = AsyncMock(side_effect=RuntimeError("not editable"))
        original.answer = AsyncMock(return_value=replacement)
        replacement.edit_text = AsyncMock(return_value=replacement)
        replacement.answer = AsyncMock(return_value=replacement)
        main.STATUS_MESSAGE_REPLACEMENTS.clear()

        first = await main.safe_status_edit(original, "Первый статус")
        second = await main.safe_status_edit(original, "Второй статус")

        self.assertIs(first, replacement)
        self.assertIs(second, replacement)
        original.answer.assert_awaited_once()
        replacement.edit_text.assert_awaited_once()


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def test_route_rejects_four_monuments_even_in_compact_mode(self):
        monuments = [
            {"category_label": "🗿 памятник", "interest_matches": []}
            for _ in range(4)
        ]
        self.assertFalse(main.combo_ok(monuments, []))
        self.assertFalse(main.relaxed_combo_ok(monuments, []))

    def test_route_summary_treats_remaining_time_as_exploration(self):
        quest = {"stops": [
            {"mission": {"minutes": 12}},
            {"mission": {"minutes": 12}},
        ]}
        route = {"time_s": 600, "distance_m": 700, "legs": []}
        text = main.route_summary(route, quest, "6 часов")
        self.assertIn("На осмотр мест, еду и отдых", text)
        self.assertNotIn("Запас", text)

    def test_center_route_names_the_real_first_start(self):
        quest = {
            "stops": [{"name_ru": "Восточные ворота", "mission": {"minutes": 12}}],
            "field_missions": [],
        }
        route = {
            "time_s": 0,
            "distance_m": 0,
            "legs": [],
            "start_mode": "center",
        }
        text = main.route_summary(route, quest, "2 часа")
        self.assertIn("Старт: <b>Восточные ворота</b>", text)
        self.assertIn("Открой первую миссию", text)
        self.assertNotIn("координат", text.lower())

    def test_all_day_route_can_be_compact_when_missions_fill_the_day(self):
        route = {"time_s": 10 * 60, "distance_m": 710, "legs": []}
        self.assertTrue(main.route_fits(route, "весь день", 7))

    def test_all_day_quest_reaches_fourteen_total_missions_with_few_places(self):
        missions = main.build_field_missions(
            "compact", ["nature"], 2, duration="весь день", ai_meta={}
        )
        self.assertEqual(len(missions), 12)
        combined = " ".join(item["text"] for item in missions).lower()
        self.assertTrue(any(word in combined for word in ("дерево", "цветок", "растение")))

    def test_every_duration_gets_linking_route_missions(self):
        cases = [("2 часа", 3, 1), ("4 часа", 5, 2), ("6 часов", 6, 4)]
        for duration, stops, expected in cases:
            with self.subTest(duration=duration):
                missions = main.build_field_missions(
                    "rich", ["history"], stops, duration=duration, ai_meta={}
                )
                self.assertEqual(len(missions), expected)
                self.assertTrue(all("after_poi_index" in item for item in missions))

    def test_full_quest_lists_route_missions(self):
        quest = {
            "stops": [{
                "name_ru": "Музей",
                "place": {"name": "Museum"},
                "mission": {"title": "Экспонат", "text": "Найди необычную деталь."},
            }],
            "field_missions": [{"title": "Живая находка", "text": "Найди красивое дерево."}],
        }
        route = {"time_s": 0, "distance_m": 0, "legs": []}
        text = main.full_quest_text(quest, route, "2 часа")
        self.assertIn("Миссии по пути", text)
        self.assertIn("Живая находка", text)

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
        self.assertFalse(main.russian_editorial_ok("Подчеркни визуальную напряжённость.", "general"))
        self.assertFalse(main.russian_editorial_ok("Запиши свою догадку.", "general"))

    def test_near_duplicate_missions_are_detected(self):
        first = {
            "title": "Цвет памятника",
            "text": "Выбери заметный цвет памятника и найди его в разных частях композиции.",
        }
        stop = {"mission": {
            "title": "Цвета памятника",
            "text": "Найди заметный цвет памятника в разных частях композиции и выбери главный.",
        }}
        self.assertTrue(main.mission_repeats_existing(first, [stop]))

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
        self.assertEqual(callbacks, {"quest_stop:0", "show_full_quest", "home"})

    def test_full_quest_view_contains_route_and_every_mission(self):
        quest = {
            "stops": [
                {"name_ru": "Музей", "mission": {"title": "Экспонат", "text": "Найди интересный предмет."}},
                {"name_ru": "Парк", "mission": {"title": "Цвет", "text": "Заметь необычный цвет."}},
            ],
        }
        route = {"time_s": 1200, "distance_m": 1500, "legs": []}
        text = main.full_quest_text(quest, route, "2 часа")
        self.assertIn("План прогулки", text)
        self.assertIn("1. Музей", text)
        self.assertIn("Найди интересный предмет", text)
        self.assertIn("2. Парк", text)
        self.assertIn("Заметь необычный цвет", text)

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

    def test_stop_card_contains_navigation_without_separate_menu(self):
        place = {"lat": 31.2, "lon": 121.5}
        markup = main.stop_keyboard(place, 1, False, 6)
        callbacks = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertIn("quest_stop:0", callbacks)
        self.assertIn("quest_stop:2", callbacks)
        self.assertIn("show_checklist", callbacks)
        self.assertIn("photo_add:s1", callbacks)

    def test_field_photo_has_own_callback_and_context(self):
        place = {"lat": 31.2, "lon": 121.5}
        field = {"title": "Живая находка", "photo": "Сними дерево."}
        markup = main.stop_keyboard(place, 0, False, 2, [(3, field)])
        callbacks = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertIn("photo_add:s0", callbacks)
        self.assertIn("photo_add:f3", callbacks)

        quest = {
            "stops": [{"name_ru": "Парк", "place": place, "mission": {"title": "Парк"}}],
            "field_missions": [{}, {}, {}, {"title": "Живая находка", "after_poi_index": 0}],
        }
        context = main.photo_context(quest, "f3")
        self.assertEqual(context[1], 0)
        self.assertEqual(context[3], "Живая находка")

    def test_legacy_numeric_photo_key_is_still_readable(self):
        self.assertEqual(main.photo_value({"2": "old-file"}, "s2"), "old-file")

    def test_field_photo_actions_keep_their_own_key(self):
        stop = {"place": {"category_label": "🏛 музей"}}
        markup = main.photo_actions_keyboard(stop, 0, version=2, photo_key="f3")
        callbacks = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertIn("vision:text:f3:2", callbacks)
        self.assertIn("photo_replace:f3", callbacks)

    def test_known_quest_place_does_not_offer_place_recognition(self):
        stop = {"place": {"category_label": "🏺 историческое место"}}
        markup = main.photo_actions_keyboard(stop, 0, version=1)
        callbacks = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertFalse(any(value.startswith("vision:place:") for value in callbacks))
        self.assertTrue(any(value.startswith("vision:monument:") for value in callbacks))

    def test_chinese_ruins_are_not_generic_attractions(self):
        categories = ["tourism.sights"]
        label = main.clean_category_label(categories, "成都东华门遗址")
        self.assertEqual(label, "🏺 историческое место")
        self.assertIn("Историческое место", main.translated_suffix_name("成都东华门遗址"))

    def test_long_chinese_poi_gets_readable_name_without_full_pinyin(self):
        place = {
            "name": "隋大兴唐长安城宫城南墙遗址",
            "pinyin": "Suí dà xīng táng cháng ān chéng gōng chéng nán qiáng yí zhǐ",
            "category_label": "🏺 историческое место",
        }
        stop = {
            "name_ru": "Достопримечательность · Sui Da Xing Tang Chang An Cheng Gong Cheng Nan Qiang Yi Zhi",
            "place": place,
        }
        self.assertEqual(
            main.display_stop_name(stop),
            "Остатки южной стены дворцового города Чанъань",
        )
        self.assertEqual(main.display_pinyin_for_place(place), "")

    def test_taiping_residence_gets_readable_russian_name(self):
        place = {
            "name": "太平天国听王府",
            "pinyin": "Tài píng tiān guó tīng wáng fǔ",
            "category_label": "🏯 историческая резиденция",
        }
        self.assertEqual(
            main.safe_russian_name(place),
            "Резиденция Тин-вана времён Тайпинского Небесного государства",
        )
        self.assertEqual(
            main.clean_category_label(["tourism.sights"], place["name"]),
            "🏯 историческая резиденция",
        )

    def test_unknown_long_chinese_name_does_not_become_pinyin_wall(self):
        place = {
            "name": "某某某某某某某某某某某历史建筑",
            "category_label": "🏯 историческое место",
        }
        shown = main.safe_russian_name(place)
        self.assertEqual(shown, "Историческое место")
        self.assertNotIn(" · ", shown)

    def test_text_photo_mission_explicitly_explains_translation(self):
        place = {"category_label": "🏺 историческое место"}
        mission = {
            "type": "text",
            "title": "Один иероглиф",
            "text": "Найди иероглиф.",
            "tip": "Выбери хорошо видимую надпись.",
            "photo": "Сфотографируй иероглиф.",
        }
        edited = main.apply_human_mission_copy(place, mission)
        self.assertIn("Загрузи снимок", edited["tip"])
        self.assertIn("Прочитать / перевести", edited["tip"])
        self.assertIn("pinyin", edited["tip"])
        self.assertNotIn("бот", edited["tip"].lower())
        self.assertNotIn("AI", edited["tip"])

    def test_food_photo_mission_explains_available_analysis(self):
        place = {"category_label": "🍜 ресторан"}
        mission = {
            "type": "color",
            "title": "Яркая тарелка",
            "text": "Выбери блюдо.",
            "tip": "Заказывать необязательно.",
            "photo": "Сфотографируй блюдо.",
        }
        edited = main.apply_human_mission_copy(place, mission)
        self.assertIn("узнать больше о блюде", edited["tip"])
        self.assertIn("возможный состав и остроту", edited["tip"])

    def test_full_quest_shows_photo_help_without_opening_mission(self):
        quest = {
            "stops": [{
                "name_ru": "Чайная",
                "place": {"category_label": "🍵 чайная", "name": "Tea"},
                "mission": {
                    "type": "compare",
                    "title": "Два аромата",
                    "text": "Сравни два чая.",
                    "tip": "Сравни варианты.",
                    "photo": "Сфотографируй два названия.",
                },
            }],
            "field_missions": [],
        }
        route = {"time_s": 0, "distance_m": 0, "legs": [], "start_mode": "center"}
        text = main.full_quest_text(quest, route, "2 часа")
        self.assertIn("Добавь фото", text)
        self.assertIn("Прочитать / перевести", text)
        self.assertIn("pinyin", text)

    def test_museum_tip_cannot_leak_ai_wording(self):
        place = {"category_label": "🏛 музей"}
        mission = {
            "type": "museum",
            "title": "Экспонат-загадка",
            "text": "Найди экспонат.",
            "tip": (
                "Сфотографируй экспонат и спроси AI. "
                "AI попробует объяснить, что это. "
                "Если фото запрещены, введи название вручную."
            ),
            "photo": "Сфотографируй экспонат.",
        }
        edited = main.apply_human_mission_copy(place, mission)
        self.assertNotIn("AI", edited["tip"])
        self.assertNotIn("бот", edited["tip"].lower())
        self.assertIn("Загрузи снимок", edited["tip"])
        self.assertIn("дополнительную информацию", edited["tip"])

    def test_mission_does_not_require_nonexistent_text_answer(self):
        original = (
            "Выбери один предмет, который привлёк внимание. "
            "Опиши, чем он отличается. Укажи, что понравилось, и запиши ответ."
        )
        edited = main.neutralize_unavailable_response_actions(original)
        self.assertNotRegex(edited.lower(), r"\b(?:опиши|укажи|запиши)\b")
        self.assertIn("Обрати внимание, чем он отличается", edited)
        self.assertIn("Реши для себя, что понравилось", edited)
        self.assertIn("запомни ответ", edited.lower())

    def test_museum_stop_restores_no_photo_action(self):
        place = {
            "lat": 31.2,
            "lon": 121.5,
            "category_label": "🏛 музей",
        }
        markup = main.stop_keyboard(place, 1, False, 6)
        callbacks = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertIn("museum_text_menu:1", callbacks)


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


class PoiTypeTests(unittest.TestCase):
    def test_statue_is_not_a_generic_sight(self):
        place = {
            "name": "孙中山像",
            "category_label": main.clean_category_label(
                ["tourism.sights"],
                "孙中山像",
            ),
        }
        self.assertEqual(place["category_label"], "🗿 статуя")
        self.assertEqual(main.place_group(place), "monument")
        self.assertNotIn("Достопримечательность", main.safe_russian_name(place))

    def test_named_types_override_generic_sight(self):
        cases = {
            "Shanghai Museum": "🏛 музей",
            "Summer Palace": "🏯 дворец",
            "People's Square": "🏙 площадь",
            "保卫和平坊": "🏮 мемориальный объект",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(
                    main.clean_category_label(["tourism.sights"], name),
                    expected,
                )

    def test_monument_fallback_mission_is_object_specific(self):
        mission = main.mission_for_place(
            {"name": "孙中山像", "category_label": "🗿 статуя"},
            [],
            0,
            set(),
        )
        combined = f"{mission['title']} {mission['text']}".lower()
        self.assertNotIn("крыши", combined)
        self.assertNotIn("архитектур", combined)


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

    def test_each_frame_has_its_own_safe_text_layout(self):
        layouts = main.TRAVEL_CARD_TEXT_LAYOUTS
        self.assertLess(layouts["ink_travel"]["bottom_right"], layouts["none"]["bottom_right"])
        self.assertGreater(layouts["ink_travel"]["header_left"], layouts["none"]["header_left"])
        self.assertNotEqual(layouts["chinese_seal"], layouts["china_journal"])

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
