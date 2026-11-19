import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/place-photo.py"
spec = importlib.util.spec_from_file_location("place_photo", SCRIPT)
place_photo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(place_photo)

PAGE = "https://www.hotel.example/rooms/double.html"


class ImageOnPageTests(unittest.TestCase):
    def test_relative_absolute_and_lazy_images_are_found(self):
        html = ('<img src="../images/room-1.jpg"><img data-src="https://cdn.hotel.example/a/b.webp">'
                '<div style="background:url(/img/hall.png)"></div>')
        for image in ("https://www.hotel.example/images/room-1.jpg", "https://cdn.hotel.example/a/b.webp",
                      "https://www.hotel.example/img/hall.png"):
            with self.subTest(image=image):
                self.assertTrue(place_photo.image_on_page(html, PAGE, image))

    def test_unrelated_picture_is_refused(self):
        html = '<img src="/images/room-1.jpg">'
        pikachu = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/25.png"
        self.assertFalse(place_photo.image_on_page(html, PAGE, pikachu))
        self.assertFalse(place_photo.image_on_page(html, PAGE, "https://other.example/images/room-1.jpg"))

    def test_http_addresses_are_refused(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "x", "--page", "http://a.example/", "--image",
                                 "https://a.example/b.jpg", "--label", "外觀", "--for", "外觀"],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)


# 2026-11-19 (Telegram): a plan for 「溪景湯屋平日 1 小時」 came with 湯布苑's lobby photo, labelled by
# the bot. The 溪景湯屋 photos were background pictures the bot's <img> listing never showed.
YUFUIN = """<section id="rooms"><div class="js-slide" style="background-image: url(https://cdn.spa.example/rooms/a1.jpg);"></div>
<div class="js-slide" style="background-image: url(https://cdn.spa.example/rooms/a2.jpg);">
<h3>溪景湯屋</h3><p>定價:NT$1,500 &nbsp;/&nbsp; 面積:4坪 舒適整潔的湯屋</p></div>
<div class="js-slide" style="background-image: url(https://cdn.spa.example/rooms/b1.jpg);"><h3>溪景套房</h3>
<p>舒適整潔的套房，泡湯池可以欣賞烏來溪景，在這乾淨的湯屋空間裡</p></div></section>
<section id="facilities"><img class="img-fluid" src="https://cdn.spa.example/facilities/lobby.jpg" alt="Image description">
<h4>1F 休憩大廳</h4><p>時尚的休憩大廳，讓您泡湯後享用美味甜點</p></section>
<img src="https://cdn.spa.example/logo/logo.png"><script>var x = "https://cdn.spa.example/rooms/zz.jpg";</script>
<a data-caption="湯屋" style="background-image: url(https://cdn.spa.example/gallery/g1.jpg)"></a>"""
SPA = "https://www.spa.example/"


class PhotoFitsThePlanTests(unittest.TestCase):
    def test_every_photo_is_listed_with_the_words_beside_it(self):
        photos = place_photo.photos_on_page(YUFUIN, SPA)
        self.assertEqual(list(photos), ["https://cdn.spa.example/rooms/a1.jpg", "https://cdn.spa.example/rooms/a2.jpg",
                                        "https://cdn.spa.example/rooms/b1.jpg",
                                        "https://cdn.spa.example/facilities/lobby.jpg",
                                        "https://cdn.spa.example/gallery/g1.jpg"])  # no logo, nothing from scripts
        self.assertEqual(photos["https://cdn.spa.example/rooms/a1.jpg"], [])
        self.assertTrue(photos["https://cdn.spa.example/rooms/a2.jpg"][0].startswith("溪景湯屋 定價:NT$1,500"))
        self.assertTrue(photos["https://cdn.spa.example/facilities/lobby.jpg"][0].startswith("1F 休憩大廳"))
        self.assertEqual(photos["https://cdn.spa.example/gallery/g1.jpg"], ["湯屋"])
        for words in photos.values():
            for w in words:
                self.assertNotIn("<", w)

    def test_the_photo_must_show_what_the_plan_recommends(self):
        plan = "溪景湯屋平日 1 小時"
        self.assertTrue(place_photo.related("溪景湯屋", plan))
        self.assertTrue(place_photo.related("湯屋", plan))
        self.assertFalse(place_photo.related("休憩大廳", plan))
        self.assertFalse(place_photo.related("溪景套房", plan))  # shares 溪景, but another room
        self.assertTrue(place_photo.related("雙人房", "週六雙人房"))
        self.assertEqual(place_photo.first_words("1F 休憩大廳 時尚的休憩大廳"), "休憩大廳")

    def test_the_label_must_be_the_pages_own_words(self):
        caption = place_photo.caption_of(YUFUIN, SPA, "https://cdn.spa.example/facilities/lobby.jpg")
        self.assertIn("休憩大廳", caption)
        self.assertNotIn("溪景湯屋", caption)  # so labelling the lobby 「溪景湯屋」 is refused

    def test_saving_needs_what_the_plan_recommends(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "x", "--page", SPA, "--image",
                                 "https://cdn.spa.example/facilities/lobby.jpg", "--label", "休憩大廳"],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--for", result.stderr)


if __name__ == "__main__":
    unittest.main()
